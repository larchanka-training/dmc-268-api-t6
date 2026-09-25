"""Create the immutable initial rule version for an onboarded repository."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID

from jsonschema import (  # type: ignore[import-untyped]  # jsonschema has no shipped stubs.
    Draft202012Validator,
    SchemaError,
    ValidationError,
)


class RuleSetValidationError(ValueError):
    """A default rule artifact does not satisfy its published JSON schema."""


@dataclass(frozen=True)
class DefaultRuleSet:
    """A validated, immutable default-rule artifact."""

    version: int
    stack: str
    rules: list[dict[str, object]]


@dataclass(frozen=True)
class PersistedRuleVersion:
    """The active version returned by the repository onboarding port."""

    repository_id: UUID
    version: int
    rules: list[dict[str, object]]
    checksum: str


@dataclass(frozen=True)
class OnboardingResult:
    rule_version: PersistedRuleVersion
    created: bool


class RepositoryRuleVersionStore(Protocol):
    async def get_active_rule_version(self, repository_id: UUID) -> PersistedRuleVersion | None: ...

    async def get_or_create_initial_rule_version(
        self, repository_id: UUID, version: int, rules: list[dict[str, object]]
    ) -> PersistedRuleVersion: ...


_FILENAME = re.compile(r"^default-(?P<stack>backend|frontend)\.v(?P<version>[1-9]\d*)\.json$")
_BACKEND_LANGUAGES = frozenset(
    {"Python", "Go", "Java", "Kotlin", "Ruby", "PHP", "C#", "Rust", "Elixir", "Scala"}
)
_FRONTEND_LANGUAGES = frozenset({"TypeScript", "JavaScript", "TSX", "JSX", "Vue", "Svelte"})


def load_default_rule_sets(rules_dir: Path) -> dict[str, DefaultRuleSet]:
    """Load default assets and validate every value against ``schema.json``.

    Validation is intentionally performed at onboarding/bootstrap time rather than
    at prompt rendering time, so invalid immutable artifacts cannot enter the DB.
    """
    schema = _read_json_object(rules_dir / "schema.json")
    assets: dict[str, DefaultRuleSet] = {}
    for path in sorted(rules_dir.glob("default-*.v*.json")):
        match = _FILENAME.fullmatch(path.name)
        if match is None:
            raise RuleSetValidationError(f"invalid default rule filename: {path.name}")
        data = _read_json_object(path)
        errors = _validate_schema(schema, data)
        if errors:
            raise RuleSetValidationError(f"{path.name}: " + "; ".join(errors))
        stack = match.group("stack")
        version = int(match.group("version"))
        if data["stack"] != stack or data["version"] != version:
            raise RuleSetValidationError(f"{path.name}: filename does not match stack and version")
        rules_value = data["rules"]
        assert isinstance(rules_value, list)
        rules = [dict(rule) for rule in rules_value if isinstance(rule, dict)]
        names = [rule["name"] for rule in rules]
        if len(names) != len(set(names)):
            raise RuleSetValidationError(f"{path.name}: rule names must be unique")
        if stack in assets:
            raise RuleSetValidationError(f"multiple default artifacts for stack {stack}")
        assets[stack] = DefaultRuleSet(version=version, stack=stack, rules=rules)
    if set(assets) != {"backend", "frontend"}:
        raise RuleSetValidationError("default backend and frontend rule artifacts are required")
    return assets


def select_default_rule_set(
    rule_sets: Mapping[str, DefaultRuleSet], languages: Mapping[str, int]
) -> DefaultRuleSet:
    """Select the stack with the largest recognized language byte count.

    A tie and a repository with no recognized language deliberately fall back to
    ``backend``.  This keeps onboarding deterministic and is safe for the
    service's Python-first default until a repository explicitly replaces rules.
    """
    backend = sum(size for name, size in languages.items() if name in _BACKEND_LANGUAGES)
    frontend = sum(size for name, size in languages.items() if name in _FRONTEND_LANGUAGES)
    stack = "frontend" if frontend > backend else "backend"
    try:
        return rule_sets[stack]
    except KeyError as error:
        raise RuleSetValidationError(f"missing default {stack} rule artifact") from error


class OnboardRepository:
    """Idempotently attach the selected immutable defaults to a repository."""

    def __init__(
        self, repository: RepositoryRuleVersionStore, rule_sets: Mapping[str, DefaultRuleSet]
    ) -> None:
        self._repository = repository
        self._rule_sets = rule_sets

    async def execute(self, repository_id: UUID, languages: Mapping[str, int]) -> OnboardingResult:
        existing = await self._repository.get_active_rule_version(repository_id)
        if existing is not None:
            return OnboardingResult(rule_version=existing, created=False)
        selected = select_default_rule_set(self._rule_sets, languages)
        version = await self._repository.get_or_create_initial_rule_version(
            repository_id, selected.version, selected.rules
        )
        return OnboardingResult(rule_version=version, created=True)


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuleSetValidationError(f"cannot read JSON artifact {path}") from error
    if not isinstance(parsed, dict):
        raise RuleSetValidationError(f"{path.name}: top-level JSON value must be an object")
    return parsed


def _validate_schema(schema: dict[str, Any], value: object) -> list[str]:
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as error:
        raise RuleSetValidationError(
            f"schema.json: invalid Draft 2020-12 schema: {error.message}"
        ) from error

    validator = Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(value), key=lambda error: list(error.absolute_path))
    return [_format_schema_error(error) for error in errors]


def _format_schema_error(error: ValidationError) -> str:
    path = error.absolute_path
    message = error.message
    rendered_path = "$" + "".join(
        f"[{part}]" if isinstance(part, int) else f".{part}" for part in path
    )
    return f"{rendered_path}: {message}"
