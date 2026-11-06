"""Validate repository rules-status responses against the OpenAPI contract."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest
from jsonschema.exceptions import ValidationError
from pydantic import ValidationError as PydanticValidationError

from app.bootstrap.reviews_api import get_rules_status
from app.main import app, to_rules_status_dto
from app.modules.repositories.application.rules_status import GetRulesStatus
from tests.openapi_contract import (
    _TRANSIENT,
    CHECKED_AT,
    REPOSITORY_ID,
    RULES_STATUSES,
    UI_ZOD_COMPONENTS,
    ContractRulesStatus,
    _operations,
    _spec,
    _validator_for,
)
from tests.portal_test_client import authenticated_test_client


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


def test_rules_status_discriminator_maps_each_status_to_its_const_schema() -> None:
    schemas = _spec()["components"]["schemas"]
    mapping = schemas["RulesStatus"]["discriminator"]["mapping"]

    for status, reference in mapping.items():
        schema_name = reference.removeprefix("#/components/schemas/")
        assert reference == f"#/components/schemas/{schema_name}"
        assert schemas[schema_name]["properties"]["status"]["const"] == status


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
        ("invalid", {"sha": "a" * 64}),
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
@pytest.mark.parametrize("variant", ["present", "missing", "invalid", "unavailable"])
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
