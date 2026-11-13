"""Shared fixtures and OpenAPI validators for contract tests."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime
from functools import cache
from pathlib import Path
from typing import Any, Self
from uuid import UUID

from jsonschema.protocols import Validator
from openapi_schema_validator import OAS31Validator
from openapi_spec_validator.readers import read_from_filename
from referencing import Registry
from referencing.jsonschema import DRAFT202012

from app.modules.repositories.application.rules_status import (
    RulesDiagnostic,
    RulesStatus,
    RulesStatusKind,
    StoredRulesStatus,
)

SPEC_PATH = Path(__file__).parents[1] / "contracts" / "openapi.yaml"
APP_PATH = Path(__file__).parents[1] / "app"
HTTP_METHODS = frozenset({"get", "put", "post", "delete", "options", "head", "patch", "trace"})
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
    "metricsSummary": "MetricsSummary",
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
