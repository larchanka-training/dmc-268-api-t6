"""Contract tests over the review/ artefacts (issue #18, plan D18)."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
REVIEW_DIR = REPO_ROOT / "review"
VALIDATOR = REVIEW_DIR / "scripts" / "validate_findings.py"


def _run_validator(path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(VALIDATOR), str(path)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


# ---------- (a) review/rules/*.json structural check against schema.json ----------

SCHEMA: dict[str, Any] = _load_json(REVIEW_DIR / "rules" / "schema.json")
DEFS: dict[str, Any] = SCHEMA["$defs"]


def _resolve(node: dict[str, Any]) -> dict[str, Any]:
    if "$ref" in node:
        name = node["$ref"].rsplit("/", 1)[-1]
        result: dict[str, Any] = DEFS[name]
        return result
    return node


def _check(node: dict[str, Any], value: object, path: str) -> list[str]:
    node = _resolve(node)
    node_type = node.get("type")
    errors: list[str] = []

    if node_type == "object":
        if not isinstance(value, dict):
            return [f"{path}: expected object"]
        for key in node.get("required", []):
            if key not in value:
                errors.append(f"{path}.{key}: missing required key")
        properties: dict[str, Any] = node.get("properties", {})
        if node.get("additionalProperties") is False:
            extra = set(value) - set(properties)
            if extra:
                errors.append(f"{path}: unexpected keys {sorted(extra)}")
        for key, subnode in properties.items():
            if key in value:
                errors.extend(_check(subnode, value[key], f"{path}.{key}"))

    elif node_type == "array":
        if not isinstance(value, list):
            return [f"{path}: expected array"]
        min_items = node.get("minItems")
        if min_items is not None and len(value) < min_items:
            errors.append(f"{path}: fewer than {min_items} items")
        max_items = node.get("maxItems")
        if max_items is not None and len(value) > max_items:
            errors.append(f"{path}: more than {max_items} items")
        items_schema = node.get("items")
        if items_schema is not None:
            for i, item in enumerate(value):
                errors.extend(_check(items_schema, item, f"{path}[{i}]"))

    elif node_type == "string":
        if not isinstance(value, str):
            return [f"{path}: expected string"]
        min_length = node.get("minLength")
        if min_length is not None and len(value) < min_length:
            errors.append(f"{path}: shorter than {min_length}")
        max_length = node.get("maxLength")
        if max_length is not None and len(value) > max_length:
            errors.append(f"{path}: longer than {max_length}")
        enum = node.get("enum")
        if enum is not None and value not in enum:
            errors.append(f"{path}: not in {enum}")

    elif node_type == "integer":
        if not isinstance(value, int) or isinstance(value, bool):
            errors.append(f"{path}: expected integer")
        else:
            minimum = node.get("minimum")
            if minimum is not None and value < minimum:
                errors.append(f"{path}: below minimum {minimum}")

    return errors


RULE_SET_FILES = sorted(p for p in (REVIEW_DIR / "rules").glob("*.json") if p.name != "schema.json")


@pytest.mark.parametrize("path", RULE_SET_FILES, ids=lambda p: p.name)
def test_rule_set_matches_schema(path: Path) -> None:
    data = _load_json(path)
    errors = _check(DEFS["RuleSet"], data, "$")
    assert errors == []


# ---------- (b) review/prompts/*.md frontmatter vs. filename version ----------

FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)
FILENAME_RE = re.compile(r"^(?P<key>.+)\.v(?P<version>\d+)\.md$")

PROMPT_FILES = sorted((REVIEW_DIR / "prompts").glob("*.md"))


def _frontmatter(path: Path) -> dict[str, str]:
    text = path.read_text(encoding="utf-8")
    match = FRONTMATTER_RE.match(text)
    assert match is not None, f"{path}: no YAML frontmatter"
    fields: dict[str, str] = {}
    for line in match.group(1).splitlines():
        if ":" in line and not line.startswith((" ", "\t")):
            key, _, value = line.partition(":")
            fields[key.strip()] = value.strip()
    return fields


@pytest.mark.parametrize("path", PROMPT_FILES, ids=lambda p: p.name)
def test_prompt_frontmatter_version_matches_filename(path: Path) -> None:
    fields = _frontmatter(path)
    assert "key" in fields
    assert "version" in fields

    name_match = FILENAME_RE.match(path.name)
    assert name_match is not None, f"{path.name}: filename must end in .v<N>.md"
    assert fields["version"] == name_match.group("version")


# ---------- (c) review.system.v1.md literals (plan §8 G-literals) ----------

SYSTEM_PROMPT_TEXT = (REVIEW_DIR / "prompts" / "review.system.v1.md").read_text(encoding="utf-8")

REQUIRED_LITERALS = [
    "According to custom instructions in '",
    "security → correctness → performance → readability",
    "AGENTS.md",
    "Do not report",
    "<rule name=",
    "what changed",
    "differs from",
    "why",
    "what breaks",
    "what to do",
    "problem",
    "done_well",
    "effort",
]


@pytest.mark.parametrize("literal", REQUIRED_LITERALS)
def test_system_prompt_contains_literal(literal: str) -> None:
    assert literal in SYSTEM_PROMPT_TEXT


# ---------- (d) lint-filter-patterns.json ----------


def test_lint_filter_patterns_are_valid() -> None:
    data = _load_json(REVIEW_DIR / "postprocess" / "lint-filter-patterns.json")

    for key in ("drop_if_any", "keep_if_any"):
        for pattern in data[key]:
            re.compile(pattern)

    assert 0 <= data["min_confidence"] <= 1
    assert data["max_inline"] <= 10


# ---------- (e) review/examples/*.sample.json pass the validator ----------


@pytest.mark.parametrize("filename", ["findings.sample.json", "conventions.sample.json"])
def test_sample_output_is_valid(filename: str) -> None:
    path = REVIEW_DIR / "examples" / filename
    if not path.is_file():
        pytest.skip(f"{path} does not exist yet (arrives in T10)")

    result = _run_validator(path)

    assert result.returncode == 0, result.stdout + result.stderr


# ---------- (f) the validator rejects a broken fixture ----------


def test_validator_rejects_broken_fixture(tmp_path: Path) -> None:
    broken = {
        "findings": [
            {
                "path": "app/modules/reviews/application/use_cases.py",
                "line": 42,
                "start_line": None,
                "severity": "urgent",  # not a valid severity
                "category": "correctness",
                "title": "Transaction stays open across the GitHub call",
                "body": "missing the mandatory attribution prefix",  # rule_name is set below
                "suggestion": None,
                "confidence": 0.8,
                "rule_name": "Error Handling Standards",
            }
        ],
        "summary": {"problem": "p", "done_well": "d", "effort": "small"},
    }
    path = tmp_path / "broken.json"
    path.write_text(json.dumps(broken), encoding="utf-8")

    result = _run_validator(path)

    assert result.returncode == 1
    assert "findings[0].severity" in result.stdout
    assert "findings[0].body" in result.stdout
