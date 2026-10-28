"""Canonical identity of a cached review or verifier model answer.

The digest is SHA-256 over canonical JSON in the ``review-result-v1`` namespace.
Callers pass ``rendered_messages_sha256`` of the original provider messages as
``rendered_input_digest``. ``ReviewResultCache`` is the storage port. This module
does not open a Redis connection and does not call the gateway.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
from typing import Any, Protocol
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
