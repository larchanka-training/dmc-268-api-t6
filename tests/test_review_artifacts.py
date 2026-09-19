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
        pattern = node.get("pattern")
        if pattern is not None and re.fullmatch(pattern, value) is None:
            errors.append(f"{path}: does not match {pattern}")

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

    names = [rule["name"] for rule in data["rules"]]
    assert len(set(names)) == len(names), f"{path.name}: rule names are not unique"


# ---------- (b) review/prompts/*.md frontmatter vs. filename version ----------

FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)
FILENAME_RE = re.compile(r"^(?P<key>.+)\.v(?P<version>\d+)\.md$")
FRONTMATTER_FIELDS = ("key", "version", "description", "input_tags", "output_schema")

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
    for field in FRONTMATTER_FIELDS:
        assert field in fields, f"{path.name}: frontmatter lacks `{field}`"
    for field, value in fields.items():
        if not value.startswith(('"', "'")):
            assert ": " not in value, f"{path.name}: unquoted `{field}` contains ': ' (not YAML)"

    name_match = FILENAME_RE.match(path.name)
    assert name_match is not None, f"{path.name}: filename must end in .v<N>.md"
    assert fields["key"] == name_match.group("key")
    assert fields["version"] == name_match.group("version")


# ---------- (c) review.system.v1.md literals (plan §8 G-literals) ----------

SYSTEM_PROMPT_TEXT = (REVIEW_DIR / "prompts" / "review.system.v1.md").read_text(encoding="utf-8")
# Prose is hard-wrapped; a literal may span a line break.
SYSTEM_PROMPT_FLAT = " ".join(SYSTEM_PROMPT_TEXT.split())

ATTRIBUTION_PREFIX = "According to custom instructions in '"

REQUIRED_LITERALS = [
    ATTRIBUTION_PREFIX,
    "security → correctness → performance → readability",
    "AGENTS.md",
    "Do not report",
    "<rule name=",
    "what changed",
    "differs from",
    "why it was done that way",
    "what breaks",
    "what to do",
    "`problem` — one sentence naming the main issue",
    "`done_well` — what is done well",
    "`effort` — the work needed before merge",
]


@pytest.mark.parametrize("literal", REQUIRED_LITERALS)
def test_system_prompt_contains_literal(literal: str) -> None:
    assert literal in SYSTEM_PROMPT_FLAT


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
    assert path.is_file()

    result = _run_validator(path)

    assert result.returncode == 0, result.stdout + result.stderr


# ---------- (f) the validator rejects a broken fixture ----------


def _review_output(**finding: object) -> dict[str, Any]:
    base: dict[str, object] = {
        "path": "app/modules/reviews/application/use_cases.py",
        "line": 42,
        "start_line": None,
        "severity": "high",
        "category": "correctness",
        "title": "Transaction stays open across the GitHub call",
        "body": "The transaction stays open across the GitHub call.",
        "suggestion": None,
        "confidence": 0.8,
        "rule_name": None,
    }
    return {
        "findings": [{**base, **finding}],
        "summary": {"problem": "p", "done_well": "d", "effort": "small"},
    }


def test_validator_rejects_broken_fixture(tmp_path: Path) -> None:
    broken = _review_output(
        severity="urgent",  # not a valid severity
        body="missing the mandatory attribution prefix",  # rule_name is set below
        rule_name="Error Handling Standards",
    )
    path = tmp_path / "broken.json"
    path.write_text(json.dumps(broken), encoding="utf-8")

    result = _run_validator(path)

    assert result.returncode == 1
    assert "findings[0].severity" in result.stdout
    assert "findings[0].body" in result.stdout


@pytest.mark.parametrize(
    ("body", "rule_name"),
    [
        pytest.param(
            f"{ATTRIBUTION_PREFIX}Naming Consistency' (names follow the module): swallowed",
            "Error Handling Standards",
            id="prefix-names-another-rule",
        ),
        pytest.param(
            f"{ATTRIBUTION_PREFIX}Error Handling Standards' (no swallowed error): swallowed",
            None,
            id="prefix-with-rule-name-null",
        ),
    ],
)
def test_validator_rejects_prefix_not_bound_to_rule_name(
    tmp_path: Path, body: str, rule_name: str | None
) -> None:
    path = tmp_path / "broken.json"
    path.write_text(json.dumps(_review_output(body=body, rule_name=rule_name)), encoding="utf-8")

    result = _run_validator(path)

    assert result.returncode == 1
    assert "findings[0].body" in result.stdout
