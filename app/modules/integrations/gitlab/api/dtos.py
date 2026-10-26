"""Strict bounded GitLab transport decoding; keep the original JSON for JSONB."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

BigId = Annotated[int, Field(strict=True, gt=0, le=9223372036854775807)]
Iid = Annotated[int, Field(strict=True, gt=0, le=2147483647)]
Action = Annotated[str, Field(strict=True, min_length=1, max_length=100)]


class GitLabProjectDto(BaseModel):
    model_config = ConfigDict(strict=True)
    id: BigId


class GitLabMergeRequestAttributesDto(BaseModel):
    model_config = ConfigDict(strict=True)
    id: BigId
    iid: Iid
    action: Action
    target_project_id: BigId | None = None


class GitLabMergeRequestDto(BaseModel):
    model_config = ConfigDict(strict=True)
    object_kind: Literal["merge_request"]
    project: GitLabProjectDto
    object_attributes: GitLabMergeRequestAttributesDto
    event_type: Literal["merge_request"] | None = None


class GitLabPayloadInvalid(ValueError):
    def __init__(self) -> None:
        super().__init__("gitlab_webhook_invalid_request")


def _pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise GitLabPayloadInvalid()
        result[key] = value
    return result


def _constant(value: str) -> object:
    raise GitLabPayloadInvalid()


def _validate_number_lexeme(value: str) -> None:
    # JSONB maps numbers to PostgreSQL numeric: at most 131072 integer digits
    # and 16383 fractional digits. Scale includes written trailing zeroes.
    # Inspect the lexeme, since float("1e-100000") silently becomes 0.0.
    mantissa, marker, exponent_text = value.lower().partition("e")
    exponent = 0
    if marker:
        digits = exponent_text.lstrip("+-").lstrip("0")
        # Bound conversion work independently of Python's integer digit guard.
        if len(digits) > 10:
            raise GitLabPayloadInvalid()
        exponent = int(digits or "0")
        if exponent > 1073741823:
            raise GitLabPayloadInvalid()
        if exponent_text.startswith("-"):
            exponent = -exponent
    integer, _, fraction = mantissa.lstrip("-").partition(".")
    if max(0, len(fraction) - exponent) > 16383:
        raise GitLabPayloadInvalid()
    coefficient = integer + fraction
    leading_zeroes = len(coefficient) - len(coefficient.lstrip("0"))
    if leading_zeroes < len(coefficient) and len(integer) + exponent - leading_zeroes > 131072:
        raise GitLabPayloadInvalid()


def _float(value: str) -> float:
    _validate_number_lexeme(value)
    number = float(value)
    if not math.isfinite(number):
        raise GitLabPayloadInvalid()
    return number


def _validate_tree(payload: dict[str, object]) -> None:
    pending: list[tuple[object, int]] = [(payload, 1)]
    while pending:
        value, depth = pending.pop()
        if isinstance(value, str):
            if "\x00" in value or any(0xD800 <= ord(char) <= 0xDFFF for char in value):
                raise GitLabPayloadInvalid()
        elif isinstance(value, (dict, list)):
            if depth > 128:
                raise GitLabPayloadInvalid()
            if isinstance(value, dict):
                pending.extend((key, depth) for key in value)
                pending.extend((child, depth + 1) for child in value.values())
            else:
                pending.extend((child, depth + 1) for child in value)


@dataclass(frozen=True)
class ParsedGitLabPayload:
    original_json: str
    payload: dict[str, object]
    merge_request: GitLabMergeRequestDto | None


def parse_gitlab_payload(body: bytes, *, event_name: str) -> ParsedGitLabPayload:
    """Validate transport/MR identity without serializing parsed floating point values."""
    try:
        original = body.decode("utf-8")
        payload = json.loads(
            original, object_pairs_hook=_pairs, parse_float=_float, parse_constant=_constant
        )
        if not isinstance(payload, dict):
            raise GitLabPayloadInvalid()
        _validate_tree(payload)
        mr = None
        if event_name == "Merge Request Hook":
            mr = GitLabMergeRequestDto.model_validate(payload)
            attributes = mr.object_attributes
            if (
                "target_project_id" in attributes.model_fields_set
                and attributes.target_project_id != mr.project.id
            ) or ("event_type" in mr.model_fields_set and mr.event_type != "merge_request"):
                raise GitLabPayloadInvalid()
        return ParsedGitLabPayload(original, payload, mr)
    except (ValueError, RecursionError, ValidationError):
        raise GitLabPayloadInvalid() from None


def decode_gitlab_payload(body: bytes, *, event_name: str) -> str:
    return parse_gitlab_payload(body, event_name=event_name).original_json
