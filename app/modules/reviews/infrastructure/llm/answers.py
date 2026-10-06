"""Validation of a model answer: the gateway's parse path (docs/PIPELINE_SPEC.md §9).

A review answer must pass ``review/schemas/review-output.schema.json`` and then
``parse_review_output``; the collected messages feed the one repair call (§5.1). There
is no second copy of the schema: the file is read once and sent to the provider as is,
minus its ``$``-annotations.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from functools import cache
from pathlib import Path
from typing import Any, cast

from jsonschema import Draft202012Validator
from pydantic import ValidationError

from app.modules.reviews.application.conventions import ConventionsDraft
from app.modules.reviews.application.review_output import (
    InvalidReviewOutput,
    ReviewFinding,
    ReviewOutput,
    Severity,
    finding_order_key,
    parse_review_output,
)

REVIEW_OUTPUT_SCHEMA_PATH = (
    Path(__file__).resolve().parents[5] / "review" / "schemas" / "review-output.schema.json"
)
_ANNOTATIONS = frozenset({"$schema", "$id", "$comment"})
_MAX_ERRORS = 20
_MAX_ERROR_CHARS = 300


class InvalidAnswer(ValueError):
    """The answer broke the contract; ``errors`` are the validator's own messages."""

    def __init__(self, errors: list[str]) -> None:
        super().__init__("; ".join(errors))
        self.errors = errors


@cache
def review_output_schema() -> dict[str, Any]:
    """The single ReviewOutput shape (draft 2020-12)."""
    loaded: dict[str, Any] = json.loads(REVIEW_OUTPUT_SCHEMA_PATH.read_text(encoding="utf-8"))
    return loaded


@cache
def _review_validator() -> Draft202012Validator:
    return Draft202012Validator(review_output_schema())


def provider_schema(schema: Mapping[str, Any]) -> dict[str, Any]:
    """The schema for ``response_format``: identical, without ``$``-annotations at any level."""
    stripped: Any = _strip_annotations(schema)
    return dict(stripped)


def _strip_annotations(node: Any) -> Any:
    if isinstance(node, Mapping):
        return {
            key: _strip_annotations(value) for key, value in node.items() if key not in _ANNOTATIONS
        }
    if isinstance(node, list):
        return [_strip_annotations(item) for item in node]
    return node


def validate_review_answer(content: str) -> dict[str, object]:
    """Return the accepted JSON object or raise ``InvalidAnswer`` with every violation.

    The strings are exactly what the model must return: a markdown fence, prose around
    the JSON or an extra key is a violation in every structured-output mode.
    """
    decoded = _decode_object(content)
    errors = [
        _schema_error(error)
        for error in sorted(
            _review_validator().iter_errors(decoded), key=lambda item: item.json_path
        )
    ][:_MAX_ERRORS]
    if errors:
        raise InvalidAnswer(errors)
    normalized = _normalize_review_answer(decoded)
    try:
        parse_review_output(normalized)
    except InvalidReviewOutput as error:
        raise InvalidAnswer(_pydantic_errors(error)) from None
    return normalized


def _normalize_review_answer(decoded: dict[str, object]) -> dict[str, object]:
    """Repair lossless deviations, reporting semantic errors at raw finding indexes."""
    findings = cast(list[dict[str, object]], decoded["findings"])
    normalized_findings = []
    errors: list[str] = []
    for index, finding in enumerate(findings):
        normalized = dict(finding)
        line = normalized["line"]
        start_line = normalized["start_line"]
        if isinstance(line, int) and isinstance(start_line, int) and start_line == line:
            normalized["start_line"] = None
        try:
            ReviewFinding.model_validate(normalized)
        except ValidationError as error:
            errors.extend(_validation_messages(error, prefix=("findings", index)))
        normalized_findings.append(normalized)
    if errors:
        raise InvalidAnswer(errors[:_MAX_ERRORS])
    normalized_findings.sort(
        key=lambda finding: finding_order_key(
            cast(Severity, finding["severity"]), cast(float, finding["confidence"])
        )
    )
    return {**decoded, "findings": normalized_findings}


def parse_review_answer(content: str) -> ReviewOutput:
    """The typed result of an accepted answer."""
    return parse_review_output(validate_review_answer(content))


def conventions_answer_validator(changed_files: tuple[str, ...]) -> _ConventionsValidator:
    return _ConventionsValidator(changed_files)


class _ConventionsValidator:
    """``RepoConventionsDraft`` plus the rule that ``files`` lists exactly the PR paths."""

    def __init__(self, changed_files: tuple[str, ...]) -> None:
        self._changed_files = changed_files

    def __call__(self, content: str) -> dict[str, object]:
        decoded = _decode_object(content)
        try:
            draft = ConventionsDraft.model_validate(decoded)
        except ValidationError as error:
            raise InvalidAnswer(_validation_messages(error)) from None
        paths = tuple(item.path for item in draft.files)
        if paths != self._changed_files:
            raise InvalidAnswer(
                ["files: must list every path of <changed_files> once, in the same order"]
            )
        return decoded


@cache
def conventions_schema() -> dict[str, Any]:
    return ConventionsDraft.model_json_schema()


def _decode_object(content: str) -> dict[str, object]:
    try:
        decoded: Any = json.loads(content)
    except json.JSONDecodeError as error:
        raise InvalidAnswer([f"not valid JSON: {error.msg} at line {error.lineno}"]) from None
    if not isinstance(decoded, dict):
        raise InvalidAnswer(["the answer must be one JSON object"])
    return decoded


def _schema_error(error: Any) -> str:
    location = "/".join(str(part) for part in error.absolute_path) or "(root)"
    return _bounded(f"{location}: {error.message}")


def _pydantic_errors(error: InvalidReviewOutput) -> list[str]:
    cause = error.__cause__
    # A decoded object only fails Pydantic with a ValidationError (the JSON decoding,
    # the other cause of InvalidReviewOutput, already happened in _decode_object).
    assert isinstance(cause, ValidationError)
    return _validation_messages(cause)


def _validation_messages(
    error: ValidationError, *, prefix: tuple[str | int, ...] = ()
) -> list[str]:
    messages = []
    for item in error.errors()[:_MAX_ERRORS]:
        location = "/".join(str(part) for part in (*prefix, *item["loc"])) or "(root)"
        messages.append(_bounded(f"{location}: {item['msg']}"))
    return messages


def _bounded(message: str) -> str:
    """jsonschema echoes the offending value; a 20 000-character body must not."""
    if len(message) <= _MAX_ERROR_CHARS:
        return message
    # The location leads and the verdict ("is too long", "is not one of …") trails:
    # keep both ends, cut the echoed value in the middle.
    half = (_MAX_ERROR_CHARS - 1) // 2
    return message[:half] + "…" + message[-half:]
