"""Canonical identity of a cached review or verifier model answer.

The digest is SHA-256 over canonical JSON in the ``review-result-v1`` namespace.
Callers pass ``rendered_messages_sha256`` of the original provider messages as
``rendered_input_digest``, and the SHA-256 of the provider body without those
messages as ``request_options_digest``. A stored value is the accepted answer
plus the run that accepted it. ``ReviewResultCache`` is the storage port. This
module does not open a Redis connection and does not call the gateway.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
from typing import Any, Protocol, cast
from uuid import UUID

REVIEW_RESULT_NAMESPACE = "review-result-v1"
REVIEW_RESULT_KEY_PREFIX = "review-result:"


class ReviewResultOperation(StrEnum):
    """A cacheable gateway operation. Conventions is never part of this key."""

    REVIEW = "review"
    VERIFICATION = "verification"


@dataclass(frozen=True)
class ReviewResultModel:
    """Provider and model id of the primary route the lookup is for."""

    provider: str
    model_id: str


@dataclass(frozen=True)
class ReviewResultIdentity:
    """The fields that decide whether two calls share a cached model answer."""

    operation: ReviewResultOperation
    repository_id: UUID
    model: ReviewResultModel
    prompt_version: str
    output_schema_version: str
    profile_version: str
    rendered_input_digest: str
    request_options_digest: str


@dataclass(frozen=True)
class ReviewResultSource:
    """The run whose accepted call produced a cached answer."""

    run_id: UUID
    call_id: UUID | None
    manifest_id: UUID | None


def review_result_entry(answer: dict[str, Any], source: ReviewResultSource) -> dict[str, Any]:
    """The Redis value: the accepted answer and the call that produced it."""
    return {
        "answer": answer,
        "source": {
            "run_id": str(source.run_id),
            "call_id": None if source.call_id is None else str(source.call_id),
            "manifest_id": None if source.manifest_id is None else str(source.manifest_id),
        },
    }


def stored_review_result(
    value: dict[str, Any],
) -> tuple[dict[str, Any], ReviewResultSource] | None:
    """The answer and its source, or None when ``value`` is not that envelope."""
    answer = value.get("answer")
    source = value.get("source")
    if not isinstance(answer, dict) or not isinstance(source, dict):
        return None
    run_ok, run_id = _parse_id(source.get("run_id"), required=True)
    call_ok, call_id = _parse_id(source.get("call_id"), required=False)
    manifest_ok, manifest_id = _parse_id(source.get("manifest_id"), required=False)
    if not (run_ok and call_ok and manifest_ok) or run_id is None:
        return None
    return (
        cast(dict[str, Any], answer),
        ReviewResultSource(run_id=run_id, call_id=call_id, manifest_id=manifest_id),
    )


def _parse_id(value: object, *, required: bool) -> tuple[bool, UUID | None]:
    """Whether ``value`` is a UUID string, or None when an optional id is absent."""
    if value is None:
        return (not required, None)
    if not isinstance(value, str):
        return (False, None)
    try:
        return (True, UUID(value))
    except ValueError:
        return (False, None)


def review_result_digest(identity: ReviewResultIdentity) -> str:
    """SHA-256 hex of the canonical ``review-result-v1`` identity."""
    payload = {
        "namespace": REVIEW_RESULT_NAMESPACE,
        "operation": identity.operation,
        "repository_id": str(identity.repository_id),
        "model": {
            "provider": identity.model.provider,
            "model_id": identity.model.model_id,
        },
        "prompt_version": identity.prompt_version,
        "output_schema_version": identity.output_schema_version,
        "profile_version": identity.profile_version,
        "rendered_input_digest": identity.rendered_input_digest,
        "request_options_digest": identity.request_options_digest,
    }
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return sha256(canonical.encode("utf-8")).hexdigest()


def review_result_cache_key(identity: ReviewResultIdentity) -> str:
    """Redis key ``review-result:`` plus the identity digest."""
    return f"{REVIEW_RESULT_KEY_PREFIX}{review_result_digest(identity)}"


class ReviewResultCache(Protocol):
    """Accepted model answer by review-result key.

    A missing key, a bad entry, or a backend failure is a miss (``None``) or a
    dropped write. Neither operation raises to the caller.
    """

    async def get(self, key: str) -> dict[str, Any] | None:
        """Return the stored JSON object, or None when the entry cannot be used."""
        ...

    async def set(self, key: str, value: dict[str, Any]) -> None:
        """Store ``value``. An oversize value or a backend failure is dropped."""
        ...
