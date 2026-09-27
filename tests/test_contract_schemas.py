"""Contract tests for the review.run/v1 and review.publish/v1 queue message schemas (#20).

No database involved: these schemas and fixtures describe RabbitMQ pointer messages
(SYSTEM_DESIGN.md §7.2), not HTTP or persistence contracts.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEMAS_DIR = REPO_ROOT / "contracts" / "schemas"
EXAMPLES_DIR = REPO_ROOT / "contracts" / "examples"

MESSAGES = ["review.run.v1", "review.publish.v1"]

Mutator = Callable[[dict[str, Any]], dict[str, Any]]


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _schema(name: str) -> dict[str, Any]:
    result: dict[str, Any] = _load_json(SCHEMAS_DIR / f"{name}.schema.json")
    return result


def _example(name: str) -> dict[str, Any]:
    result: dict[str, Any] = _load_json(EXAMPLES_DIR / f"{name}.json")
    return result


def _validator(name: str) -> Draft202012Validator:
    return Draft202012Validator(_schema(name), format_checker=Draft202012Validator.FORMAT_CHECKER)


# ---------- (a) each schema is a valid draft 2020-12 schema ----------


@pytest.mark.parametrize("name", MESSAGES)
def test_schema_is_valid_draft_2020_12(name: str) -> None:
    Draft202012Validator.check_schema(_schema(name))


# ---------- (b) each fixture validates against its schema ----------


@pytest.mark.parametrize("name", MESSAGES)
def test_example_matches_schema(name: str) -> None:
    _validator(name).validate(_example(name))


# ---------- (c) mutation table: each mutation is rejected ----------


def _set(data: dict[str, Any], dotted_key: str, value: Any) -> dict[str, Any]:
    mutated = copy.deepcopy(data)
    node = mutated
    parts = dotted_key.split(".")
    for part in parts[:-1]:
        node = node[part]
    node[parts[-1]] = value
    return mutated


def _remove(data: dict[str, Any], key: str) -> dict[str, Any]:
    mutated = copy.deepcopy(data)
    del mutated[key]
    return mutated


def _add_extra(data: dict[str, Any]) -> dict[str, Any]:
    mutated = copy.deepcopy(data)
    mutated["extra"] = "not-in-schema"
    return mutated


RUN_REQUIRED_KEYS = [
    "schema",
    "message_id",
    "run_id",
    "workspace_id",
    "installation_id",
    "repo",
    "pr",
    "engine",
    "rule_version_id",
    "prompt_version_id",
    "trigger",
    "attempt",
    "requested_at",
]

PUBLISH_REQUIRED_KEYS = [
    "schema",
    "message_id",
    "run_id",
    "head_sha",
    "findings_hash",
    "review_event",
]


def _missing_key_cases(name: str, keys: list[str]) -> list[Any]:
    return [
        pytest.param(name, (lambda d, k=key: _remove(d, k)), id=f"{name}-missing-{key}")
        for key in keys
    ]


RUN_MUTATIONS = [
    *_missing_key_cases("review.run.v1", RUN_REQUIRED_KEYS),
    pytest.param("review.run.v1", _add_extra, id="review.run.v1-extra-key"),
    pytest.param(
        "review.run.v1",
        (lambda d: _set(d, "message_id", "b3c1…")),
        id="review.run.v1-placeholder-id",
    ),
    pytest.param(
        "review.run.v1",
        (lambda d: _set(d, "workspace_id", "ws_1")),
        id="review.run.v1-prefixed-id",
    ),
    pytest.param(
        "review.run.v1", (lambda d: _set(d, "engine", "medium")), id="review.run.v1-bad-engine"
    ),
    pytest.param(
        "review.run.v1", (lambda d: _set(d, "trigger", "cron")), id="review.run.v1-bad-trigger"
    ),
    pytest.param(
        "review.run.v1", (lambda d: _set(d, "attempt", 0)), id="review.run.v1-attempt-zero"
    ),
    pytest.param(
        "review.run.v1",
        (lambda d: _set(d, "pr.head_sha", "a3f9")),
        id="review.run.v1-short-sha",
    ),
]

PUBLISH_MUTATIONS = [
    *_missing_key_cases("review.publish.v1", PUBLISH_REQUIRED_KEYS),
    pytest.param("review.publish.v1", _add_extra, id="review.publish.v1-extra-key"),
    pytest.param(
        "review.publish.v1",
        (lambda d: _set(d, "message_id", "b3c1…")),
        id="review.publish.v1-placeholder-id",
    ),
    pytest.param(
        "review.publish.v1",
        (lambda d: _set(d, "run_id", "ws_1")),
        id="review.publish.v1-prefixed-id",
    ),
    pytest.param(
        "review.publish.v1",
        (lambda d: _set(d, "findings_hash", "sha256:" + "a" * 64)),
        id="review.publish.v1-findings-hash-prefixed",
    ),
    pytest.param(
        "review.publish.v1",
        (lambda d: _set(d, "findings_hash", "a" * 63)),
        id="review.publish.v1-findings-hash-63-hex",
    ),
    pytest.param(
        "review.publish.v1",
        (lambda d: _set(d, "findings_hash", "A" * 64)),
        id="review.publish.v1-findings-hash-uppercase",
    ),
    pytest.param(
        "review.publish.v1",
        (lambda d: _set(d, "review_event", "APPROVE")),
        id="review.publish.v1-bad-review-event",
    ),
    pytest.param(
        "review.publish.v1",
        (lambda d: _set(d, "head_sha", "a3f9")),
        id="review.publish.v1-short-sha",
    ),
]


@pytest.mark.parametrize(("name", "mutate"), [*RUN_MUTATIONS, *PUBLISH_MUTATIONS])
def test_mutation_is_rejected(name: str, mutate: Mutator) -> None:
    mutated = mutate(_example(name))

    with pytest.raises(ValidationError):
        _validator(name).validate(mutated)
