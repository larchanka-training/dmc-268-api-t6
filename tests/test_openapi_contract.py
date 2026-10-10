"""Keep contracts/openapi.yaml in step with the FastAPI app and the UI Zod contract."""

from __future__ import annotations

import copy
import json
import re
from collections.abc import AsyncIterator, Callable, Iterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime
from functools import cache
from pathlib import Path
from typing import Any, Self, get_args
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

from app.bootstrap.reviews_api import (
    get_cancel_run,
    get_pull_requests,
    get_repository_settings,
    get_rerun_uow_factory,
    get_run_publisher,
)
from app.common.application.github_repository_name import REPOSITORY_FULL_NAME_MAX_LENGTH
from app.main import app, get_file_blob_cache, get_run_event_hub, get_run_repository
from app.modules.repositories.application.repository_settings import (
    RepositorySettings,
    RepositorySettingsChange,
)
from app.modules.reviews.application.cancel_run import CancelRequestResult, CancelRun
from app.modules.reviews.application.get_run_actions import RunActionResponse
from app.modules.reviews.application.get_run_diff import DiffSnapshot
from app.modules.reviews.application.get_run_file_lines import (
    BlobCacheEntry,
    BlobCacheKey,
    BlobCacheStatus,
)
from app.modules.reviews.application.list_pulls import LatestRunRow, PullRequestRow
from app.modules.reviews.application.list_runs import RunCursor, RunListItem
from app.modules.reviews.application.rerun_run import RerunOutcome, RerunResult
from app.modules.reviews.application.review_output import Category, Severity
from app.modules.reviews.application.run_events import RunUpdated
from app.modules.reviews.application.try_enqueue_webhook_run import PendingRunMessage
from tests.portal_test_client import authenticated_test_client
from tests.test_ui_zod_contracts import (
    RUN_ID,
    RUN_ORIGIN_CASES,
    ContractRepository,
    _generated_schemas,
    run_origin_responses,
)

SPEC_PATH = Path(__file__).parents[1] / "contracts" / "openapi.yaml"
HTTP_METHODS = frozenset({"get", "put", "post", "delete", "options", "head", "patch", "trace"})
# api#20 D9: the webhook receiver and the liveness probe are not part of the browser API.
EXCLUDED_APP_PATHS = frozenset({"/healthcheck", "/webhooks/github"})
# DTOs shared with the UI: Zod snapshot name -> contracts/openapi.yaml component.
UI_ZOD_COMPONENTS = {
    "runSession": "RunSession",
    "runAction": "RunAction",
    "reviewComment": "ReviewComment",
    "runDetail": "RunDetail",
    "findingView": "FindingView",
    "runListPage": "RunListPage",
    "runUpdatedEvent": "RunUpdatedEvent",
    "repository": "Repository",
    "repositoryUpdate": "RepositoryUpdate",
    "rawFileDiff": "RawFileDiff",
    "fileSlice": "FileSlice",
    "authSession": "AuthSession",
    "me": "Me",
}
# z.int() exports the safe-integer range as its bounds; the spec leaves such integers unbounded.
ZOD_SAFE_INTEGER = 2**53 - 1
# Keywords that make a schema typed; a schema with none of them accepts any JSON value, null too.
TYPED_KEYWORDS = frozenset({"type", "properties", "items", "enum", "const", "anyOf", "oneOf"})
UNKNOWN_RUN_ID = UUID("99999999-9999-4999-8999-999999999999")
EXPIRED_PATH = "expired.py"
RUN_URL = f"/api/runs/{RUN_ID}"
REPOSITORY_ID = UUID("22222222-2222-4222-8222-222222222222")
CONFLICT_RUN_ID = UUID("88888888-8888-4888-8888-888888888888")


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

    async def create_rerun(self, run_id: UUID, now: datetime) -> RerunResult:
        if run_id == CONFLICT_RUN_ID:
            return RerunResult(RerunOutcome.CONFLICT)
        if run_id != RUN_ID:
            return RerunResult(RerunOutcome.NOT_FOUND)
        return RerunResult(
            RerunOutcome.CREATED,
            PendingRunMessage(
                run_id=RUN_ID,
                workspace_id=REPOSITORY_ID,
                installation_id=17,
                repository_id=REPOSITORY_ID,
                repository_external_id=101,
                repository_full_name="org/repo",
                pr_number=1,
                head_sha="a" * 40,
                base_sha="b" * 40,
                base_ref="main",
                engine="fast",
                rule_version_id=REPOSITORY_ID,
                prompt_version_id=REPOSITORY_ID,
                attempt=1,
                requested_at=now,
            ),
        )

    async def mark_rerun_published(self, run_id: UUID, now: datetime) -> None:
        return None

    @property
    def runs(self) -> OpenApiContractRepository:
        return self

    @property
    def repository(self) -> OpenApiContractRepository:
        return self

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        return None


REPOSITORY = RepositorySettings(
    id=REPOSITORY_ID,
    full_name="org/repo",
    url="https://github.test/org/repo",
    default_branch="main",
    enabled=True,
    default_engine="fast",
    wait_for_ci="auto",
    max_comments=10,
    review_event="COMMENT",
)


class ContractRepositories:
    """Repository settings store and its unit of work."""

    def __init__(self) -> None:
        self.item = REPOSITORY

    @property
    def repositories(self) -> ContractRepositories:
        return self

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        return None

    async def list_repositories(self) -> list[RepositorySettings]:
        return [self.item]

    async def get_repository(self, repository_id: UUID) -> RepositorySettings | None:
        return self.item if repository_id == REPOSITORY_ID else None

    async def update_repository(
        self, repository_id: UUID, change: RepositorySettingsChange
    ) -> RepositorySettings | None:
        if repository_id != REPOSITORY_ID:
            return None
        values = {key: value for key, value in vars(change).items() if value is not None}
        self.item = replace(self.item, **values)
        return self.item


class ContractPulls:
    async def list_pulls(
        self, repository_id: UUID, *, state: str, cursor: RunCursor | None, limit: int
    ) -> list[PullRequestRow] | None:
        if repository_id != REPOSITORY_ID:
            return None
        updated_at = datetime(2026, 9, 25, tzinfo=UTC)
        return [
            PullRequestRow(
                id=REPOSITORY_ID,
                number=1,
                title="Contract fixture",
                url="https://example.test/pr/1",
                author="octocat",
                head_sha="a" * 40,
                updated_at=updated_at,
                latest_run=LatestRunRow(RUN_ID, "succeeded", False, ("high",)),
            ),
            PullRequestRow(
                id=UNKNOWN_RUN_ID,
                number=2,
                title="Never reviewed",
                url="https://example.test/pr/2",
                author=None,
                head_sha="b" * 40,
                updated_at=updated_at,
                latest_run=None,
            ),
        ][:limit]


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
    repositories = ContractRepositories()
    pulls = ContractPulls()
    app.dependency_overrides[get_run_repository] = lambda: repository
    app.dependency_overrides[get_file_blob_cache] = lambda: blob_cache
    app.dependency_overrides[get_repository_settings] = lambda: lambda: repositories
    app.dependency_overrides[get_rerun_uow_factory] = lambda: lambda: repository
    app.dependency_overrides[get_pull_requests] = lambda: pulls
    app.dependency_overrides[get_run_publisher] = lambda: None
    app.dependency_overrides[get_cancel_run] = lambda: CancelRun(uow_factory=lambda: repository)
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
        pytest.param("post", "/api/runs/{run_id}/rerun", f"{RUN_URL}/rerun", "202", id="rerun"),
        pytest.param(
            "post",
            "/api/runs/{run_id}/rerun",
            f"/api/runs/{CONFLICT_RUN_ID}/rerun",
            "409",
            id="rerun-conflict",
        ),
        pytest.param("get", "/api/repos", "/api/repos", "200", id="repositories"),
        pytest.param(
            "get", "/api/repos/{repo_id}", f"/api/repos/{REPOSITORY_ID}", "200", id="repository"
        ),
        pytest.param(
            "get",
            "/api/repos/{repo_id}",
            f"/api/repos/{UNKNOWN_RUN_ID}",
            "404",
            id="repository-not-found",
        ),
        pytest.param(
            "get",
            "/api/repos/{repo_id}/pulls",
            f"/api/repos/{REPOSITORY_ID}/pulls?state=all",
            "200",
            id="pulls",
        ),
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
    # Comment frames (`: keepalive`) carry no event; every other frame is a field per line.
    frames = [frame for frame in response.text.split("\n\n") if frame and frame[0] != ":"]
    fields = dict(line.split(": ", 1) for line in frames[0].split("\n"))
    media_type = _escape("text/event-stream")
    pointer = f"/paths/{_escape('/api/stream')}/get/responses/200/content/{media_type}"

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert len(frames) == 1
    assert fields.keys() == {"id", "event", "data"}
    assert fields["event"] == "run.updated"
    assert fields["id"].isdecimal()
    _validator_at(f"{pointer}/x-events/run.updated").validate(json.loads(fields["data"]))


def test_stream_declares_the_last_event_id_header_the_app_accepts() -> None:
    def headers(operation: dict[str, Any]) -> list[tuple[str, bool]]:
        return [
            (parameter["name"].lower(), parameter.get("required", False))
            for parameter in operation.get("parameters", [])
            if parameter["in"] == "header"
        ]

    app_operation = app.openapi()["paths"]["/api/stream"]["get"]
    spec_operation = _spec()["paths"]["/api/stream"]["get"]

    assert headers(spec_operation) == [("last-event-id", False)]
    assert headers(app_operation) == headers(spec_operation)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("status", "completed"),
        ("pullRequest", {"repo": "org/repo"}),
        ("trigger", "cron"),
        ("createdAt", None),
    ],
)
def test_response_validation_rejects_drift(client: TestClient, field: str, value: object) -> None:
    page = client.get("/api/runs?limit=1").json()
    page["items"][0][field] = value

    with pytest.raises(ValidationError):
        _validator_for("get", "/api/runs", "200").validate(page)


@pytest.mark.parametrize("run_changes", RUN_ORIGIN_CASES)
def test_run_origin_validates_against_the_declared_schema(run_changes: dict[str, Any]) -> None:
    listed, detail = run_origin_responses(run_changes)

    _validator_for("get", "/api/runs", "200").validate(listed)
    _validator_for("get", "/api/runs/{run_id}", "200").validate(detail)


@pytest.mark.parametrize("field", ["trigger", "createdAt"])
def test_run_origin_is_required_in_the_list_and_the_detail(client: TestClient, field: str) -> None:
    page = client.get("/api/runs?limit=1").json()
    detail = client.get(RUN_URL).json()
    del page["items"][0][field]
    del detail[field]

    with pytest.raises(ValidationError):
        _validator_for("get", "/api/runs", "200").validate(page)
    with pytest.raises(ValidationError):
        _validator_for("get", "/api/runs/{run_id}", "200").validate(detail)


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


def _resolve(schema: Mapping[str, Any], components: Mapping[str, Any]) -> Mapping[str, Any]:
    while "$ref" in schema:
        schema = components[str(schema["$ref"]).removeprefix("#/components/schemas/")]
    return schema


def _split_null(
    schema: Mapping[str, Any], components: Mapping[str, Any]
) -> tuple[bool, Mapping[str, Any]]:
    """Return whether ``schema`` admits null and the schema of its non-null values."""
    schema = _resolve(schema, components)
    if not TYPED_KEYWORDS & schema.keys():
        return True, {}
    nullable = False
    branches = []
    for branch in schema.get("anyOf") or schema.get("oneOf") or [schema]:
        branch = _resolve(branch, components)
        types = branch.get("type")
        if types == "null":
            nullable = True
            continue
        if isinstance(types, list) and "null" in types:
            nullable = True
            rest = [name for name in types if name != "null"]
            branch = {**branch, "type": rest[0] if len(rest) == 1 else rest}
        branches.append(branch)
    if len(branches) == 1:
        return nullable, branches[0]
    return nullable, {"anyOf": branches}


def _bound(schema: Mapping[str, Any], keyword: str) -> object:
    value = schema.get(keyword)
    return None if value in (ZOD_SAFE_INTEGER, -ZOD_SAFE_INTEGER) else value


def _facets(schema: Mapping[str, Any]) -> dict[str, object]:
    """The value constraints compared field by field; Zod's ``const`` equals a one-item enum."""
    return {
        "type": schema.get("type"),
        "format": schema.get("format"),
        "enum": schema.get("enum", [schema["const"]] if "const" in schema else None),
        **{
            keyword: _bound(schema, keyword)
            for keyword in (
                "minimum",
                "exclusiveMinimum",
                "maximum",
                "exclusiveMaximum",
                "minLength",
                "maxLength",
                "minItems",
                "maxItems",
            )
        },
    }


def _contract_mismatches(
    spec: Mapping[str, Any], zod: Mapping[str, Any], components: Mapping[str, Any], path: str
) -> list[str]:
    """List every difference between a spec schema and its Zod JSON Schema, nested ones too."""
    spec_nullable, spec = _split_null(spec, components)
    zod_nullable, zod = _split_null(zod, components)
    mismatches = []
    if spec_nullable != zod_nullable:
        mismatches.append(f"{path}: nullable {spec_nullable} != {zod_nullable}")
    if "anyOf" in spec or "anyOf" in zod:
        # Fail closed: a union of several non-null schemas has no single shape to compare.
        return [*mismatches, f"{path}: a union of several non-null schemas is not compared"]
    spec_facets, zod_facets = _facets(spec), _facets(zod)
    mismatches += [
        f"{path}: {name} {spec_facets[name]!r} != {zod_facets[name]!r}"
        for name in spec_facets
        if spec_facets[name] != zod_facets[name]
    ]
    if "properties" in spec or "properties" in zod:
        spec_properties = spec.get("properties", {})
        zod_properties = zod.get("properties", {})
        if spec_properties.keys() != zod_properties.keys():
            mismatches.append(
                f"{path}: properties {sorted(spec_properties)} != {sorted(zod_properties)}"
            )
        spec_required = sorted(spec.get("required", []))
        zod_required = sorted(zod.get("required", []))
        if spec_required != zod_required:
            mismatches.append(f"{path}: required {spec_required} != {zod_required}")
        spec_closed = spec.get("additionalProperties") is False
        zod_closed = zod.get("additionalProperties") is False
        if not (spec_closed and zod_closed):
            mismatches.append(
                f"{path}: additionalProperties must be false on both sides: "
                f"spec {spec_closed}, zod {zod_closed}"
            )
        for name in sorted(spec_properties.keys() & zod_properties.keys()):
            mismatches += _contract_mismatches(
                spec_properties[name], zod_properties[name], components, f"{path}.{name}"
            )
    if "items" in spec or "items" in zod:
        mismatches += _contract_mismatches(
            spec.get("items", {}), zod.get("items", {}), components, f"{path}[]"
        )
    return mismatches


def test_ui_zod_snapshot_covers_the_shared_dtos() -> None:
    assert _generated_schemas().keys() == UI_ZOD_COMPONENTS.keys()
    assert len(UI_ZOD_COMPONENTS) >= 12


@pytest.mark.parametrize(("zod_name", "component"), UI_ZOD_COMPONENTS.items())
def test_component_schemas_mirror_the_ui_zod_contract(zod_name: str, component: str) -> None:
    components = _components()

    mismatches = _contract_mismatches(
        components[component], _generated_schemas()[zod_name], components, component
    )

    assert mismatches == []


@pytest.mark.parametrize(
    ("keyword", "kind"),
    [
        ("minimum", "integer"),
        ("exclusiveMinimum", "integer"),
        ("maximum", "integer"),
        ("exclusiveMaximum", "integer"),
        ("minLength", "string"),
        ("maxLength", "string"),
        ("minItems", "array"),
        ("maxItems", "array"),
    ],
)
def test_contract_mirror_compares_every_bound(keyword: str, kind: str) -> None:
    mismatches = _contract_mismatches({"type": kind, keyword: 3}, {"type": kind}, {}, "field")

    assert mismatches == [f"field: {keyword} 3 != None"]


def test_contract_mirror_does_not_pass_unions_it_cannot_compare() -> None:
    spec = {"anyOf": [{"type": "string"}, {"type": "integer"}, {"type": "null"}]}
    zod = {"anyOf": [{"type": "boolean"}, {"type": "object"}, {"type": "null"}]}

    assert _contract_mismatches(spec, zod, {}, "field") == [
        "field: a union of several non-null schemas is not compared"
    ]


@pytest.mark.parametrize(
    ("component", "path", "corrupt"),
    [
        pytest.param(
            "RunDetail",
            "RunDetail.severityCounts",
            lambda schema: schema["properties"].update(
                severityCounts={"anyOf": [schema["properties"]["severityCounts"], {"type": "null"}]}
            ),
            id="nullable-field",
        ),
        pytest.param(
            "ReviewComment",
            "ReviewComment.newLine",
            lambda schema: schema["properties"]["newLine"].update(minimum=0),
            id="lower-bound",
        ),
        pytest.param(
            "RunDetail",
            "RunDetail",
            lambda schema: schema["required"].remove("findings"),
            id="required",
        ),
        pytest.param(
            "Repository",
            "Repository",
            lambda schema: schema["properties"].update(archived={"type": "boolean"}),
            id="extra-field",
        ),
        pytest.param(
            "FileSlice",
            "FileSlice.startLine",
            lambda schema: schema["properties"]["startLine"].update(type="number"),
            id="type",
        ),
        pytest.param(
            "PullRequestRef",
            "RunSession.pullRequest.author",
            lambda schema: schema["properties"]["author"].update(type="string"),
            id="nested-ref",
        ),
        pytest.param(
            "Severity",
            "ReviewComment.severity",
            lambda schema: schema["enum"].reverse(),
            id="enum",
        ),
        pytest.param(
            "RunStatus",
            "RunSession.status",
            lambda schema: schema["enum"].remove("skipped"),
            id="enum-member",
        ),
        pytest.param(
            "Workspace",
            "Me.workspaces[]",
            lambda schema: schema["required"].remove("installationId"),
            id="array-items",
        ),
        pytest.param(
            "Repository",
            "Repository.maxComments",
            lambda schema: schema["properties"]["maxComments"].update(maximum=11),
            id="upper-bound",
        ),
        pytest.param(
            "PullRequestRef",
            "RunSession.pullRequest.number",
            lambda schema: schema["properties"].update(number={"type": "integer", "minimum": 1}),
            id="exclusive-bound",
        ),
        pytest.param(
            "Repository",
            "Repository.fullName",
            lambda schema: schema["properties"]["fullName"].pop("minLength"),
            id="string-bound",
        ),
        pytest.param(
            "RawFileDiff",
            "RawFileDiff",
            lambda schema: schema.update(additionalProperties=True),
            id="additional-properties",
        ),
        pytest.param(
            "Repository",
            "Repository.url",
            lambda schema: schema["properties"]["url"].pop("format"),
            id="format",
        ),
    ],
)
def test_contract_mirror_catches_a_corrupted_spec(
    component: str, path: str, corrupt: Callable[[dict[str, Any]], object]
) -> None:
    components = copy.deepcopy(dict(_components()))
    corrupt(components[component])
    root = path.split(".")[0]
    zod_name = next(name for name, parent in UI_ZOD_COMPONENTS.items() if parent == root)

    mismatches = _contract_mismatches(
        components[root], _generated_schemas()[zod_name], components, root
    )

    assert any(mismatch.startswith(f"{path}:") for mismatch in mismatches), mismatches


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
    assert components["RunTrigger"]["enum"] == zod["runSession"]["properties"]["trigger"]["enum"]
    queue_schema = json.loads((SPEC_PATH.parent / "schemas/review.run.v1.schema.json").read_text())
    queue_run = queue_schema["$defs"]["ReviewRunV1"]
    assert components["RunTrigger"]["enum"] == queue_run["properties"]["trigger"]["enum"]
    assert components["Severity"]["enum"] == list(get_args(Severity.__value__))
    assert components["Category"]["enum"] == list(get_args(Category.__value__))
    assert components["Severity"]["enum"] == zod["reviewComment"]["properties"]["severity"]["enum"]
    assert components["Category"]["enum"] == zod["reviewComment"]["properties"]["category"]["enum"]


def test_runs_repo_filter_bounds_match_the_repository_name_constant() -> None:
    parameters = _spec()["paths"]["/api/runs"]["get"]["parameters"]
    (repo,) = [parameter for parameter in parameters if parameter.get("name") == "repo"]

    assert repo["in"] == "query"
    assert (repo["schema"]["minLength"], repo["schema"]["maxLength"]) == (
        1,
        REPOSITORY_FULL_NAME_MAX_LENGTH,
    )


def test_repository_update_validates_and_returns_the_repository_schema(client: TestClient) -> None:
    url = f"/api/repos/{REPOSITORY_ID}"
    updated = client.patch(url, json={"waitForCi": "never", "maxComments": 3})
    review_event = client.patch(url, json={"reviewEvent": "REQUEST_CHANGES"})
    invalid = [
        client.patch(url, json=body)
        for body in (
            {"maxComments": 0},
            {"maxComments": 11},
            {"waitForCi": "sometimes"},
            {"reviewEvent": "APPROVE"},
            {"name": "renamed"},
            {"enabled": None},
            {},
            # deep (SandboxEngine) is phase 3: the API accepts only fast.
            {"defaultEngine": "deep"},
        )
    ]
    missing = client.patch(f"/api/repos/{UNKNOWN_RUN_ID}", json={"enabled": False})

    assert updated.status_code == 200
    assert (updated.json()["waitForCi"], updated.json()["maxComments"]) == ("never", 3)
    assert review_event.json()["reviewEvent"] == "REQUEST_CHANGES"
    _validator_for("patch", "/api/repos/{repo_id}", "200").validate(updated.json())
    assert [response.status_code for response in invalid] == [422] * 8
    assert missing.status_code == 404


def test_pulls_page_derives_the_latest_run_verdict(client: TestClient) -> None:
    page = client.get(f"/api/repos/{REPOSITORY_ID}/pulls").json()

    assert [item["latestRun"] for item in page["items"]] == [
        {"id": str(RUN_ID), "status": "succeeded", "verdict": "blocking"},
        None,
    ]
    assert page["nextCursor"] is None
    assert client.get(f"/api/repos/{REPOSITORY_ID}/pulls?state=merged").status_code == 422
    assert client.get(f"/api/repos/{REPOSITORY_ID}/pulls?cursor=bad").status_code == 422


def test_browser_contract_documents_auth_configuration_and_provider_failures() -> None:
    paths = _spec()["paths"]
    for path, item in paths.items():
        if path.startswith("/api/"):
            for method, operation in item.items():
                if method in HTTP_METHODS:
                    assert "503" in operation["responses"], (method, path)
    assert "502" in paths["/api/auth/github/callback"]["post"]["responses"]
