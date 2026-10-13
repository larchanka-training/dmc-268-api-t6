"""Immutable policy snapshot handed from the policy loader to the LLM pipeline.

Shape of RULES_FORMAT_SPEC §7.1 and §7.4. This module is the contract only: loading,
validation and storage arrive with the loader. ``PromptBindings`` and
``ExecutionProfile`` are the part of the contract defined by the LLM pipeline (#120);
they live here so that the snapshot has one description.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Literal
from uuid import UUID

POLICY_SNAPSHOT_SCHEMA_VERSION = 1
RULES_DOCUMENT_PATH = ".review/rules.md"
AGENTS_DOCUMENT_PATH = "AGENTS.md"


class DocumentPresence(StrEnum):
    PRESENT = "present"
    MISSING = "missing"


@dataclass(frozen=True)
class PolicyDocument:
    """One policy document of the pinned base revision.

    An empty file is present with the checksums of empty content; a missing file has no
    blob, checksums or text, so the two never share an identity.
    """

    path: str
    presence: DocumentPresence
    blob_oid: str | None
    raw_sha256: str | None
    text_sha256: str | None
    text: str | None
    byte_count: int | None
    line_count: int | None

    def __post_init__(self) -> None:
        content = (
            self.blob_oid,
            self.raw_sha256,
            self.text_sha256,
            self.text,
            self.byte_count,
            self.line_count,
        )
        if self.presence is DocumentPresence.MISSING and any(item is not None for item in content):
            raise ValueError("a missing policy document carries no content")
        if self.presence is DocumentPresence.PRESENT and any(item is None for item in content):
            raise ValueError("a present policy document carries its full content")


@dataclass(frozen=True)
class PromptBindings:
    """Pinned prompts of the three review pipeline stages (RULES_FORMAT_SPEC §7.1).

    Each prompt is pinned by the immutable id of its stored version and by its checksum.
    Bound when the run is created, before any VCS read. A value stays empty until the
    run pins that prompt, and ``is_complete`` is false until all three are pinned.
    """

    review_system_checksum: str
    conventions_checksum: str
    verifier_checksum: str
    review_system_key: str = "review.system"
    review_system_version: str = "v2"
    conventions_key: str = "review.conventions"
    conventions_version: str = "v2"
    verifier_key: str = "review.verifier"
    verifier_version: str = "v1"
    review_system_id: UUID | None = None
    conventions_id: UUID | None = None
    verifier_id: UUID | None = None

    @property
    def is_complete(self) -> bool:
        checksums = (self.review_system_checksum, self.conventions_checksum, self.verifier_checksum)
        ids = (self.review_system_id, self.conventions_id, self.verifier_id)
        return all(checksums) and all(item is not None for item in ids)

    def pins_review_prompt(self, prompt_version_id: UUID) -> bool:
        """Whether the review prompt is the one the run pinned as ``prompt_version_id``."""
        return self.review_system_id == prompt_version_id


@dataclass(frozen=True)
class ExecutionProfile:
    """Everything about model execution that stays the same on a retry (RULES §7.1).

    Primary and fallback model ids, provider routes, tokenizer and counter versions,
    temperature, the response schema and the recovery and budget profiles. A value that
    the run has not pinned yet is ``None``; ``is_complete`` tells when none is missing.
    A stage without a fallback model keeps its fallback ``None``.
    """

    profile_name: Literal["fast", "deep"]
    review_model: str
    verifier_model: str
    conventions_model: str | None = None
    max_policy_tokens: int = 12_000
    max_input_tokens: int = 60_000
    max_calls_per_run: int = 4
    fallback_review_model: str | None = None
    fallback_verifier_model: str | None = None
    fallback_conventions_model: str | None = None
    primary_provider_route: str | None = None
    fallback_provider_route: str | None = None
    tokenizer_version: str | None = None
    counter_version: str | None = None
    # A decimal string, as canonical policy JSON carries configuration numbers.
    temperature: str | None = None
    response_schema_version: str | None = None
    recovery_profile_version: str | None = None
    budget_profile_version: str | None = None

    def __post_init__(self) -> None:
        if self.temperature is not None:
            try:
                Decimal(self.temperature)
            except InvalidOperation as exc:
                raise ValueError("temperature must be a decimal string") from exc

    @property
    def is_complete(self) -> bool:
        required = (
            self.review_model,
            self.verifier_model,
            self.primary_provider_route,
            self.tokenizer_version,
            self.counter_version,
            self.temperature,
            self.response_schema_version,
            self.recovery_profile_version,
            self.budget_profile_version,
        )
        return all(required)

    @classmethod
    def fast(
        cls, review_model: str, verifier_model: str, conventions_model: str | None = None
    ) -> ExecutionProfile:
        return cls("fast", review_model, verifier_model, conventions_model)

    @classmethod
    def deep(
        cls, review_model: str, verifier_model: str, conventions_model: str | None = None
    ) -> ExecutionProfile:
        return cls(
            "deep", review_model, verifier_model, conventions_model, max_input_tokens=150_000
        )


@dataclass(frozen=True)
class PolicySnapshot:
    """The policy of one run: both documents, the ignore policy and every pinned version."""

    schema_version: int
    snapshot_id: UUID
    run_id: UUID
    repository_id: UUID
    policy_base_sha: str
    rules: PolicyDocument
    agents: PolicyDocument
    ignore_patterns: tuple[str, ...]
    ignore_digest: str
    parser_profile: str
    parser_version: str
    matcher_version: str
    limits_version: str
    builtin_exclusions_version: str
    priority_matcher_version: str
    service_defaults_rule_version_id: UUID
    defaults_checksum: str
    prompt_bindings: PromptBindings
    execution_profile: ExecutionProfile
    policy_digest: str
    # When the snapshot was stored; it is not part of ``policy_digest``.
    created_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.rules.path != RULES_DOCUMENT_PATH or self.agents.path != AGENTS_DOCUMENT_PATH:
            raise ValueError("policy snapshot documents are bound to their fixed paths")
