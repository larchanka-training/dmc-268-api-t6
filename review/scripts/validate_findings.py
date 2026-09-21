#!/usr/bin/env python3
"""Stdlib-only validator for the AI reviewer's JSON outputs.

Usage: python review/scripts/validate_findings.py <file.json>

The kind is autodetected from the top-level key: `findings` -> ReviewOutput
(review/prompts/review.system.v1.md, section 10), `files` -> RepoConventionsDraft
(review/prompts/review.conventions.v1.md). Exit codes: 0 = valid (prints
"OK <kind> <n> items"), 1 = contract violations (one "path.to.field: message"
line per violation, on stdout), 2 = the file is missing, not JSON, or its
top-level shape matches neither kind.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import TypeGuard

SEVERITIES = {"critical", "high", "medium", "low", "info"}
CATEGORIES = {"security", "correctness", "performance", "readability"}
EFFORTS = {"none", "small", "medium", "large"}

MAX_FINDINGS = 10
MAX_TITLE_CHARS = 80
MAX_BODY_CHARS = 1200
MAX_KEY_PATTERN_CHARS = 160

ATTRIBUTION_PREFIX = "According to custom instructions in '"

# `(from: standard/<category>)` or `(from: <rule name>)`, nothing after it.
FROM_SUFFIX_RE = re.compile(
    r".*\(from: (standard/(security|correctness|performance|readability)|(?!standard/)[^()]+)\)"
)

FINDING_KEYS = (
    "path",
    "line",
    "start_line",
    "severity",
    "category",
    "title",
    "body",
    "suggestion",
    "confidence",
    "rule_name",
)


def _err(path: str, message: str) -> str:
    return f"{path}: {message}"


def _is_int(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: object) -> TypeGuard[int | float]:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_nonempty_str(value: object) -> TypeGuard[str]:
    return isinstance(value, str) and bool(value)


def validate_review_output(data: object) -> list[str]:
    """Validate `data` against the ReviewOutput contract. Returns violation messages."""
    if not isinstance(data, dict):
        return ["$: must be an object"]

    errors: list[str] = []
    extra = set(data) - {"findings", "summary"}
    if extra:
        errors.append(_err("$", f"unexpected top-level keys: {sorted(extra)}"))

    findings = data.get("findings")
    if not isinstance(findings, list):
        errors.append(_err("findings", "must be a list"))
    else:
        if len(findings) > MAX_FINDINGS:
            errors.append(
                _err("findings", f"must have at most {MAX_FINDINGS} items, got {len(findings)}")
            )
        for i, item in enumerate(findings):
            errors.extend(_validate_finding(item, f"findings[{i}]"))

    errors.extend(_validate_summary(data.get("summary"), "summary"))
    return errors


def _validate_finding(item: object, prefix: str) -> list[str]:
    if not isinstance(item, dict):
        return [_err(prefix, "must be an object")]

    errors: list[str] = []
    missing = [key for key in FINDING_KEYS if key not in item]
    if missing:
        errors.append(_err(prefix, f"missing keys: {missing}"))
    extra = set(item) - set(FINDING_KEYS)
    if extra:
        errors.append(_err(prefix, f"unexpected keys: {sorted(extra)}"))

    if not _is_nonempty_str(item.get("path")):
        errors.append(_err(f"{prefix}.path", "must be a non-empty string"))

    line = item.get("line")
    line_ok = _is_int(line) and line >= 1
    if not line_ok:
        errors.append(_err(f"{prefix}.line", "must be an int >= 1"))

    start_line = item.get("start_line")
    if start_line is not None:
        start_ok = _is_int(start_line) and start_line >= 1
        if not start_ok:
            errors.append(_err(f"{prefix}.start_line", "must be an int >= 1 or null"))
        elif line_ok and start_line >= line:
            errors.append(_err(f"{prefix}.start_line", "must be < line"))

    if item.get("severity") not in SEVERITIES:
        errors.append(_err(f"{prefix}.severity", f"must be one of {sorted(SEVERITIES)}"))

    if item.get("category") not in CATEGORIES:
        errors.append(_err(f"{prefix}.category", f"must be one of {sorted(CATEGORIES)}"))

    title = item.get("title")
    if not (_is_nonempty_str(title) and len(title) <= MAX_TITLE_CHARS):
        errors.append(
            _err(f"{prefix}.title", f"must be a non-empty string, at most {MAX_TITLE_CHARS} chars")
        )

    body = item.get("body")
    body_ok = _is_nonempty_str(body) and len(body) <= MAX_BODY_CHARS
    if not body_ok:
        errors.append(
            _err(f"{prefix}.body", f"must be a non-empty string of at most {MAX_BODY_CHARS} chars")
        )

    suggestion = item.get("suggestion")
    if suggestion is not None and not isinstance(suggestion, str):
        errors.append(_err(f"{prefix}.suggestion", "must be a string or null"))

    confidence = item.get("confidence")
    if not (_is_number(confidence) and 0 <= confidence <= 1):
        errors.append(_err(f"{prefix}.confidence", "must be a number in [0, 1]"))

    rule_name = item.get("rule_name")
    if rule_name is not None and not isinstance(rule_name, str):
        errors.append(_err(f"{prefix}.rule_name", "must be a string or null"))

    if body_ok:
        assert isinstance(body, str)
        if isinstance(rule_name, str):
            expected = f"{ATTRIBUTION_PREFIX}{rule_name}' ("
            if not body.startswith(expected):
                errors.append(
                    _err(f"{prefix}.body", f'rule_name is set, body must start with "{expected}"')
                )
        elif body.startswith(ATTRIBUTION_PREFIX):
            errors.append(
                _err(f"{prefix}.body", "rule_name is null, body must not start with the prefix")
            )

    return errors


def _validate_summary(summary: object, prefix: str) -> list[str]:
    if not isinstance(summary, dict):
        return [_err(prefix, "must be an object")]

    errors: list[str] = []
    extra = set(summary) - {"problem", "done_well", "effort"}
    if extra:
        errors.append(_err(prefix, f"unexpected keys: {sorted(extra)}"))
    if not _is_nonempty_str(summary.get("problem")):
        errors.append(_err(f"{prefix}.problem", "must be a non-empty string"))
    if not _is_nonempty_str(summary.get("done_well")):
        errors.append(_err(f"{prefix}.done_well", "must be a non-empty string"))
    if summary.get("effort") not in EFFORTS:
        errors.append(_err(f"{prefix}.effort", f"must be one of {sorted(EFFORTS)}"))
    return errors


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
