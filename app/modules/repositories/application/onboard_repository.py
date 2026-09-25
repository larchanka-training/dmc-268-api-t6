"""Create the immutable initial rule version for an onboarded repository."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID


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
    definitions = schema.get("$defs")
    if not isinstance(definitions, dict):
        raise RuleSetValidationError("schema.json: missing $defs")
    root_ref = schema.get("$ref")
    if not isinstance(root_ref, str):
        raise RuleSetValidationError("schema.json: missing root $ref")
    return _validate_node({"$ref": root_ref}, value, "$", definitions)


def _validate_node(
    node: dict[str, Any], value: object, path: str, definitions: dict[str, Any]
) -> list[str]:
    reference = node.get("$ref")
    if isinstance(reference, str):
        name = reference.rsplit("/", 1)[-1]
        referenced = definitions.get(name)
        if not isinstance(referenced, dict):
            return [f"{path}: unresolved schema reference {reference}"]
        return _validate_node(referenced, value, path, definitions)

    node_type = node.get("type")
    if node_type == "object":
        if not isinstance(value, dict):
            return [f"{path}: expected object"]
        properties = node.get("properties", {})
        required = node.get("required", [])
        if not isinstance(properties, dict) or not isinstance(required, list):
            return [f"{path}: invalid object schema"]
        object_errors = [
            f"{path}.{key}: missing required key" for key in required if key not in value
        ]
        if node.get("additionalProperties") is False:
            extras = set(value) - set(properties)
            if extras:
                object_errors.append(f"{path}: unexpected keys {sorted(extras)}")
        for key, subnode in properties.items():
            if key in value and isinstance(subnode, dict):
                object_errors.extend(
                    _validate_node(subnode, value[key], f"{path}.{key}", definitions)
                )
        return object_errors
    if node_type == "array":
        if not isinstance(value, list):
            return [f"{path}: expected array"]
        array_errors: list[str] = []
        minimum = node.get("minItems")
        maximum = node.get("maxItems")
        if isinstance(minimum, int) and len(value) < minimum:
            array_errors.append(f"{path}: fewer than {minimum} items")
        if isinstance(maximum, int) and len(value) > maximum:
            array_errors.append(f"{path}: more than {maximum} items")
        items = node.get("items")
        if isinstance(items, dict):
            for index, item in enumerate(value):
                array_errors.extend(_validate_node(items, item, f"{path}[{index}]", definitions))
        return array_errors
    if node_type == "string":
        if not isinstance(value, str):
            return [f"{path}: expected string"]
        string_errors: list[str] = []
        minimum = node.get("minLength")
        maximum = node.get("maxLength")
        if isinstance(minimum, int) and len(value) < minimum:
            string_errors.append(f"{path}: shorter than {minimum}")
        if isinstance(maximum, int) and len(value) > maximum:
            string_errors.append(f"{path}: longer than {maximum}")
        enum = node.get("enum")
        if isinstance(enum, list) and value not in enum:
            string_errors.append(f"{path}: not in {enum}")
        pattern = node.get("pattern")
        if isinstance(pattern, str) and re.fullmatch(pattern, value) is None:
            string_errors.append(f"{path}: does not match {pattern}")
        return string_errors
    if node_type == "integer":
        if not isinstance(value, int) or isinstance(value, bool):
            return [f"{path}: expected integer"]
        minimum = node.get("minimum")
        if isinstance(minimum, int) and value < minimum:
            return [f"{path}: below minimum {minimum}"]
    return []
