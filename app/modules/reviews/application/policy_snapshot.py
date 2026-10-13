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
from hashlib import sha256
from typing import Literal
from uuid import UUID

POLICY_SNAPSHOT_SCHEMA_VERSION = 1
RULES_DOCUMENT_PATH = ".review/rules.md"
AGENTS_DOCUMENT_PATH = "AGENTS.md"

_EMPTY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"

type StageName = Literal["conventions", "review", "verifier", "repair", "fallback"]
type SourceRole = Literal[
    "policy_document",
    "service_defaults",
    "conventions",
    "pr_metadata",
    "diff",
    "source_excerpt",
    "ast_excerpt",
    "candidate",
    "previous_output",
]


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

    @property
    def is_present(self) -> bool:
        return self.presence is DocumentPresence.PRESENT

    @property
    def is_missing(self) -> bool:
        return self.presence is DocumentPresence.MISSING


def make_policy_document_present(
    path: str,
    text: str,
    *,
    blob_oid: str = "d" * 40,
) -> PolicyDocument:
    """Create a present PolicyDocument with auto-calculated checksums and counts."""
    digest = sha256(text.encode("utf-8")).hexdigest()
    return PolicyDocument(
        path=path,
        presence=DocumentPresence.PRESENT,
        blob_oid=blob_oid,
        raw_sha256=digest,
        text_sha256=digest,
        text=text,
        byte_count=len(text.encode("utf-8")),
        line_count=len(text.splitlines()),
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

    @property
    def has_custom_rules(self) -> bool:
        return self.rules.presence is DocumentPresence.PRESENT and bool(
            (self.rules.text or "").strip()
        )

    @property
    def has_agents_doc(self) -> bool:
        return self.agents.presence is DocumentPresence.PRESENT and bool(
            (self.agents.text or "").strip()
        )

    @property
    def is_ready_for_llm(self) -> bool:
        return bool(
            self.policy_digest
            and self.prompt_bindings.review_system_checksum
            and self.prompt_bindings.conventions_checksum
            and self.prompt_bindings.verifier_checksum
        )


@dataclass(frozen=True)
class ManifestBlock:
    """One rendered block in a call manifest with exact coordinate boundaries."""

    block_id: str
    source_role: SourceRole
    source_kind: str
    path: str | None = None
    revision: str | None = None
    content_sha256: str | None = None
    start_line: int | None = None
    end_line: int | None = None
    shown_line_count: int = 0


@dataclass(frozen=True)
class CallManifest:
    """The exact record of blocks and coordinate ranges presented to an LLM call.

    Used by attribution and verification passes to validate evidence and requirement references.
    """

    call_id: UUID
    run_id: UUID
    stage: StageName
    blocks: tuple[ManifestBlock, ...]

    def get_block(self, block_id: str) -> ManifestBlock | None:
        for block in self.blocks:
            if block.block_id == block_id:
                return block
        return None

    def is_range_shown(self, block_id: str, start_line: int, end_line: int) -> bool:
        block = self.get_block(block_id)
        if block is None:
            return False
        if block.start_line is None or block.end_line is None:
            return False
        return block.start_line <= start_line and end_line <= block.end_line
