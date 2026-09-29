"""Keep contracts/openapi.yaml in step with the FastAPI app and the UI Zod contract."""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator, Iterator, Mapping
from contextlib import asynccontextmanager
from functools import cache
from pathlib import Path
from typing import Any, get_args
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from jsonschema.exceptions import ValidationError
from jsonschema.protocols import Validator
from openapi_schema_validator import OAS31Validator
from openapi_spec_validator import OpenAPIV31SpecValidator, validate
from openapi_spec_validator.readers import read_from_filename
from referencing import Registry
from referencing.jsonschema import DRAFT202012

from app.main import app, get_file_blob_cache, get_run_event_hub, get_run_repository
from app.modules.reviews.application.cancel_run import CancelRequestResult
from app.modules.reviews.application.get_run_actions import RunActionResponse
from app.modules.reviews.application.get_run_diff import DiffSnapshot
from app.modules.reviews.application.get_run_file_lines import (
    BlobCacheEntry,
    BlobCacheKey,
    BlobCacheStatus,
)
from app.modules.reviews.application.list_runs import RunCursor, RunListItem
from app.modules.reviews.application.review_output import Category, Severity
from app.modules.reviews.application.run_events import RunUpdated
from tests.portal_test_client import authenticated_test_client
from tests.test_ui_zod_contracts import RUN_ID, ContractRepository, _generated_schemas

SPEC_PATH = Path(__file__).parents[1] / "contracts" / "openapi.yaml"
HTTP_METHODS = frozenset({"get", "put", "post", "delete", "options", "head", "patch", "trace"})
# api#20 D9: the webhook receiver and the liveness probe are not part of the browser API.
EXCLUDED_APP_PATHS = frozenset({"/healthcheck", "/webhooks/github"})
# Optional PullRequestRef fields (#34). The UI Zod contract gains them in ui#57; until the
# snapshot is regenerated it may lack any of them, and where present they stay optional.
PLANNED_PULL_REQUEST_FIELDS = frozenset({"author", "headRef", "baseRef"})
UNKNOWN_RUN_ID = UUID("99999999-9999-4999-8999-999999999999")
EXPIRED_PATH = "expired.py"
RUN_URL = f"/api/runs/{RUN_ID}"
REPOSITORY_ID = UUID("22222222-2222-4222-8222-222222222222")


@cache
def _load_spec() -> tuple[Mapping[str, Any], str]:
    return read_from_filename(str(SPEC_PATH))


def _spec() -> Mapping[str, Any]:
    return _load_spec()[0]


def _components() -> Mapping[str, Any]:
    schemas: Mapping[str, Any] = _spec()["components"]["schemas"]
    return schemas


def _operations(paths: Mapping[str, Any]) -> dict[tuple[str, str], Mapping[str, Any]]:
    """Key operations by method and template path with parameter names erased."""
    return {
        (method, re.sub(r"\{[^}]+\}", "{}", path)): operation
        for path, item in paths.items()
        for method, operation in item.items()
        if method in HTTP_METHODS
    }


def _escape(token: str) -> str:
    return token.replace("~", "~0").replace("/", "~1")


def _response_schema_pointer(method: str, path: str, status: str) -> str:
    """Locate the declared JSON body schema, following a response-level ``$ref``."""
    pointer = f"/paths/{_escape(path)}/{method}/responses/{status}"
    reference = _spec()["paths"][path][method]["responses"][status].get("$ref")
    if reference is not None:
        pointer = str(reference).removeprefix("#")
    return f"{pointer}/content/{_escape('application/json')}/schema"


@cache
def _registry() -> Registry[Any]:
    spec, base_uri = _load_spec()
    registry: Registry[Any] = Registry().with_resource(
        base_uri, DRAFT202012.create_resource(dict(spec))
    )
    return registry


def _validator_at(pointer: str) -> Validator:
    """Validate against the schema at ``pointer``, resolving ``$ref`` inside the whole document."""
    validator: Validator = OAS31Validator(
        {"$ref": f"{_load_spec()[1]}#{pointer}"},
        registry=_registry(),
        format_checker=OAS31Validator.FORMAT_CHECKER,
    )
    return validator


def _validator_for(method: str, path: str, status: str) -> Validator:
    return _validator_at(_response_schema_pointer(method, path, status))


class OpenApiContractRepository(ContractRepository):
    """The UI contract fixture, extended to every implemented run endpoint."""

    async def list_runs(
        self,
        *,
        status: str | None,
        repository: str | None,
        cursor: RunCursor | None,
        limit: int,
    ) -> list[RunListItem]:
        item = await self.get_run(RUN_ID)
        assert item is not None
        return [item, item]

    async def request_cancel(self, run_id: UUID) -> CancelRequestResult:
        return CancelRequestResult(found=run_id == RUN_ID, changed=False)

    async def get_run_action_response(self, run_id: UUID, index: int) -> RunActionResponse | None:
        if run_id != RUN_ID or index != 0:
            return None
        return RunActionResponse(response={"ok": True})

    async def get_run_diff(self, run_id: UUID) -> list[DiffSnapshot] | None:
        if run_id != RUN_ID:
            return None
        patch = "diff --git a/app/service.py b/app/service.py\n@@ -1 +1 @@\n-old\n+new\n"
        return [
            DiffSnapshot(filename="app/service.py", patch=patch),
            DiffSnapshot(filename="uv.lock", patch=None),
        ]

    async def get_run_file_key(self, run_id: UUID, path: str) -> BlobCacheKey | None:
        if run_id != RUN_ID:
            return None
        return BlobCacheKey(
            repository_id=REPOSITORY_ID,
            blob_sha=("e" if path == EXPIRED_PATH else "a") * 40,
        )


class ContractBlobCache:
    async def get(self, key: BlobCacheKey) -> BlobCacheEntry:
        if key.blob_sha == "e" * 40:
            return BlobCacheEntry(status=BlobCacheStatus.EXPIRED, content=None)
        return BlobCacheEntry(status=BlobCacheStatus.HIT, content="one\ntwo\nthree\n")


class OneEventHub:
    """Ends the stream after one update so the test client can read the whole body."""

    @asynccontextmanager
    async def subscribe(self) -> AsyncIterator[AsyncIterator[RunUpdated]]:
        async def events() -> AsyncIterator[RunUpdated]:
            yield RunUpdated(run_id=RUN_ID, status="running")

        yield events()


@pytest.fixture
def client() -> Iterator[TestClient]:
    repository = OpenApiContractRepository()
    blob_cache = ContractBlobCache()
    app.dependency_overrides[get_run_repository] = lambda: repository
    app.dependency_overrides[get_file_blob_cache] = lambda: blob_cache
    try:
        yield authenticated_test_client(app)
    finally:
        app.dependency_overrides.clear()


def test_document_is_valid_openapi_3_1() -> None:
    spec, base_uri = _load_spec()

    assert str(spec["openapi"]).startswith("3.1.")
    validate(spec, base_uri=base_uri, cls=OpenAPIV31SpecValidator)


def test_spec_operations_match_the_app_routes() -> None:
    app_paths = app.openapi()["paths"]
    assert app_paths.keys() >= EXCLUDED_APP_PATHS
    assert EXCLUDED_APP_PATHS.isdisjoint(_spec()["paths"])

    app_operations = set(
        _operations(
            {path: item for path, item in app_paths.items() if path not in EXCLUDED_APP_PATHS}
        )
    )
    spec_operations = _operations(_spec()["paths"])
    planned = {key for key, operation in spec_operations.items() if operation.get("x-status")}
    implemented = spec_operations.keys() - planned

    assert app_operations - spec_operations.keys() == set(), "app routes missing from the spec"
    assert implemented - app_operations == set(), "spec operations missing from the app"
    assert planned & app_operations == set(), "implemented operations still marked planned"


def test_every_operation_declares_its_service_and_plan() -> None:
    for (method, path), operation in _operations(_spec()["paths"]).items():
        where = f"{method.upper()} {path}"
        service = "auth-api" if path.startswith("/api/auth/") else "portal-api"

        assert operation.get("operationId"), where
        assert operation.get("tags"), where
        assert operation.get("x-service") == service, where
        assert operation.get("x-status") in {None, "planned"}, where
        if operation.get("x-status") == "planned":
            assert "x-issue" in operation or "x-note" in operation, where


@pytest.mark.parametrize(
    ("method", "spec_path", "url", "status"),
    [
        pytest.param("get", "/api/runs", "/api/runs?limit=1", "200", id="list-runs"),
        pytest.param(
            "get",
            "/api/runs/{run_id}",
            RUN_URL,
            "200",
            id="run-detail",
            marks=pytest.mark.xfail(
                raises=ValidationError, strict=True, reason="#34: run detail per api#20 D3"
            ),
        ),
        pytest.param("post", "/api/runs/{run_id}/cancel", f"{RUN_URL}/cancel", "200", id="cancel"),
        pytest.param(
            "get", "/api/runs/{run_id}/comments", f"{RUN_URL}/comments", "200", id="comments"
        ),
        pytest.param(
            "get", "/api/runs/{run_id}/actions", f"{RUN_URL}/actions", "200", id="actions"
        ),
        pytest.param(
            "get",
            "/api/runs/{run_id}/actions/{index}/response",
            f"{RUN_URL}/actions/0/response",
            "200",
            id="action-response",
        ),
        pytest.param("get", "/api/runs/{run_id}/diff", f"{RUN_URL}/diff", "200", id="diff"),
        pytest.param(
            "get",
            "/api/runs/{run_id}/files",
            f"{RUN_URL}/files?path=app/service.py&limit=2",
            "200",
            id="file-slice",
        ),
        pytest.param(
            "get", "/api/runs/{run_id}", f"/api/runs/{UNKNOWN_RUN_ID}", "404", id="run-not-found"
        ),
        pytest.param(
            "get",
            "/api/runs/{run_id}/files",
            f"{RUN_URL}/files?path={EXPIRED_PATH}",
            "410",
            id="file-expired",
        ),
        pytest.param("get", "/api/runs", "/api/runs?limit=0", "422", id="invalid-limit"),
        pytest.param("get", "/api/runs", "/api/runs?cursor=not-a-cursor", "422", id="bad-cursor"),
    ],
)
def test_app_responses_validate_against_the_declared_schema(
    client: TestClient, method: str, spec_path: str, url: str, status: str
) -> None:
    response = client.request(method.upper(), url)

    assert response.status_code == int(status)
    _validator_for(method, spec_path, status).validate(response.json())


def test_stream_events_validate_against_the_declared_event_schema() -> None:
    app.dependency_overrides[get_run_event_hub] = OneEventHub
    app.dependency_overrides[get_run_repository] = OpenApiContractRepository
    try:
        response = authenticated_test_client(app).get("/api/stream")
    finally:
        app.dependency_overrides.clear()
    event, data = response.text.strip().split("\n")
    media_type = _escape("text/event-stream")
    pointer = f"/paths/{_escape('/api/stream')}/get/responses/200/content/{media_type}"

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert event == "event: run.updated"
    _validator_at(f"{pointer}/x-events/run.updated").validate(
        json.loads(data.removeprefix("data: "))
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [("status", "completed"), ("pullRequest", {"repo": "org/repo"})],
)
def test_response_validation_rejects_drift(client: TestClient, field: str, value: object) -> None:
    page = client.get("/api/runs?limit=1").json()
    page["items"][0][field] = value

    with pytest.raises(ValidationError):
        _validator_for("get", "/api/runs", "200").validate(page)


def test_run_detail_schema_accepts_a_complete_run(client: TestClient) -> None:
    session = client.get(RUN_URL).json()
    detail = {
        **session,
        "pullRequest": {
            **session["pullRequest"],
            "author": "octocat",
            "headRef": "feature",
            "baseRef": "main",
        },
        "findings": [
            {
                "id": "44444444-4444-4444-8444-444444444444",
                "file": "app/service.py",
                "oldLine": None,
                "newLine": 23,
                "endLine": 25,
                "side": "RIGHT",
                "severity": "high",
                "category": "correctness",
                "title": "Contract fixture",
                "body": "The API response must follow the contract.",
                "suggestion": None,
                "confidence": 0.8,
                "ruleName": None,
            }
        ],
        "summary": {"problem": "One bug.", "doneWell": "Clear names.", "effort": "small"},
        "verdict": "blocking",
        "severityCounts": {"critical": 0, "high": 1, "medium": 0, "low": 0, "info": 0},
        "budget": {
            "tokensIn": 1200,
            "tokensOut": 300,
            "costUsd": 0.02,
            "tokenLimit": 60000,
            "costLimitUsd": 0.5,
        },
    }

    _validator_for("get", "/api/runs/{run_id}", "200").validate(detail)


@pytest.mark.parametrize(
    ("zod_name", "component"),
    [("runSession", "RunSession"), ("runAction", "RunAction"), ("reviewComment", "ReviewComment")],
)
def test_component_schemas_mirror_the_ui_zod_contract(zod_name: str, component: str) -> None:
    zod = _generated_schemas()[zod_name]
    schema = _components()[component]

    assert schema["properties"].keys() == zod["properties"].keys()
    assert set(schema["required"]) == set(zod["required"])
    assert schema["additionalProperties"] is zod["additionalProperties"] is False


def test_pull_request_ref_extends_the_ui_contract_only_with_optional_fields() -> None:
    zod = _generated_schemas()["runSession"]["properties"]["pullRequest"]
    schema = _components()["PullRequestRef"]

    assert schema["properties"].keys() - zod["properties"].keys() <= PLANNED_PULL_REQUEST_FIELDS
    assert zod["properties"].keys() <= schema["properties"].keys()
    assert set(schema["required"]) == set(zod["required"])
    assert (PLANNED_PULL_REQUEST_FIELDS & zod["properties"].keys()).isdisjoint(zod["required"])


@pytest.mark.parametrize(("base", "extended"), [("RunSession", "RunDetail"), ("User", "Me")])
def test_flat_extensions_repeat_their_base_schema(base: str, extended: str) -> None:
    base_schema = _components()[base]
    extended_schema = _components()[extended]
    repeated = {name: extended_schema["properties"].get(name) for name in base_schema["properties"]}

    assert repeated == base_schema["properties"]
    assert set(base_schema["required"]) <= set(extended_schema["required"])


def test_enums_match_the_ui_contract_and_the_review_output_model() -> None:
    zod = _generated_schemas()
    components = _components()

    assert components["RunStatus"]["enum"] == zod["runSession"]["properties"]["status"]["enum"]
    assert len(components["RunStatus"]["enum"]) == 7
    assert components["Severity"]["enum"] == list(get_args(Severity.__value__))
    assert components["Category"]["enum"] == list(get_args(Category.__value__))
    assert components["Severity"]["enum"] == zod["reviewComment"]["properties"]["severity"]["enum"]
    assert components["Category"]["enum"] == zod["reviewComment"]["properties"]["category"]["enum"]
