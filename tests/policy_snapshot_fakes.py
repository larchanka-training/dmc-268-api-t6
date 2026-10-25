"""Test-only factories for policy documents and complete pinned run profiles."""

from __future__ import annotations

from hashlib import sha256
from typing import Literal
from uuid import UUID

from app.modules.reviews.application.policy_snapshot import (
    DEFAULT_MAX_POLICY_TOKENS,
    PINNED_PROMPT_CHECKSUMS,
    DocumentPresence,
    ExecutionProfile,
    PolicyDocument,
    PromptBindings,
)

_EMPTY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
_DEFAULT_REVIEW_SYSTEM_ID = UUID("00000000-0000-0000-0000-000000000001")
_DEFAULT_CONVENTIONS_ID = UUID("00000000-0000-0000-0000-000000000002")
_DEFAULT_VERIFIER_ID = UUID("00000000-0000-0000-0000-000000000003")


def make_policy_document_present(
    path: str,
    text: str,
    *,
    blob_oid: str = "d" * 40,
) -> PolicyDocument:
    """Create a present PolicyDocument with auto-calculated checksums and counts."""
    raw_bytes = text.encode("utf-8")
    raw_digest = sha256(raw_bytes).hexdigest()
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    text_digest = sha256(normalized.encode("utf-8")).hexdigest()
    line_count = normalized.count("\n") + (1 if normalized and not normalized.endswith("\n") else 0)
    return PolicyDocument(
        path=path,
        presence=DocumentPresence.PRESENT,
        blob_oid=blob_oid,
        raw_sha256=raw_digest,
        text_sha256=text_digest,
        text=normalized,
        byte_count=len(raw_bytes),
        line_count=line_count,
    )


def make_policy_document_missing(path: str) -> PolicyDocument:
    """Create a missing PolicyDocument with all content fields None."""
    return PolicyDocument(
        path=path,
        presence=DocumentPresence.MISSING,
        blob_oid=None,
        raw_sha256=None,
        text_sha256=None,
        text=None,
        byte_count=None,
        line_count=None,
    )


def make_policy_document_empty(path: str, *, blob_oid: str = "f" * 40) -> PolicyDocument:
    """Create an empty PolicyDocument (present with empty content)."""
    return PolicyDocument(
        path=path,
        presence=DocumentPresence.PRESENT,
        blob_oid=blob_oid,
        raw_sha256=_EMPTY_SHA256,
        text_sha256=_EMPTY_SHA256,
        text="",
        byte_count=0,
        line_count=0,
    )


def make_complete_prompt_bindings(
    *,
    review_system_id: UUID = _DEFAULT_REVIEW_SYSTEM_ID,
    conventions_id: UUID = _DEFAULT_CONVENTIONS_ID,
    verifier_id: UUID = _DEFAULT_VERIFIER_ID,
    review_system_checksum: str = PINNED_PROMPT_CHECKSUMS["review.system:v2"],
    conventions_checksum: str = PINNED_PROMPT_CHECKSUMS["review.conventions:v2"],
    verifier_checksum: str = PINNED_PROMPT_CHECKSUMS["review.verifier:v2"],
) -> PromptBindings:
    """Create complete PromptBindings with pinned IDs and checksums."""
    return PromptBindings(
        review_system_checksum=review_system_checksum,
        conventions_checksum=conventions_checksum,
        verifier_checksum=verifier_checksum,
        review_system_id=review_system_id,
        conventions_id=conventions_id,
        verifier_id=verifier_id,
    )


def make_complete_execution_profile(
    profile_name: Literal["fast", "deep"] = "fast",
    *,
    review_model: str = "mistral-small-4",
    verifier_model: str = "mistral-small-4",
    conventions_model: str | None = None,
    primary_provider_route: str = "primary",
    fallback_provider_route: str | None = None,
    tokenizer_version: str = "tokenizer/v1",
    counter_version: str = "counter/v2",
    temperature: str = "0.0",
    response_schema_version: str = "review-output/v2",
    recovery_profile_version: str = "recovery/v1",
    budget_profile_version: str = "budget/v1",
) -> ExecutionProfile:
    """Create complete ExecutionProfile with all required fields set."""
    max_input = 150_000 if profile_name == "deep" else 60_000
    return ExecutionProfile(
        profile_name=profile_name,
        review_model=review_model,
        verifier_model=verifier_model,
        conventions_model=conventions_model,
        max_policy_tokens=DEFAULT_MAX_POLICY_TOKENS,
        max_input_tokens=max_input,
        primary_provider_route=primary_provider_route,
        fallback_provider_route=fallback_provider_route,
        tokenizer_version=tokenizer_version,
        counter_version=counter_version,
        temperature=temperature,
        response_schema_version=response_schema_version,
        recovery_profile_version=recovery_profile_version,
        budget_profile_version=budget_profile_version,
    )
