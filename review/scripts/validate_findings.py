#!/usr/bin/env python3
"""Validator for the AI reviewer's JSON outputs.

Usage: uv run python review/scripts/validate_findings.py <file.json>

The kind is autodetected from the top-level key: `findings` -> ReviewOutput
(review/prompts/review.system.v2.md, section 10), `files` -> RepoConventionsDraft
(review/prompts/review.conventions.v2.md). A ReviewOutput's shape is checked
against review/schemas/review-output.schema.json (hence `jsonschema` and
`uv run`); this script adds only the rules that schema cannot express. Exit
codes: 0 = valid (prints "OK <kind> <n> items"), 1 = contract violations (one
"path.to.field: message" line per violation, on stdout), 2 = the file is
missing, not JSON, or its top-level shape matches neither kind.
"""

from __future__ import annotations

import json
import re
import sys
from functools import cache
from pathlib import Path
from typing import TypeGuard

from jsonschema import Draft202012Validator, ValidationError

REVIEW_OUTPUT_SCHEMA = Path(__file__).resolve().parents[1] / "schemas" / "review-output.schema.json"

MAX_KEY_PATTERN_CHARS = 160
MAX_MESSAGE_CHARS = 200

ATTRIBUTION_PREFIX = "According to custom instructions in '"

# Mirror the model validators of the runtime contract
# (app/modules/reviews/application/review_output.py); this script runs standalone.
SEVERITY_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
SENTENCE_RE = re.compile(r"[^.!?]+[.!?](?:\s|$)")

# `(from: standard/<category>)` or `(from: <rule name>)`, nothing after it.
FROM_SUFFIX_RE = re.compile(
    r".*\(from: (standard/(security|correctness|performance|readability)|(?!standard/)[^()]+)\)"
)


def _err(path: str, message: str) -> str:
    return f"{path}: {message}"


def _is_int(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: object) -> TypeGuard[int | float]:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_nonempty_str(value: object) -> TypeGuard[str]:
    return isinstance(value, str) and bool(value)


@cache
def _review_output_validator() -> Draft202012Validator:
    schema = json.loads(REVIEW_OUTPUT_SCHEMA.read_text(encoding="utf-8"))
    return Draft202012Validator(schema)


def _field_path(json_path: str) -> str:
    """`$.findings[0].line` -> `findings[0].line`; the root stays `$`."""
    return json_path.removeprefix("$.")


def _schema_message(error: ValidationError) -> str:
    """jsonschema quotes the offending value; name the keyword instead when that is long."""
    if len(error.message) <= MAX_MESSAGE_CHARS:
        return error.message
    return f"fails {error.validator}: {error.validator_value!r}"


def validate_review_output(data: object) -> list[str]:
    """Validate `data` against the ReviewOutput contract. Returns violation messages."""
    errors = [
        _err(_field_path(error.json_path), _schema_message(error))
        for error in _review_output_validator().iter_errors(data)
    ]
    # Semantic rules run even on a shape-invalid object, so one run reports both;
    # each check skips values whose shape the schema already rejects.
    if isinstance(data, dict):
        errors.extend(_semantic_errors(data))
    return sorted(errors)


def _semantic_errors(data: dict[object, object]) -> list[str]:
    errors: list[str] = []
    findings = data.get("findings")
    if isinstance(findings, list):
        for i, item in enumerate(findings):
            if isinstance(item, dict):
                errors.extend(_finding_semantic_errors(item, f"findings[{i}]"))
        errors.extend(_order_errors(findings))

    summary = data.get("summary")
    if isinstance(summary, dict):
        errors.extend(_summary_semantic_errors(summary, "summary"))
    return errors


def _finding_semantic_errors(item: dict[object, object], prefix: str) -> list[str]:
    errors: list[str] = []

    # JSON Schema's `integer` admits 1.0; the strict runtime model does not.
    for key in ("line", "start_line"):
        value = item.get(key)
        if isinstance(value, float) and value.is_integer():
            errors.append(_err(f"{prefix}.{key}", "must be an integer, not a float"))

    line = item.get("line")
    start_line = item.get("start_line")
    if _is_int(line) and _is_int(start_line) and start_line >= line:
        errors.append(_err(f"{prefix}.start_line", "must be < line"))

    title = item.get("title")
    if _is_nonempty_str(title):
        if title.endswith("."):
            errors.append(_err(f"{prefix}.title", "must not end with a period"))
        if title.splitlines() != [title]:
            errors.append(_err(f"{prefix}.title", "must be one line"))

    body = item.get("body")
    rule_name = item.get("rule_name")
    if _is_nonempty_str(body):
        if isinstance(rule_name, str):
            expected = f"{ATTRIBUTION_PREFIX}{rule_name}' ("
            if not body.startswith(expected):
                errors.append(
                    _err(f"{prefix}.body", f'rule_name is set, body must start with "{expected}"')
                )
        elif rule_name is None and body.startswith(ATTRIBUTION_PREFIX):
            errors.append(
                _err(f"{prefix}.body", "rule_name is null, body must not start with the prefix")
            )

    return errors


def _order_errors(findings: list[object]) -> list[str]:
    """Severity first, then confidence descending, as the runtime model requires."""
    ordering: list[tuple[int, float]] = []
    for item in findings:
        if not isinstance(item, dict):
            return []
        severity = item.get("severity")
        confidence = item.get("confidence")
        if not (isinstance(severity, str) and severity in SEVERITY_RANK):
            return []
        if not _is_number(confidence):
            return []
        ordering.append((SEVERITY_RANK[severity], -confidence))
    if ordering != sorted(ordering):
        return [_err("findings", "must be ordered by severity, then by confidence descending")]
    return []


def _summary_semantic_errors(summary: dict[object, object], prefix: str) -> list[str]:
    errors: list[str] = []
    problem = summary.get("problem")
    if _is_nonempty_str(problem) and _sentence_count(problem) != 1:
        errors.append(_err(f"{prefix}.problem", "must contain exactly one sentence"))
    done_well = summary.get("done_well")
    if _is_nonempty_str(done_well) and not 1 <= _sentence_count(done_well) <= 2:
        errors.append(_err(f"{prefix}.done_well", "must contain one or two sentences"))
    return errors


def _sentence_count(text: str) -> int:
    return len(SENTENCE_RE.findall(text))


def validate_conventions(data: object) -> list[str]:
    """Validate `data` against the RepoConventionsDraft contract. Returns violation messages."""
    if not isinstance(data, dict):
        return ["$: must be an object"]

    errors: list[str] = []
    extra = set(data) - {"files", "key_patterns", "recommendations"}
    if extra:
        errors.append(_err("$", f"unexpected top-level keys: {sorted(extra)}"))

    files = data.get("files")
    if not isinstance(files, list):
        errors.append(_err("files", "must be a list"))
    else:
        if not files:
            errors.append(_err("files", "must have at least 1 item"))
        seen: set[str] = set()
        for i, item in enumerate(files):
            errors.extend(_validate_file_entry(item, f"files[{i}]"))
            path = item.get("path") if isinstance(item, dict) else None
            if isinstance(path, str):
                if path in seen:
                    errors.append(_err(f"files[{i}].path", f"duplicate path {path!r}"))
                seen.add(path)

    key_patterns = data.get("key_patterns")
    errors.extend(_validate_string_list(key_patterns, "key_patterns", 3, 10, MAX_KEY_PATTERN_CHARS))
    errors.extend(_validate_recommendations(data.get("recommendations"), "recommendations"))
    return errors


def _validate_file_entry(item: object, prefix: str) -> list[str]:
    if not isinstance(item, dict):
        return [_err(prefix, "must be an object")]

    errors: list[str] = []
    extra = set(item) - {"path", "relevance"}
    if extra:
        errors.append(_err(prefix, f"unexpected keys: {sorted(extra)}"))
    for key in ("path", "relevance"):
        if not _is_nonempty_str(item.get(key)):
            errors.append(_err(f"{prefix}.{key}", "must be a non-empty string"))
    return errors


def _validate_string_list(
    value: object, prefix: str, min_len: int, max_len: int, max_chars: int | None = None
) -> list[str]:
    if not isinstance(value, list):
        return [_err(prefix, "must be a list")]

    errors: list[str] = []
    if not (min_len <= len(value) <= max_len):
        errors.append(
            _err(prefix, f"must have between {min_len} and {max_len} items, got {len(value)}")
        )
    for i, item in enumerate(value):
        if not _is_nonempty_str(item):
            errors.append(_err(f"{prefix}[{i}]", "must be a non-empty string"))
        elif max_chars is not None and len(item) > max_chars:
            errors.append(_err(f"{prefix}[{i}]", f"must be at most {max_chars} chars"))
    return errors


def _validate_recommendations(value: object, prefix: str) -> list[str]:
    errors = _validate_string_list(value, prefix, 5, 12)
    if isinstance(value, list):
        for i, item in enumerate(value):
            if _is_nonempty_str(item) and not _ends_with_from(item):
                errors.append(
                    _err(
                        f"{prefix}[{i}]",
                        "must end with '(from: standard/<category>)' or '(from: <rule name>)'",
                    )
                )
    return errors


def _ends_with_from(text: str) -> bool:
    return FROM_SUFFIX_RE.fullmatch(text.rstrip()) is not None


def _load(path: Path) -> tuple[object | None, str | None]:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        return None, f"cannot read {path}: {exc}"
    try:
        return json.loads(raw), None
    except json.JSONDecodeError as exc:
        return None, f"not valid JSON: {exc}"


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: validate_findings.py <file.json>", file=sys.stderr)
        return 2

    path = Path(args[0])
    if not path.is_file():
        print(f"error: no such file: {path}", file=sys.stderr)
        return 2

    data, load_error = _load(path)
    if load_error is not None:
        print(f"error: {load_error}", file=sys.stderr)
        return 2

    if not isinstance(data, dict):
        print("error: top-level JSON value must be an object", file=sys.stderr)
        return 2

    if "findings" in data and "files" in data:
        print(
            "error: unknown kind: top-level object has both 'findings' and 'files'",
            file=sys.stderr,
        )
        return 2

    if "findings" in data:
        kind = "ReviewOutput"
        errors = validate_review_output(data)
        n_items = len(data["findings"]) if isinstance(data.get("findings"), list) else 0
    elif "files" in data:
        kind = "RepoConventionsDraft"
        errors = validate_conventions(data)
        n_items = len(data["files"]) if isinstance(data.get("files"), list) else 0
    else:
        print(
            "error: unknown kind: top-level object has neither 'findings' nor 'files'",
            file=sys.stderr,
        )
        return 2

    if errors:
        for error in errors:
            print(error)
        return 1

    print(f"OK {kind} {n_items} items")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
