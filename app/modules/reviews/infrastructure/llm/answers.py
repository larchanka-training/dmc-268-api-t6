"""Validation of a model answer: the gateway's parse path (docs/PIPELINE_SPEC.md §9).

A review answer must pass ``review/schemas/review-output.schema.json`` and then
``parse_review_output``; the collected messages feed the one repair call (§5.1). There
is no second copy of the schema: the file is read once and sent to the provider as is,
minus its ``$``-annotations and with a Python-only end anchor in a ``pattern`` written as
``$`` (``provider_schema``). A run whose snapshot binds review prompt ``v4`` or ``v5`` is
held to ``review-output.schema.v2.json`` instead (``review_answer_contract``).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any, cast

from jsonschema import Draft202012Validator
from pydantic import BaseModel, ValidationError

from app.modules.reviews.application.conventions import ConventionsDraft
from app.modules.reviews.application.policy_errors import PolicyError, PolicyErrorCode
from app.modules.reviews.application.reflection_pass import VerificationOutput
from app.modules.reviews.application.review_output import (
    InvalidReviewOutput,
    ReviewFinding,
    ReviewFindingV2,
    ReviewOutput,
    Severity,
    finding_order_key,
    parse_review_output,
    parse_review_output_v2,
)
from app.modules.reviews.application.review_path import SNAPSHOT_REVIEW_PROMPT_VERSIONS

REVIEW_OUTPUT_SCHEMA_PATH = (
    Path(__file__).resolve().parents[5] / "review" / "schemas" / "review-output.schema.json"
)
REVIEW_OUTPUT_V2_SCHEMA_PATH = (
    Path(__file__).resolve().parents[5] / "review" / "schemas" / "review-output.schema.v2.json"
)
VERIFIER_OUTPUT_SCHEMA_PATH = (
    Path(__file__).resolve().parents[5] / "review" / "schemas" / "verifier-output.schema.v1.json"
)
_ANNOTATIONS = frozenset({"$schema", "$id", "$comment"})
# Python's ``$`` also matches before a final newline, so a schema file ends a ``pattern``
# with this lookahead; ECMA-262 ``$`` without the ``m`` flag already means the end of input.
_PYTHON_END_ANCHOR = r"(?![\s\S])"
# The review answer schema follows the bound review prompt version (#130 decision 9).
_V1_REVIEW_PROMPT_VERSIONS = frozenset({"v1", "v2", "v3"})
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
    """The schema for ``response_format``: a copy without ``$``-annotations at any level.

    A ``pattern`` that ends with the Python-only end anchor ``(?![\\s\\S])`` ends with ``$``
    instead: the provider reads the ECMA-262 dialect, where that is the same anchor, and may
    reject a lookahead (#130). Local validation keeps the file's own pattern.
    """
    stripped: Any = _strip_annotations(schema)
    return dict(stripped)


def _strip_annotations(node: Any) -> Any:
    # A ``pattern`` keyword is a string; a property named ``pattern`` is a sub-schema.
    if isinstance(node, Mapping):
        return {
            key: (
                _ecma_end_anchor(value)
                if key == "pattern" and isinstance(value, str)
                else _strip_annotations(value)
            )
            for key, value in node.items()
            if key not in _ANNOTATIONS
        }
    if isinstance(node, list):
        return [_strip_annotations(item) for item in node]
    return node


def _ecma_end_anchor(pattern: str) -> str:
    if pattern.endswith(_PYTHON_END_ANCHOR):
        return pattern.removesuffix(_PYTHON_END_ANCHOR) + "$"
    return pattern


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


@cache
def review_output_schema_v2() -> dict[str, Any]:
    """The ReviewOutput v2 shape with ``evidence_refs`` (draft 2020-12, CVS §6.1)."""
    loaded: dict[str, Any] = json.loads(REVIEW_OUTPUT_V2_SCHEMA_PATH.read_text(encoding="utf-8"))
    return loaded


@cache
def _review_v2_validator() -> Draft202012Validator:
    return Draft202012Validator(review_output_schema_v2())


def validate_review_answer_v2(content: str) -> dict[str, object]:
    """``validate_review_answer`` against the v2 schema, ending in ``parse_review_output_v2``.

    Only a v4- or v5-bound (snapshot-path) run is held to v2, so it decodes fail-closed.
    """
    decoded = _decode_object_fail_closed(content)
    errors = [
        _schema_error(error)
        for error in sorted(
            _review_v2_validator().iter_errors(decoded), key=lambda item: item.json_path
        )
    ][:_MAX_ERRORS]
    if errors:
        raise InvalidAnswer(errors)
    normalized = _normalize_review_answer(decoded, finding_model=ReviewFindingV2)
    try:
        parse_review_output_v2(normalized)
    except InvalidReviewOutput as error:
        raise InvalidAnswer(_pydantic_errors(error)) from None
    return normalized


@dataclass(frozen=True)
class ReviewAnswerContract:
    """The schema the provider gets for a review answer and the validator it must pass."""

    name: str
    schema: dict[str, Any]
    validate: Callable[[str], dict[str, object]]


def review_answer_contract(review_system_version: str | None) -> ReviewAnswerContract:
    """Pick the review answer contract by the bound review prompt version.

    No snapshot (``None``) or ``v1``-``v3``: ReviewOutput v1, the live contract. ``v4`` and
    ``v5``: ReviewOutput v2 with ``evidence_refs``. Any other version has no known answer
    shape, so the run fails before a provider call.
    """
    if review_system_version is None or review_system_version in _V1_REVIEW_PROMPT_VERSIONS:
        return ReviewAnswerContract("ReviewOutput", review_output_schema(), validate_review_answer)
    if review_system_version in SNAPSHOT_REVIEW_PROMPT_VERSIONS:
        return ReviewAnswerContract(
            "ReviewOutputV2", review_output_schema_v2(), validate_review_answer_v2
        )
    raise PolicyError(
        PolicyErrorCode.SNAPSHOT_INTEGRITY_ERROR,
        f"Unknown review prompt version '{review_system_version}' in prompt bindings: "
        "no review answer schema is bound to it",
    )


def _normalize_review_answer(
    decoded: dict[str, object], *, finding_model: type[BaseModel] = ReviewFinding
) -> dict[str, object]:
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
            finding_model.model_validate(normalized)
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


@cache
def verifier_output_schema() -> dict[str, Any]:
    """The VerificationOutput shape of the Reflection Pass verifier (draft 2020-12)."""
    loaded: dict[str, Any] = json.loads(VERIFIER_OUTPUT_SCHEMA_PATH.read_text(encoding="utf-8"))
    return loaded


@cache
def _verifier_validator() -> Draft202012Validator:
    return Draft202012Validator(verifier_output_schema())


def validate_verifier_answer(content: str) -> dict[str, object]:
    """Return the accepted verifier JSON object or raise ``InvalidAnswer``.

    The schema and ``VerificationOutput`` errors earn one repair call. The candidate-ID and
    reference checks against the call's manifest follow as the task's ``check_answer`` and
    earn the same recovery (``GatewayVerifierModel``, #130 tech lead 2026-10-28).
    """
    decoded = _decode_object_fail_closed(content)
    errors = [
        _schema_error(error)
        for error in sorted(
            _verifier_validator().iter_errors(decoded), key=lambda item: item.json_path
        )
    ][:_MAX_ERRORS]
    if errors:
        raise InvalidAnswer(errors)
    try:
        VerificationOutput.model_validate(decoded)
    except ValidationError as error:
        raise InvalidAnswer(_validation_messages(error)) from None
    return decoded


def verifier_answer_without_reasons(content: str) -> str:
    """The verifier answer as its ``llm.call`` stores it: without ``decisions[].reason``.

    E-2 (TL-3), fail-closed (decided by the tech lead on 2026-10-25): a valid answer is
    stored as the allow-list of each decision, ``candidate_id``, ``status`` and the refs;
    any other text is withheld whole, since a reason inside it cannot be found reliably.
    """
    try:
        output = VerificationOutput.model_validate(validate_verifier_answer(content))
    except (RecursionError, ValueError):
        # InvalidAnswer is a ValueError; nothing the text makes the decoder raise is stored.
        return f"[withheld: {len(content)} characters that are not a valid verifier answer]"
    allowed = {"candidate_id", "status", "evidence_refs", "requirement_refs"}
    stored = output.model_dump(mode="json", include={"decisions": {"__all__": allowed}})
    return json.dumps(stored, ensure_ascii=False)


def conventions_answer_validator(
    changed_files: tuple[str, ...], *, fail_closed: bool = False
) -> _ConventionsValidator:
    """``fail_closed``: the snapshot path's decoder; legacy conventions keep ``_decode_object``."""
    return _ConventionsValidator(changed_files, fail_closed=fail_closed)


class _ConventionsValidator:
    """``RepoConventionsDraft`` plus the rule that ``files`` lists exactly the PR paths."""

    def __init__(self, changed_files: tuple[str, ...], *, fail_closed: bool = False) -> None:
        self._changed_files = changed_files
        self._decode = _decode_object_fail_closed if fail_closed else _decode_object

    def __call__(self, content: str) -> dict[str, object]:
        decoded = self._decode(content)
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


def _decode_object_fail_closed(content: str) -> dict[str, object]:
    """``_decode_object`` that also refuses what the decoder itself cannot read: nesting past
    the recursion limit and an integer past the digit limit (re-review of 172c8c5c).

    Both become ``InvalidAnswer``, so the gateway repairs them (and the stored verifier
    ``llm.call`` withholds the text), instead of an error escaping after the call is paid.
    Snapshot path only: the verifier, the v2 review and snapshot-path conventions. The legacy
    v1 review and legacy conventions keep ``_decode_object`` as live runs use it (F1).
    """
    try:
        return _decode_object(content)
    except InvalidAnswer:
        raise
    except RecursionError:
        raise InvalidAnswer(["not valid JSON: nested too deeply"]) from None
    except ValueError:
        raise InvalidAnswer(["not valid JSON: a number has too many digits"]) from None


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


def bounded_errors(errors: list[str]) -> list[str]:
    """The validators' own caps for any error list: 20 messages of at most 300 characters."""
    return [_bounded(message) for message in errors[:_MAX_ERRORS]]


def _bounded(message: str) -> str:
    """jsonschema echoes the offending value; a 20 000-character body must not."""
    if len(message) <= _MAX_ERROR_CHARS:
        return message
    # The location leads and the verdict ("is too long", "is not one of …") trails:
    # keep both ends, cut the echoed value in the middle.
    half = (_MAX_ERROR_CHARS - 1) // 2
    return message[:half] + "…" + message[-half:]
