"""Behavioural contracts for repository default-rule onboarding."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import pytest

from app.modules.repositories.application.onboard_repository import (
    OnboardRepository,
    PersistedRuleVersion,
    RuleSetValidationError,
    load_default_rule_sets,
    select_default_rule_set,
)
from app.modules.repositories.infrastructure.models import RuleVersion
from app.modules.repositories.infrastructure.rule_version_repository import (
    SqlAlchemyRepositoryRuleVersionStore,
)


def test_load_default_rule_sets_reads_backend_and_frontend_assets() -> None:
    rule_sets = load_default_rule_sets(Path("review/rules"))

    assert set(rule_sets) == {"backend", "frontend"}
    assert rule_sets["backend"].version == 1
    assert rule_sets["frontend"].version == 1
    assert rule_sets["backend"].rules[0]["name"] == "Naming Consistency"


@pytest.mark.parametrize(
    "payload",
    [
        {"version": 1, "stack": "backend", "rules": [], "unknown": True},
        {
            "version": 1,
            "stack": "backend",
            "rules": [
                {
                    "name": "One",
                    "include": ["app/**/*.py"],
                    "exclude": [],
                    "checks": ["Check it."],
                    "unknown": True,
                }
            ],
        },
    ],
    ids=["top-level-additional-property", "rule-additional-property"],
)
def test_load_default_rule_sets_rejects_schema_violations(
    tmp_path: Path, payload: dict[str, object]
) -> None:
    rules_dir = tmp_path / "rules"
    rules_dir.mkdir()
    (rules_dir / "schema.json").write_text(
        Path("review/rules/schema.json").read_text(encoding="utf-8"), encoding="utf-8"
    )
    (rules_dir / "default-backend.v1.json").write_text(json.dumps(payload), encoding="utf-8")
    (rules_dir / "default-frontend.v1.json").write_text(
        Path("review/rules/default-frontend.v1.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    with pytest.raises(RuleSetValidationError, match="unexpected"):
        load_default_rule_sets(rules_dir)


def test_load_default_rule_sets_honors_draft_202012_composition_keywords(tmp_path: Path) -> None:
    rules_dir = tmp_path / "rules"
    rules_dir.mkdir()
    schema = json.loads(Path("review/rules/schema.json").read_text(encoding="utf-8"))
    schema["$defs"]["Rule"]["allOf"] = [
        {
            "not": {
                "properties": {"name": {"const": "Forbidden"}},
                "required": ["name"],
            }
        }
    ]
    (rules_dir / "schema.json").write_text(json.dumps(schema), encoding="utf-8")
    backend = json.loads(Path("review/rules/default-backend.v1.json").read_text(encoding="utf-8"))
    backend["rules"][0]["name"] = "Forbidden"
    (rules_dir / "default-backend.v1.json").write_text(json.dumps(backend), encoding="utf-8")
    (rules_dir / "default-frontend.v1.json").write_text(
        Path("review/rules/default-frontend.v1.json").read_text(encoding="utf-8"), encoding="utf-8"
    )

    with pytest.raises(RuleSetValidationError, match="Forbidden"):
        load_default_rule_sets(rules_dir)


@pytest.mark.parametrize(
    ("languages", "stack"),
    [
        ({"Python": 100}, "backend"),
        ({"TypeScript": 100}, "frontend"),
        ({"Python": 50, "TypeScript": 50}, "backend"),
        ({"Markdown": 100}, "backend"),
        ({}, "backend"),
    ],
)
def test_default_stack_selection_is_deterministic(languages: dict[str, int], stack: str) -> None:
    rule_sets = load_default_rule_sets(Path("review/rules"))

    selected = select_default_rule_set(rule_sets, languages)

    assert selected.stack == stack


@dataclass
class FakeOnboardingRepository:
    existing: PersistedRuleVersion | None = None
    create_calls: int = 0

    async def get_active_rule_version(self, repository_id: UUID) -> PersistedRuleVersion | None:
        return self.existing

    async def get_or_create_initial_rule_version(
        self, repository_id: UUID, version: int, rules: list[dict[str, object]]
    ) -> PersistedRuleVersion:
        self.create_calls += 1
        if self.existing is None:
            self.existing = PersistedRuleVersion(
                repository_id=repository_id,
                version=version,
                rules=rules,
                checksum=sha256(
                    json.dumps(rules, sort_keys=True, separators=(",", ":")).encode("utf-8")
                ).hexdigest(),
            )
        return self.existing


def test_onboarding_creates_one_unchanged_active_default_rule_version() -> None:
    repository_id = uuid4()
    repository = FakeOnboardingRepository()
    rule_sets = load_default_rule_sets(Path("review/rules"))

    result = asyncio.run(
        OnboardRepository(repository, rule_sets).execute(repository_id, {"TypeScript": 99})
    )

    assert result.created is True
    assert result.rule_version.rules == rule_sets["frontend"].rules
    assert (
        result.rule_version.checksum
        == sha256(
            json.dumps(result.rule_version.rules, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
        ).hexdigest()
    )
    assert repository.create_calls == 1


def test_onboarding_retry_keeps_the_existing_active_version_unchanged() -> None:
    repository_id = uuid4()
    repository = FakeOnboardingRepository()
    rule_sets = load_default_rule_sets(Path("review/rules"))
    onboarding = OnboardRepository(repository, rule_sets)

    first = asyncio.run(onboarding.execute(repository_id, {"Python": 99}))
    second = asyncio.run(onboarding.execute(repository_id, {"TypeScript": 99}))

    assert first.created is True
    assert second.created is False
    assert second.rule_version == first.rule_version
    assert repository.create_calls == 1


class FakeRuleVersionSession:
    def __init__(self) -> None:
        self.row: object | None = None
        self.flushes = 0

    async def scalar(self, statement: object) -> object | None:
        return self.row

    async def execute(self, statement: object) -> None:
        params = cast(dict[str, object], cast(Any, statement).compile().params)
        self.row = RuleVersion.from_rules(
            repository_id=cast(UUID, params["repository_id"]),
            version=cast(int, params["version"]),
            rules=cast(list[dict[str, object]], params["rules"]),
        )
        self.flushes += 1


def test_sqlalchemy_store_upserts_the_factory_built_initial_version() -> None:
    repository_id = uuid4()
    session = FakeRuleVersionSession()
    store = SqlAlchemyRepositoryRuleVersionStore(session)  # type: ignore[arg-type]
    rules = load_default_rule_sets(Path("review/rules"))["backend"].rules

    result = asyncio.run(store.get_or_create_initial_rule_version(repository_id, 1, rules))

    assert result.repository_id == repository_id
    assert result.rules == rules
    assert (
        result.checksum
        == sha256(
            json.dumps(rules, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
    )
    assert session.flushes == 1
