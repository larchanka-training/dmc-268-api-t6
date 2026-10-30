"""Keep contracts/openapi.yaml in step with the FastAPI app and the UI Zod contract."""

from __future__ import annotations

import ast
import copy
import json
import re
from collections.abc import AsyncIterator, Callable, Iterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from functools import cache
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Self, get_args
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError
from jsonschema.protocols import Validator
from openapi_schema_validator import OAS31Validator
from openapi_spec_validator import OpenAPIV31SpecValidator, validate
from openapi_spec_validator.readers import read_from_filename
from pydantic import ValidationError as PydanticValidationError
from referencing import Registry
from referencing.jsonschema import DRAFT202012

from app.bootstrap.bitbucket import get_bitbucket_management, get_bitbucket_workspace_scope
from app.bootstrap.portal_auth import get_auth_scope
from app.bootstrap.reviews_api import (
    get_cancel_run,
    get_pull_requests,
    get_repository_settings,
    get_rerun_uow_factory,
    get_rules_status,
    get_run_publisher,
)
from app.common.application.github_repository_name import REPOSITORY_FULL_NAME_MAX_LENGTH
from app.main import (
    app,
    get_file_blob_cache,
    get_run_event_hub,
    get_run_repository,
    to_rules_status_dto,
)
from app.modules.auth.application.scope import AuthScope
from app.modules.code_index.domain.enums import PurgeReason, RagIndexStatus
from app.modules.code_index.domain.rag_index import RagIndex
from app.modules.integrations.bitbucket.api.connections import ConnectionItem, ConnectResponse
from app.modules.integrations.bitbucket.application.manage_connection import (
    BitbucketManagementError,
    ConnectResult,
    RepositoryBinding,
)
from app.modules.integrations.bitbucket.infrastructure.repository_provider import (
    BitbucketProviderError,
)
from app.modules.repositories.application.repository_settings import (
    RepositorySettings,
    RepositorySettingsChange,
)
from app.modules.repositories.application.rules_status import (
    GetRulesStatus,
    RulesDiagnostic,
    RulesStatus,
    RulesStatusKind,
    StoredRulesStatus,
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
from app.modules.reviews.application.rerun_run import RerunFacts
from app.modules.reviews.application.review_output import Category, Severity
from app.modules.reviews.application.run_events import RunUpdated
from app.modules.reviews.application.run_trigger import RunTrigger
from app.modules.reviews.application.try_enqueue_webhook_run import (
    CandidateMiss,
    PendingRunMessage,
    RunInsertCandidate,
)
from tests.portal_test_client import authenticated_test_client
from tests.test_branch_patterns import INVALID_BRANCH_PATTERNS
from tests.test_rerun_origin import facts as rerun_facts
from tests.test_ui_zod_contracts import (
    RUN_ID,
    RUN_ORIGIN_CASES,
    ContractRepository,
    _generated_schemas,
    run_origin_responses,
)

SPEC_PATH = Path(__file__).parents[1] / "contracts" / "openapi.yaml"
APP_PATH = Path(__file__).parents[1] / "app"
HTTP_METHODS = frozenset({"get", "put", "post", "delete", "options", "head", "patch", "trace"})
# api#20 D9: the webhook receiver and the liveness probe are not part of the browser API.
EXCLUDED_APP_PATHS = frozenset(
    {
        "/healthcheck",
        "/webhooks/github",
        "/webhooks/gitlab/{installationId}",
        "/webhooks/bitbucket/{installation_id}/{repository_id}",
    }
)
# DTOs shared with the UI: Zod snapshot name -> contracts/openapi.yaml component.
UI_ZOD_COMPONENTS = {
    "runSession": "RunSession",
    "runAction": "RunAction",
    "reviewComment": "ReviewComment",
    "runDetail": "RunDetail",
    "findingView": "FindingView",
    "runListPage": "RunListPage",
    "runUpdatedEvent": "RunUpdatedEvent",
    "runStepEvent": "RunStepEvent",
    "repository": "Repository",
    "repositoryUpdate": "RepositoryUpdate",
    "rawFileDiff": "RawFileDiff",
    "fileSlice": "FileSlice",
    "authSession": "AuthSession",
    "me": "Me",
    "bitbucketDiscoveryRequest": "BitbucketDiscoveryRequest",
    "bitbucketDiscoveredRepository": "BitbucketDiscoveredRepository",
    "bitbucketDiscoveryResponse": "BitbucketDiscoveryResponse",
    "bitbucketConnectRequest": "BitbucketConnectRequest",
    "bitbucketWebhookSetup": "BitbucketWebhookSetup",
    "bitbucketConnectResponse": "BitbucketConnectResponse",
    "bitbucketConnectionItem": "BitbucketConnectionItem",
    "bitbucketConnectionList": "BitbucketConnectionList",
    "bitbucketCredentialRotateRequest": "BitbucketCredentialRotateRequest",
    "bitbucketCredentialRotateResponse": "BitbucketCredentialRotateResponse",
}
# z.int() exports the safe-integer range as its bounds; the spec leaves such integers unbounded.
ZOD_SAFE_INTEGER = 2**53 - 1
# Keywords that make a schema typed; a schema with none of them accepts any JSON value, null too.
TYPED_KEYWORDS = frozenset({"type", "properties", "items", "enum", "const", "anyOf", "oneOf"})
# api#165: every step name the code writes to run_actions (docs/PIPELINE_SPEC.md §2). The column
# is free text, not an enum, so nothing else catches a tool missing from the contract.
RUN_ACTION_TOOLS = frozenset(
    {
        "policy.load",
        "vcs.fetch_diff",
        "context.build",
        "context.rag",
        "context.manifest",
        "llm.repo_conventions",
        "llm.call",
        "llm.review_output",
        "llm.verify",
        "review.postprocess",
        "github.publish_review",
        "gitlab.publish_review",
        "bitbucket.publish_review",
    }
)
# A backticked `<lower>.<lower_snake>` token; state names and docs/ paths do not match.
BACKTICKED_TOOL = re.compile(r"`([a-z]+\.[a-z_]+)`")
UNKNOWN_RUN_ID = UUID("99999999-9999-4999-8999-999999999999")
EXPIRED_PATH = "expired.py"
RUN_URL = f"/api/runs/{RUN_ID}"
REPOSITORY_ID = UUID("22222222-2222-4222-8222-222222222222")
ORIGIN_CHANGED_RUN_ID = UUID(int=169)
CONFLICT_RUN_ID = UUID("88888888-8888-4888-8888-888888888888")
NOT_CONFIGURED_RUN_ID = UUID("77777777-7777-4777-8777-777777777777")
# PATCH /api/repos/{id} bodies the API rejects with 422 (#149, #145); the PostgreSQL API test
# reuses them.
INVALID_REVIEW_SETTINGS: dict[str, dict[str, object]] = {
    **{
        f"branch-filter-{name}": {"branchFilter": [pattern]}
        for name, pattern in INVALID_BRANCH_PATTERNS.items()
    },
    "branch-filter-21-patterns": {"branchFilter": [f"release/{number}" for number in range(21)]},
    "branch-filter-duplicates": {"branchFilter": ["main", "main"]},
    "branch-filter-null": {"branchFilter": None},
    "branch-filter-not-a-list": {"branchFilter": "main"},
    "severity-threshold-unknown": {"severityThreshold": "high"},
    "severity-threshold-null": {"severityThreshold": None},
    "installation-available": {"installationAvailable": True},
    "rag-enabled-null": {"ragEnabled": None},
    "rag-index": {
        "ragIndex": {
            "status": "ready",
            "indexedCommitSha": "a" * 40,
            "indexedAt": "2026-10-16T09:30:00Z",
            "fileCount": 1,
            "symbolCount": 1,
            "lastErrorCode": None,
        }
    },
}


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

    async def load_rerun_facts(self, run_id: UUID) -> RerunFacts | None:
        value = rerun_facts()
        if run_id == CONFLICT_RUN_ID:
            return replace(value, active_run=True)
        if run_id == ORIGIN_CHANGED_RUN_ID:
            return replace(value, origin=replace(value.origin, installation_id=None))
        if run_id == NOT_CONFIGURED_RUN_ID:
            return replace(value, candidate=CandidateMiss.MISSING_RULES)
        if run_id != RUN_ID:
            return None
        return value

    async def insert_rerun(
        self, candidate: RunInsertCandidate, code_change_id: UUID, now: datetime
    ) -> PendingRunMessage:
        return PendingRunMessage(
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
    provider="github",
    id=REPOSITORY_ID,
    full_name="org/repo",
    url="https://github.test/org/repo",
    default_branch="main",
    enabled=True,
    default_engine="fast",
    wait_for_ci="auto",
    max_comments=10,
    review_event="COMMENT",
    branch_filter=(),
    severity_threshold="all",
    installation_available=True,
    rag_enabled=False,
    rag_index=RagIndex(RagIndexStatus.DISABLED, None, None, 0, 0, None),
)


class ContractRepositories:
    """Repository settings store and its unit of work."""

    def __init__(self) -> None:
        self.item = REPOSITORY

    @property
    def repositories(self) -> ContractRepositories:
        return self

    @property
    def index_purger(self) -> ContractRepositories:
        return self

    async def purge(self, repository_id: UUID, reason: PurgeReason) -> None:
        return None

    @property
    def index_enabler(self) -> ContractRepositories:
        return self

    async def enable(self, repository_id: UUID) -> None:
        return None

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
        if change.rag_enabled is not None:
            # As the store reads it for a repository without a state row (#145).
            status = RagIndexStatus.EMPTY if change.rag_enabled else RagIndexStatus.DISABLED
            self.item = replace(self.item, rag_index=RagIndex(status, None, None, 0, 0, None))
        return self.item


CHECKED_AT = datetime(2026, 10, 18, 9, 0, tzinfo=UTC)
_TOO_LARGE = RulesDiagnostic(
    "policy_document_too_large", "The rules document is over 64 KiB.", None, None, False, 65536
)
_TRANSIENT = RulesDiagnostic(
    "policy_fetch_transient", "Could not read the rules documents from GitHub.", None, None, True
)
# One stored result of every variant; None is a repository that was never read.
RULES_STATUSES: dict[str, RulesStatus | None] = {
    "present": RulesStatus(
        RulesStatusKind.PRESENT,
        "main",
        "a" * 40,
        CHECKED_AT,
        text="# Rules\n",
        checksum="b" * 64,
        ignore_masks=("docs/**",),
    ),
    "present-empty": RulesStatus(
        RulesStatusKind.PRESENT,
        "main",
        "a" * 40,
        CHECKED_AT,
        text="",
        checksum="c" * 64,
        ignore_masks=(),
    ),
    "missing": RulesStatus(
        RulesStatusKind.MISSING, "main", "a" * 40, CHECKED_AT, defaults_version="v1"
    ),
    "invalid": RulesStatus(
        RulesStatusKind.INVALID,
        "main",
        "a" * 40,
        CHECKED_AT,
        diagnostics=(
            RulesDiagnostic("policy_ignore_pattern_invalid", "Mask.", 5, 6, False, pattern_index=0),
        ),
        text="```review-ignore\nsrc/a?.py\n```\n",
    ),
    "invalid-too-large": RulesStatus(
        RulesStatusKind.INVALID, "main", "a" * 40, CHECKED_AT, diagnostics=(_TOO_LARGE,)
    ),
    # A read that stopped past the cap knows only a lower bound of the size.
    "invalid-too-large-lower-bound": RulesStatus(
        RulesStatusKind.INVALID,
        "main",
        "a" * 40,
        CHECKED_AT,
        diagnostics=(replace(_TOO_LARGE, actual=69_632, actual_is_lower_bound=True),),
    ),
    "unavailable": RulesStatus(
        RulesStatusKind.UNAVAILABLE, "main", None, CHECKED_AT, diagnostics=(_TRANSIENT,)
    ),
    "pending": None,
}


class ContractRulesStatus:
    """Rules status store and its unit of work; requests are counted, not stored."""

    def __init__(self, status: RulesStatus | None = RULES_STATUSES["present"]) -> None:
        self.status = status
        self.requests = 0

    @property
    def rules_status(self) -> ContractRulesStatus:
        return self

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        return None

    async def get(self, repository_id: UUID) -> StoredRulesStatus | None:
        if repository_id != REPOSITORY_ID:
            return None
        return StoredRulesStatus(
            "main", self.status, refresh_requested=False, installation_available=True
        )

    async def request_refresh(self, repository_id: UUID) -> None:
        self.requests += 1


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
    rules_status = ContractRulesStatus()
    app.dependency_overrides[get_run_repository] = lambda: repository
    app.dependency_overrides[get_rules_status] = lambda: GetRulesStatus(
        lambda: rules_status, now=lambda: CHECKED_AT
    )
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
        pytest.param(
            "get",
            "/api/repos/{repo_id}/rules-status",
            f"/api/repos/{REPOSITORY_ID}/rules-status",
            "200",
            id="rules-status",
        ),
        pytest.param(
            "get",
            "/api/repos/{repo_id}/rules-status",
            f"/api/repos/{UNKNOWN_RUN_ID}/rules-status",
            "404",
            id="rules-status-not-found",
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


def test_rerun_not_configured_response_validates_against_declared_schema(
    client: TestClient,
) -> None:
    response = client.post(f"/api/runs/{NOT_CONFIGURED_RUN_ID}/rerun")

    assert response.status_code == 422
    assert response.json() == {"detail": "the repository has no active rule or prompt version"}
    _validator_for("post", "/api/runs/{run_id}/rerun", "422").validate(response.json())


def test_rerun_invalid_uuid_response_validates_against_declared_schema(client: TestClient) -> None:
    response = client.post("/api/runs/not-a-uuid/rerun")

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert isinstance(detail, list)
    assert len(detail) == 1
    assert detail[0]["type"] == "uuid_parsing"
    assert detail[0]["loc"] == ["path", "run_id"]
    assert detail[0]["input"] == "not-a-uuid"
    assert detail[0]["msg"].startswith("Input should be a valid UUID")
    assert "ctx" in detail[0]
    _validator_for("post", "/api/runs/{run_id}/rerun", "422").validate(response.json())


@pytest.mark.parametrize(
    ("schema_name", "body"),
    [
        ("BitbucketDiscoveryRequest", {"bitbucketWorkspace": "team"}),
        (
            "BitbucketConnectRequest",
            {
                "bitbucketWorkspace": "team",
                "repositorySlug": "repo",
                "repositoryUuid": "00000000-0000-4000-8000-000000000334",
            },
        ),
        ("BitbucketCredentialRotateRequest", {}),
    ],
)
def test_bitbucket_token_request_contract_matches_printable_ascii_limit(
    schema_name: str, body: dict[str, str]
) -> None:
    validator = _validator_at(f"/components/schemas/{schema_name}")
    for token in ("a", "x" * 4096):
        validator.validate({**body, "apiToken": token})
    for token in ("", "has space", "bad\nline", "secret\n", "bad\u00a0char", "x" * 4097):
        with pytest.raises(ValidationError):
            validator.validate({**body, "apiToken": token})


@pytest.mark.parametrize(
    ("generated_name", "checked_name", "fields"),
    [
        ("DiscoveryRequest", "BitbucketDiscoveryRequest", ("apiToken", "bitbucketWorkspace")),
        (
            "ConnectRequest",
            "BitbucketConnectRequest",
            ("apiToken", "bitbucketWorkspace", "repositorySlug"),
        ),
        ("CredentialRotateRequest", "BitbucketCredentialRotateRequest", ("apiToken",)),
    ],
)
def test_bitbucket_request_constraints_match_generated_openapi(
    generated_name: str, checked_name: str, fields: tuple[str, ...]
) -> None:
    generated = app.openapi()["components"]["schemas"][generated_name]["properties"]
    checked = _spec()["components"]["schemas"][checked_name]["properties"]
    for field in fields:
        for constraint in ("type", "minLength", "maxLength", "pattern", "writeOnly"):
            assert generated[field].get(constraint) == checked[field].get(constraint), (
                generated_name,
                field,
                constraint,
            )


@pytest.mark.parametrize("name", RULES_STATUSES)
def test_every_rules_status_variant_validates_against_the_declared_schema(name: str) -> None:
    store = ContractRulesStatus(RULES_STATUSES[name])
    app.dependency_overrides[get_rules_status] = lambda: GetRulesStatus(
        lambda: store, now=lambda: CHECKED_AT
    )
    try:
        response = authenticated_test_client(app).get(f"/api/repos/{REPOSITORY_ID}/rules-status")
    finally:
        app.dependency_overrides.clear()
    body = response.json()

    assert response.status_code == 200
    assert body["status"] == name.split("-")[0]
    assert body["path"] == ".review/rules.md"
    _validator_for("get", "/api/repos/{repo_id}/rules-status", "200").validate(body)


@pytest.mark.parametrize(
    ("variant", "change"),
    [
        ("present", {"status": "missing"}),
        ("missing", {"defaultsVersion": None}),
        ("invalid", {"diagnostics": []}),
        ("unavailable", {"diagnostics": [{**_TRANSIENT.__dict__}]}),
        ("unavailable", {"sha": "A" * 40}),
        ("present", {"sha": "a" * 64}),
        ("missing", {"sha": "a" * 39}),
        ("pending", {"checkedAt": "2026-10-18T09:00:00Z"}),
    ],
)
def test_rules_status_validation_rejects_drift(variant: str, change: dict[str, Any]) -> None:
    store = ContractRulesStatus(RULES_STATUSES[variant])
    app.dependency_overrides[get_rules_status] = lambda: GetRulesStatus(
        lambda: store, now=lambda: CHECKED_AT
    )
    try:
        body = authenticated_test_client(app).get(f"/api/repos/{REPOSITORY_ID}/rules-status")
    finally:
        app.dependency_overrides.clear()

    with pytest.raises(ValidationError):
        _validator_for("get", "/api/repos/{repo_id}/rules-status", "200").validate(
            {**body.json(), **change}
        )


@pytest.mark.parametrize("sha", ["a" * 64, "A" * 40, "a" * 39], ids=["sha-256", "upper", "short"])
@pytest.mark.parametrize("variant", ["present", "missing", "invalid"])
def test_the_rules_status_dto_takes_a_40_hex_sha_only(variant: str, sha: str) -> None:
    stored = RULES_STATUSES[variant]
    assert stored is not None

    with pytest.raises(PydanticValidationError, match="sha"):
        to_rules_status_dto(replace(stored, sha=sha))


def test_diagnostic_omits_the_limits_it_does_not_know() -> None:
    store = ContractRulesStatus(RULES_STATUSES["invalid"])
    app.dependency_overrides[get_rules_status] = lambda: GetRulesStatus(
        lambda: store, now=lambda: CHECKED_AT
    )
    try:
        body = authenticated_test_client(app).get(f"/api/repos/{REPOSITORY_ID}/rules-status")
    finally:
        app.dependency_overrides.clear()

    assert body.json()["diagnostics"] == [
        {
            "code": "policy_ignore_pattern_invalid",
            "message": "Mask.",
            "line": 5,
            "column": 6,
            "retryable": False,
            "patternIndex": 0,
        }
    ]


def test_diagnostic_serves_the_offending_mask_when_it_has_one() -> None:
    stored = RULES_STATUSES["invalid"]
    assert stored is not None
    (diagnostic,) = stored.diagnostics
    store = ContractRulesStatus(
        replace(stored, diagnostics=(replace(diagnostic, excerpt="src/a?.py"),))
    )
    app.dependency_overrides[get_rules_status] = lambda: GetRulesStatus(
        lambda: store, now=lambda: CHECKED_AT
    )
    try:
        body = authenticated_test_client(app).get(f"/api/repos/{REPOSITORY_ID}/rules-status")
    finally:
        app.dependency_overrides.clear()

    assert body.json()["diagnostics"][0]["excerpt"] == "src/a?.py"
    _validator_for("get", "/api/repos/{repo_id}/rules-status", "200").validate(body.json())


def test_rules_operations_are_replaced_and_the_status_is_not_shared_with_zod() -> None:
    operations = _operations(_spec()["paths"])
    for key in (
        ("get", "/api/repos/{}/rules"),
        ("post", "/api/repos/{}/rules"),
        ("post", "/api/rules/{}/preview"),
    ):
        assert operations[key]["x-status"] == "planned"
        assert "Replaced" in operations[key]["x-note"]
        assert "§10" in operations[key]["x-note"]
    assert "x-status" not in operations[("get", "/api/repos/{}/rules-status")]
    assert "RulesStatus" not in UI_ZOD_COMPONENTS.values()


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


def test_stream_declares_run_step_beside_the_status_event() -> None:
    operation = _spec()["paths"]["/api/stream"]["get"]
    content = operation["responses"]["200"]["content"]["text/event-stream"]
    events = content["x-events"]

    assert set(events) == {"run.updated", "run.step"}
    assert events["run.step"] == {"$ref": "#/components/schemas/RunStepEvent"}

    description = operation["description"]
    assert "`run.updated` is the status event" in description
    assert "`run.step` is not replayed and has no `id:` line" in description

    app_description = app.openapi()["paths"]["/api/stream"]["get"]["description"]
    assert "Each event carries an id" not in app_description
    assert "Only `run.updated` is id-addressed and replayed" in app_description
    assert "`run.step` has no id and is not replayed" in app_description

    media_type = _escape("text/event-stream")
    pointer = (
        f"/paths/{_escape('/api/stream')}/get/responses/200/content/{media_type}/x-events/run.step"
    )
    step: dict[str, Any] = {
        "runId": "11111111-1111-4111-8111-111111111111",
        "tool": "llm.call",
        "index": 0,
        "ok": True,
        "startedAt": "2026-10-12T09:00:00Z",
        "durationMs": 12,
        "attempt": 1,
        "llmOperation": "review",
    }
    _validator_at(pointer).validate(step)

    with pytest.raises(ValidationError):
        _validator_at(pointer).validate({**step, "status": "running"})
    without_tool = {key: value for key, value in step.items() if key != "tool"}
    with pytest.raises(ValidationError):
        _validator_at(pointer).validate(without_tool)
    without_attempt = {key: value for key, value in step.items() if key != "attempt"}
    with pytest.raises(ValidationError):
        _validator_at(pointer).validate(without_attempt)
    with pytest.raises(ValidationError):
        _validator_at(pointer).validate({**step, "attempt": 0})
    non_llm = {key: value for key, value in step.items() if key != "llmOperation"}
    _validator_at(pointer).validate({**non_llm, "tool": "context.build"})
    with pytest.raises(ValidationError):
        _validator_at(pointer).validate({**step, "llmOperation": "other"})


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
            if not rest:
                continue
            branch = {**branch, "type": rest[0] if len(rest) == 1 else rest}
        branches.append(branch)
    if not branches:
        return nullable, {"type": "null"}
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


@pytest.mark.parametrize(
    "component",
    [
        "BitbucketDiscoveryRequest",
        "BitbucketConnectRequest",
        "BitbucketCredentialRotateRequest",
    ],
)
def test_bitbucket_api_token_schema_accepts_only_printable_ascii_up_to_4096(
    component: str,
) -> None:
    token_schema = _components()[component]["properties"]["apiToken"]
    validator = Draft202012Validator(token_schema)

    assert token_schema["writeOnly"] is True
    assert validator.is_valid("A" * 4096)
    for invalid in ("", "A" * 4097, "A token", "A\n", "A\x7f", "Aé"):
        assert not validator.is_valid(invalid)


@pytest.mark.parametrize(
    ("zod_name", "component"),
    [
        ("bitbucketDiscoveryRequest", "BitbucketDiscoveryRequest"),
        ("bitbucketConnectRequest", "BitbucketConnectRequest"),
        ("bitbucketCredentialRotateRequest", "BitbucketCredentialRotateRequest"),
    ],
)
def test_bitbucket_api_token_patterns_match_the_printable_ascii_contract(
    zod_name: str, component: str
) -> None:
    spec_token = _components()[component]["properties"]["apiToken"]
    ui_token = _generated_schemas()[zod_name]["properties"]["apiToken"]
    values = [
        "",
        *(chr(codepoint) for codepoint in range(128)),
        "é",
        "Ａ",
        "A\n",
        "A A",
        "A" * 4096,
        "A" * 4097,
    ]

    for source, schema in (("OpenAPI", spec_token), ("UI Zod", ui_token)):
        assert schema["type"] == "string", source
        assert (schema["minLength"], schema["maxLength"]) == (1, 4096), source
        pattern = schema["pattern"]
        assert pattern.startswith("^"), source
        assert pattern.endswith(("$", r"(?![\s\S])")), source
        for value in values:
            expected = 1 <= len(value) <= 4096 and all(
                33 <= ord(character) <= 126 for character in value
            )
            actual = 1 <= len(value) <= 4096 and re.fullmatch(pattern, value) is not None
            assert actual == expected, (source, component, repr(value[:20]))


def test_contract_mirror_distinguishes_null_only_from_nullable_string() -> None:
    assert _contract_mismatches({"type": "null"}, {"type": "null"}, {}, "cursor") == []
    assert _contract_mismatches({"type": ["null"]}, {"type": "null"}, {}, "cursor") == []
    assert _contract_mismatches({"type": ["string", "null"]}, {"type": "null"}, {}, "cursor")
    assert _contract_mismatches({}, {"type": "null"}, {}, "cursor")
    assert _contract_mismatches({"type": "null"}, {}, {}, "cursor")


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
    assert components["RunTrigger"]["enum"] == list(get_args(RunTrigger.__value__))
    assert components["Severity"]["enum"] == list(get_args(Severity.__value__))
    assert components["Category"]["enum"] == list(get_args(Category.__value__))
    assert components["Severity"]["enum"] == zod["reviewComment"]["properties"]["severity"]["enum"]
    assert components["Category"]["enum"] == zod["reviewComment"]["properties"]["category"]["enum"]
    assert components["RagIndexStatus"]["enum"] == [
        "disabled",
        "empty",
        "building",
        "ready",
        "stale",
        "failed",
    ]


def test_runtime_run_dtos_declare_the_trigger_enum_of_the_spec() -> None:
    runtime = app.openapi()["components"]["schemas"]
    trigger_ref = {"$ref": "#/components/schemas/RunTrigger"}

    assert runtime["RunSessionDto"]["properties"]["trigger"] == trigger_ref
    assert runtime["RunDetailDto"]["properties"]["trigger"] == trigger_ref
    assert runtime["RunTrigger"]["enum"] == _components()["RunTrigger"]["enum"]


@pytest.mark.parametrize(
    "name",
    ["PullRequestDto", "PullRequestDetailDto", "RunSessionDto", "RunDetailDto"],
)
def test_runtime_provider_is_required_without_a_github_default(name: str) -> None:
    schema = app.openapi()["components"]["schemas"][name]
    assert "provider" in schema["required"]
    assert "default" not in schema["properties"]["provider"]


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("post", "/api/workspaces/{workspace_id}/bitbucket/discovery"),
        ("post", "/api/workspaces/{workspace_id}/bitbucket/connections"),
        (
            "put",
            "/api/workspaces/{workspace_id}/bitbucket/connections/{installation_id}/credential",
        ),
    ],
)
def test_bitbucket_error_contract_accepts_safe_application_and_request_validation(
    method: str, path: str
) -> None:
    operation = _spec()["paths"][path][method]
    _validator_for(method, path, "422").validate({"detail": "repository_inaccessible"})
    _validator_for(method, path, "422").validate(
        {"detail": [{"loc": ["body", "apiToken"], "msg": "Invalid value", "type": "value_error"}]}
    )
    assert operation["responses"]["429"] == {"$ref": "#/components/responses/BitbucketRateLimited"}
    assert "Retry-After" in _spec()["components"]["responses"]["BitbucketRateLimited"]["headers"]


_BITBUCKET_WORKSPACE = UUID(int=333)
_BITBUCKET_INSTALLATION = UUID(int=334)
_BITBUCKET_REPOSITORY = UUID(int=335)
_BITBUCKET_PREFIX = f"/api/workspaces/{_BITBUCKET_WORKSPACE}/bitbucket"
_BITBUCKET_CONNECTIONS = "/api/workspaces/{workspace_id}/bitbucket/connections"
_BITBUCKET_CREDENTIAL = f"{_BITBUCKET_CONNECTIONS}/{{installation_id}}/credential"
_BITBUCKET_REPOSITORY_PATH = (
    f"{_BITBUCKET_CONNECTIONS}/{{installation_id}}/repositories/{{repository_id}}"
)


class _BitbucketContractManager:
    def webhook_url(self, installation_id: UUID, repository_id: UUID) -> str:
        return f"https://example.test/webhooks/bitbucket/{installation_id}/{repository_id}"

    async def discover(self, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(
            repositories=(
                SimpleNamespace(
                    uuid=_BITBUCKET_REPOSITORY,
                    workspace_slug="team",
                    slug="repo",
                    name="Repo",
                    is_private=True,
                    html_url="https://bitbucket.org/team/repo",
                ),
            ),
            next_cursor=None,
        )

    async def list_connections(self, **_kwargs: object) -> tuple[RepositoryBinding, ...]:
        return (
            RepositoryBinding(
                installation_id=_BITBUCKET_INSTALLATION,
                repository_id=_BITBUCKET_REPOSITORY,
                repository_uuid=_BITBUCKET_REPOSITORY,
                full_name="team/repo",
                web_url="https://bitbucket.org/team/repo",
                credential_generation=1,
                binding_generation=1,
                confirmed_at=datetime(2026, 10, 1, tzinfo=UTC),
            ),
        )

    async def connect(self, **_kwargs: object) -> ConnectResult:
        return ConnectResult(
            installation_id=_BITBUCKET_INSTALLATION,
            repository_id=_BITBUCKET_REPOSITORY,
            repository_uuid=_BITBUCKET_REPOSITORY,
            workspace_slug="team",
            repository_slug="repo",
            credential_generation=1,
            webhook_secret="one-time-secret",
            confirmed=False,
        )

    async def rotate_token(self, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(installation_id=_BITBUCKET_INSTALLATION, generation=2)

    async def disconnect(self, **_kwargs: object) -> None:
        return None

    async def rotate_webhook_secret(self, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(
            url=self.webhook_url(_BITBUCKET_INSTALLATION, _BITBUCKET_REPOSITORY),
            secret="new-one-time-secret",
        )


@pytest.fixture
def bitbucket_contract_client() -> Iterator[TestClient]:
    app.dependency_overrides[get_auth_scope] = lambda: AuthScope(42, (_BITBUCKET_WORKSPACE,))
    app.dependency_overrides[get_bitbucket_workspace_scope] = lambda: AuthScope(
        42, (_BITBUCKET_WORKSPACE,)
    )
    app.dependency_overrides[get_bitbucket_management] = _BitbucketContractManager
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


@pytest.mark.parametrize(
    ("method", "path", "suffix", "body", "status"),
    [
        (
            "post",
            f"{_BITBUCKET_PREFIX}/discovery",
            "/discovery",
            {"bitbucketWorkspace": "team", "apiToken": "token"},
            "200",
        ),
        ("get", _BITBUCKET_CONNECTIONS, "/connections", None, "200"),
        (
            "post",
            _BITBUCKET_CONNECTIONS,
            "/connections",
            {
                "bitbucketWorkspace": "team",
                "repositorySlug": "repo",
                "repositoryUuid": str(_BITBUCKET_REPOSITORY),
                "apiToken": "token",
            },
            "201",
        ),
        (
            "put",
            _BITBUCKET_CREDENTIAL,
            f"/connections/{_BITBUCKET_INSTALLATION}/credential",
            {"apiToken": "token"},
            "200",
        ),
        (
            "delete",
            _BITBUCKET_REPOSITORY_PATH,
            f"/connections/{_BITBUCKET_INSTALLATION}/repositories/{_BITBUCKET_REPOSITORY}",
            None,
            "204",
        ),
        (
            "post",
            f"{_BITBUCKET_REPOSITORY_PATH}/webhook-secret",
            f"/connections/{_BITBUCKET_INSTALLATION}/repositories/{_BITBUCKET_REPOSITORY}/webhook-secret",
            None,
            "200",
        ),
    ],
)
def test_bitbucket_operation_response_matches_spec(
    bitbucket_contract_client: TestClient,
    method: str,
    path: str,
    suffix: str,
    body: dict[str, str] | None,
    status: str,
) -> None:
    spec_path = path.replace(str(_BITBUCKET_WORKSPACE), "{workspace_id}")
    response = bitbucket_contract_client.request(
        method.upper(), f"{_BITBUCKET_PREFIX}{suffix}", json=body
    )
    assert response.status_code == int(status)
    if status == "204":
        assert response.content == b""
        assert "content" not in _spec()["paths"][spec_path][method]["responses"][status]
        return
    payload = response.json()
    _validator_for(method, spec_path, status).validate(payload)
    if method == "post" and suffix == "/connections":
        with pytest.raises(ValidationError):
            _validator_for(method, spec_path, status).validate(
                {key: value for key, value in payload.items() if key != "webhook"}
            )
        with pytest.raises(ValidationError):
            _validator_for(method, spec_path, status).validate({**payload, "status": "bogus"})


def test_bitbucket_connect_and_secret_rotation_return_all_five_webhook_events(
    bitbucket_contract_client: TestClient,
) -> None:
    events = [
        "repo:push",
        "pullrequest:created",
        "pullrequest:updated",
        "pullrequest:fulfilled",
        "pullrequest:rejected",
    ]
    connected = bitbucket_contract_client.post(
        _BITBUCKET_CONNECTIONS.replace("{workspace_id}", str(_BITBUCKET_WORKSPACE)),
        json={
            "bitbucketWorkspace": "team",
            "repositorySlug": "repo",
            "repositoryUuid": str(_BITBUCKET_REPOSITORY),
            "apiToken": "token",
        },
    )
    rotated = bitbucket_contract_client.post(
        f"{_BITBUCKET_PREFIX}/connections/{_BITBUCKET_INSTALLATION}"
        f"/repositories/{_BITBUCKET_REPOSITORY}/webhook-secret"
    )

    assert connected.status_code == 201
    assert rotated.status_code == 200
    assert connected.json()["webhook"]["events"] == events
    assert rotated.json()["events"] == events
    assert _components()["BitbucketWebhookSetup"]["properties"]["events"]["items"]["enum"] == events


@pytest.mark.parametrize(
    ("method", "path", "suffix", "body"),
    [
        (
            "post",
            "/api/workspaces/{workspace_id}/bitbucket/discovery",
            "/discovery",
            {"bitbucketWorkspace": "team", "apiToken": "token"},
        ),
        ("get", _BITBUCKET_CONNECTIONS, "/connections", None),
        (
            "post",
            _BITBUCKET_CONNECTIONS,
            "/connections",
            {
                "bitbucketWorkspace": "team",
                "repositorySlug": "repo",
                "repositoryUuid": str(_BITBUCKET_REPOSITORY),
                "apiToken": "token",
            },
        ),
        (
            "put",
            _BITBUCKET_CREDENTIAL,
            f"/connections/{_BITBUCKET_INSTALLATION}/credential",
            {"apiToken": "token"},
        ),
        (
            "delete",
            _BITBUCKET_REPOSITORY_PATH,
            f"/connections/{_BITBUCKET_INSTALLATION}/repositories/{_BITBUCKET_REPOSITORY}",
            None,
        ),
        (
            "post",
            f"{_BITBUCKET_REPOSITORY_PATH}/webhook-secret",
            f"/connections/{_BITBUCKET_INSTALLATION}/repositories/{_BITBUCKET_REPOSITORY}/webhook-secret",
            None,
        ),
    ],
)
def test_bitbucket_operation_not_found_body_matches_spec(
    bitbucket_contract_client: TestClient,
    method: str,
    path: str,
    suffix: str,
    body: dict[str, str] | None,
) -> None:
    class MissingManager:
        def __getattr__(self, _name: str) -> Any:
            async def missing(**_kwargs: object) -> None:
                raise BitbucketManagementError("workspace_not_found")

            return missing

    app.dependency_overrides[get_bitbucket_management] = MissingManager
    response = bitbucket_contract_client.request(
        method.upper(), f"{_BITBUCKET_PREFIX}{suffix}", json=body
    )
    assert response.status_code == 404
    _validator_for(method, path, "404").validate(response.json())


@pytest.mark.parametrize(
    "code,status",
    [
        ("invalid_credential", "400"),
        ("repository_inaccessible", "422"),
        ("provider_rate_limited", "429"),
    ],
)
def test_bitbucket_provider_error_body_matches_spec(
    bitbucket_contract_client: TestClient, code: str, status: str
) -> None:
    class FailingManager(_BitbucketContractManager):
        async def discover(self, **_kwargs: object) -> SimpleNamespace:
            raise BitbucketProviderError(code, retry_after=37 if status == "429" else None)

    app.dependency_overrides[get_bitbucket_management] = FailingManager
    response = bitbucket_contract_client.post(
        f"{_BITBUCKET_PREFIX}/discovery", json={"bitbucketWorkspace": "team", "apiToken": "token"}
    )
    assert response.status_code == int(status)
    _validator_for("post", "/api/workspaces/{workspace_id}/bitbucket/discovery", status).validate(
        response.json()
    )
    if status == "429":
        assert response.headers["Retry-After"] == "37"


def test_bitbucket_rotate_invalid_credential_response_matches_spec(
    bitbucket_contract_client: TestClient,
) -> None:
    class FailingManager(_BitbucketContractManager):
        async def rotate_token(self, **_kwargs: object) -> SimpleNamespace:
            raise BitbucketManagementError("invalid_credential")

    app.dependency_overrides[get_bitbucket_management] = FailingManager
    response = bitbucket_contract_client.put(
        f"{_BITBUCKET_PREFIX}/connections/{_BITBUCKET_INSTALLATION}/credential",
        json={"apiToken": "token"},
    )
    assert response.status_code == 400
    _validator_for("put", _BITBUCKET_CREDENTIAL, "400").validate(response.json())


@pytest.mark.parametrize(
    ("method", "path", "suffix", "body"),
    [
        (
            "post",
            "/api/workspaces/{workspace_id}/bitbucket/discovery",
            "/discovery",
            {"bitbucketWorkspace": "team", "apiToken": "bad token"},
        ),
        (
            "post",
            _BITBUCKET_CONNECTIONS,
            "/connections",
            {
                "bitbucketWorkspace": "team",
                "repositorySlug": "repo",
                "repositoryUuid": str(_BITBUCKET_REPOSITORY),
                "apiToken": "bad token",
            },
        ),
        (
            "put",
            _BITBUCKET_CREDENTIAL,
            f"/connections/{_BITBUCKET_INSTALLATION}/credential",
            {"apiToken": "bad token"},
        ),
    ],
)
def test_bitbucket_request_validation_response_matches_spec_without_token_echo(
    bitbucket_contract_client: TestClient,
    method: str,
    path: str,
    suffix: str,
    body: dict[str, str],
) -> None:
    response = bitbucket_contract_client.request(
        method.upper(), f"{_BITBUCKET_PREFIX}{suffix}", json=body
    )
    assert response.status_code == 422
    assert body["apiToken"] not in response.text
    _validator_for(method, path, "422").validate(response.json())


@pytest.mark.parametrize(
    ("method", "path", "suffix"),
    [
        ("get", _BITBUCKET_CONNECTIONS, "/connections"),
        ("delete", _BITBUCKET_REPOSITORY_PATH, "/connections/not-a-uuid/repositories/not-a-uuid"),
        (
            "post",
            f"{_BITBUCKET_REPOSITORY_PATH}/webhook-secret",
            "/connections/not-a-uuid/repositories/not-a-uuid/webhook-secret",
        ),
    ],
)
def test_bitbucket_malformed_uuid_response_matches_spec(
    bitbucket_contract_client: TestClient, method: str, path: str, suffix: str
) -> None:
    if method == "get":
        url = "/api/workspaces/not-a-uuid/bitbucket/connections"
    else:
        url = f"{_BITBUCKET_PREFIX}{suffix}"
    response = bitbucket_contract_client.request(method.upper(), url)
    assert response.status_code == 422
    _validator_for(method, path, "422").validate(response.json())


@pytest.mark.parametrize(
    ("code", "status"),
    [
        ("bitbucket_connection_inactive", "409"),
        ("credential_unavailable", "503"),
    ],
)
def test_bitbucket_webhook_secret_error_body_matches_spec(
    bitbucket_contract_client: TestClient, code: str, status: str
) -> None:
    class FailingManager(_BitbucketContractManager):
        async def rotate_webhook_secret(self, **_kwargs: object) -> SimpleNamespace:
            raise BitbucketManagementError(code)

    app.dependency_overrides[get_bitbucket_management] = FailingManager
    path = f"{_BITBUCKET_REPOSITORY_PATH}/webhook-secret"
    response = bitbucket_contract_client.post(
        f"{_BITBUCKET_PREFIX}/connections/{_BITBUCKET_INSTALLATION}"
        f"/repositories/{_BITBUCKET_REPOSITORY}/webhook-secret"
    )
    assert response.status_code == int(status)
    _validator_for("post", path, status).validate(response.json())


def test_bitbucket_provider_failure_body_matches_spec(
    bitbucket_contract_client: TestClient,
) -> None:
    class FailingManager(_BitbucketContractManager):
        async def discover(self, **_kwargs: object) -> SimpleNamespace:
            raise BitbucketProviderError("provider_unavailable")

    app.dependency_overrides[get_bitbucket_management] = FailingManager
    response = bitbucket_contract_client.post(
        f"{_BITBUCKET_PREFIX}/discovery", json={"bitbucketWorkspace": "team", "apiToken": "token"}
    )
    assert response.status_code == 502
    _validator_for("post", "/api/workspaces/{workspace_id}/bitbucket/discovery", "502").validate(
        response.json()
    )


@pytest.mark.parametrize("field", ["workspaceSlug", "repositorySlug"])
def test_bitbucket_response_slug_bounds_match_runtime_and_contract(
    bitbucket_contract_client: TestClient, field: str
) -> None:
    response = bitbucket_contract_client.post(
        _BITBUCKET_CONNECTIONS.replace("{workspace_id}", str(_BITBUCKET_WORKSPACE)),
        json={
            "bitbucketWorkspace": "team",
            "repositorySlug": "repo",
            "repositoryUuid": str(_BITBUCKET_REPOSITORY),
            "apiToken": "token",
        },
    )
    assert response.status_code == 201
    bad = {**response.json(), field: "x" * 129}
    with pytest.raises(PydanticValidationError):
        ConnectResponse.model_validate(bad)
    with pytest.raises(ValidationError):
        _validator_at("/components/schemas/BitbucketConnectResponse").validate(bad)

    listed = bitbucket_contract_client.get(
        _BITBUCKET_CONNECTIONS.replace("{workspace_id}", str(_BITBUCKET_WORKSPACE))
    )
    assert listed.status_code == 200
    bad_item = {**listed.json()["connections"][0], field: "x" * 129}
    with pytest.raises(PydanticValidationError):
        ConnectionItem.model_validate(bad_item)
    with pytest.raises(ValidationError):
        _validator_at("/components/schemas/BitbucketConnectionItem").validate(bad_item)


def _string_constant(node: ast.expr | None) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _written_tools(source: str) -> set[str]:
    """Collect the string-constant tool names from the shapes that write run_actions rows.

    The scanned shapes: a module-level ``NAME_TOOL = "..."`` assignment, the third positional
    argument of a ``traced_step(...)`` call, the second positional argument of a ``record(...)``
    call (the ``RunTrace.record(run_id, tool, ...)`` sink) and a ``tool=`` keyword of any call.
    Only string constants count; a name or an attribute in these positions is skipped. A
    ``tool=`` keyword and a literal second argument of ``record(...)`` are counted on any call,
    whatever the receiver, so a ``tool="x.y"`` or a ``ledger.record(context, "x.y")`` that does not
    write run_actions surfaces as unexpected and must be renamed or the scan taught to skip it. A
    blanket ``"x.y"`` match would also pick up prompt names such as ``review.system``, which are
    not tools.
    """
    tree = ast.parse(source)
    found: list[str | None] = []
    targets: list[ast.expr]
    value: ast.expr | None
    for statement in tree.body:
        if isinstance(statement, ast.Assign):
            targets, value = statement.targets, statement.value
        elif isinstance(statement, ast.AnnAssign):
            targets, value = [statement.target], statement.value
        else:
            continue
        if any(isinstance(target, ast.Name) and target.id.endswith("_TOOL") for target in targets):
            found.append(_string_constant(value))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        callee = func.id if isinstance(func, ast.Name) else None
        callee = func.attr if isinstance(func, ast.Attribute) else callee
        if callee == "traced_step" and len(node.args) >= 3:
            found.append(_string_constant(node.args[2]))
        if callee == "record" and len(node.args) >= 2:
            found.append(_string_constant(node.args[1]))
        found += [
            _string_constant(keyword.value) for keyword in node.keywords if keyword.arg == "tool"
        ]
    return {tool for tool in found if tool is not None}


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ('traced_step(trace, run_id, "a.b", {})', {"a.b"}),
        ('trace.record(run_id, "x.y", {}, None, now, 0)', {"x.y"}),
        ("ledger.record(context, usage)", set()),
        ("self._trace.record(pointer.run_id, route.tool, {})", set()),
        ('Route(tool="c.d")', {"c.d"}),
        ('LLM_CALL_TOOL = "e.f"', {"e.f"}),
        ('X_TOOL: Final = "g.h"', {"g.h"}),
        ('ledger.record(context, "x.y")', {"x.y"}),
        ('def record(self, run_id, tool="x.y"): ...', set()),
        ('prompts.load("review.system")', set()),
    ],
    ids=[
        "traced_step-third-argument",
        "record-second-argument",
        "record-without-tool-argument",
        "record-attribute-argument",
        "tool-keyword",
        "module-constant",
        "module-annotated-constant",
        "record-any-receiver",
        "record-definition",
        "plain-string-literal",
    ],
)
def test_written_tools_scan_picks_up_only_the_run_action_shapes(
    source: str, expected: set[str]
) -> None:
    assert _written_tools(source) == expected


def _catalog_drift(side: str, found: set[str]) -> str:
    missing, unexpected = sorted(RUN_ACTION_TOOLS - found), sorted(found - RUN_ACTION_TOOLS)
    return f"{side} vs catalog: missing {missing}, unexpected {unexpected}"


def test_run_action_tool_catalog_matches_the_tools_the_code_writes() -> None:
    written: set[str] = set()
    for path in sorted(APP_PATH.rglob("*.py")):
        written |= _written_tools(path.read_text(encoding="utf-8"))

    assert written == RUN_ACTION_TOOLS, _catalog_drift("code", written)


@pytest.mark.parametrize(
    "location", [("RunAction", "properties", "tool"), ("RunStatus",)], ids=".".join
)
def test_run_action_tool_descriptions_list_the_catalog(location: tuple[str, ...]) -> None:
    schema: Mapping[str, Any] = _components()
    for key in location:
        schema = schema[key]

    listed = set(BACKTICKED_TOOL.findall(schema["description"]))

    assert listed == RUN_ACTION_TOOLS, _catalog_drift(f"{'.'.join(location)} description", listed)


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


def test_repository_review_settings_round_trip_and_clear(client: TestClient) -> None:
    url = f"/api/repos/{REPOSITORY_ID}"
    defaults = client.get(url).json()
    patched = client.patch(
        url, json={"branchFilter": ["main", "release/**"], "severityThreshold": "critical"}
    )
    stored = client.get(url).json()
    twenty = client.patch(url, json={"branchFilter": [f"release/{n}" for n in range(20)]})
    cleared = client.patch(url, json={"branchFilter": []})

    assert (
        defaults["branchFilter"],
        defaults["severityThreshold"],
        defaults["installationAvailable"],
    ) == ([], "all", True)
    assert patched.status_code == 200
    _validator_for("patch", "/api/repos/{repo_id}", "200").validate(patched.json())
    assert (stored["branchFilter"], stored["severityThreshold"]) == (
        ["main", "release/**"],
        "critical",
    )
    assert twenty.status_code == 200 and len(twenty.json()["branchFilter"]) == 20
    assert cleared.status_code == 200
    assert (cleared.json()["branchFilter"], cleared.json()["severityThreshold"]) == ([], "critical")


def test_repository_rag_toggle_round_trip(client: TestClient) -> None:
    url = f"/api/repos/{REPOSITORY_ID}"
    disabled = {
        "status": "disabled",
        "indexedCommitSha": None,
        "indexedAt": None,
        "fileCount": 0,
        "symbolCount": 0,
        "lastErrorCode": None,
    }
    defaults = client.get(url).json()
    listed = client.get("/api/repos").json()
    enabled = client.patch(url, json={"ragEnabled": True})
    stored = client.get(url).json()
    disabled_again = client.patch(url, json={"ragEnabled": False})

    assert (defaults["ragEnabled"], defaults["ragIndex"]) == (False, disabled)
    assert (listed[0]["ragEnabled"], listed[0]["ragIndex"]) == (False, disabled)
    assert enabled.status_code == 200
    _validator_for("patch", "/api/repos/{repo_id}", "200").validate(enabled.json())
    assert (enabled.json()["ragEnabled"], enabled.json()["ragIndex"]) == (
        True,
        {**disabled, "status": "empty"},
    )
    assert (stored["ragEnabled"], stored["ragIndex"]["status"]) == (True, "empty")
    assert disabled_again.status_code == 200
    assert (disabled_again.json()["ragEnabled"], disabled_again.json()["ragIndex"]) == (
        False,
        disabled,
    )


def test_published_rag_index_validates_against_the_spec_and_the_ui_zod_schema(
    client: TestClient,
) -> None:
    repositories = ContractRepositories()
    repositories.item = replace(
        REPOSITORY,
        rag_enabled=True,
        rag_index=RagIndex(
            RagIndexStatus.STALE,
            "a" * 40,
            # The same instant as a database session outside UTC returns it.
            datetime(2026, 10, 16, 11, 30, tzinfo=timezone(timedelta(hours=2))),
            12,
            340,
            "github_rate_limited",
        ),
    )
    app.dependency_overrides[get_repository_settings] = lambda: lambda: repositories

    response = client.get(f"/api/repos/{REPOSITORY_ID}")

    assert response.status_code == 200
    assert response.json()["ragIndex"] == {
        "status": "stale",
        "indexedCommitSha": "a" * 40,
        "indexedAt": "2026-10-16T09:30:00Z",
        "fileCount": 12,
        "symbolCount": 340,
        "lastErrorCode": "github_rate_limited",
    }
    _validator_for("get", "/api/repos/{repo_id}", "200").validate(response.json())
    Draft202012Validator(_generated_schemas()["repository"]).validate(response.json())


@pytest.mark.parametrize(
    ("name", "body"), INVALID_REVIEW_SETTINGS.items(), ids=INVALID_REVIEW_SETTINGS.keys()
)
def test_repository_update_rejects_invalid_review_settings(
    client: TestClient, name: str, body: dict[str, object]
) -> None:
    url = f"/api/repos/{REPOSITORY_ID}"

    # Escaped JSON text: httpx's ``json=`` cannot UTF-8-encode a lone surrogate (#149).
    rejected = client.patch(
        url, content=json.dumps(body), headers={"Content-Type": "application/json"}
    )
    stored = client.get(url).json()

    assert rejected.status_code == 422
    if name == "branch-filter-lone-surrogate":
        # The ASCII-escaped fallback body keeps FastAPI's default shape (app/main.py).
        (detail,) = rejected.json()["detail"]
        assert detail["loc"] == ["body", "branchFilter"]
        assert detail["msg"] != "Invalid input"
        assert detail["input"] == body["branchFilter"]
    assert (stored["branchFilter"], stored["severityThreshold"]) == ([], "all")
    assert (stored["ragEnabled"], stored["ragIndex"]["status"]) == (False, "disabled")


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


def test_changed_rerun_origin_uses_existing_conflict_wire_contract(client: TestClient) -> None:
    response = client.post(f"/api/runs/{ORIGIN_CHANGED_RUN_ID}/rerun")
    assert response.status_code == 409
    assert response.json() == {
        "detail": "the pull request has an active run or is closed, "
        "or repository reviews are paused or unavailable"
    }
    _validator_for("post", "/api/runs/{run_id}/rerun", "409").validate(response.json())
