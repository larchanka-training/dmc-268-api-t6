"""Immutable policy snapshot handed from the policy loader to the LLM pipeline.

Shape of RULES_FORMAT_SPEC §7.1 and §7.4. This module is the contract only: loading,
validation and storage arrive with the loader. ``PromptBindings`` and
``ExecutionProfile`` are defined by the LLM pipeline and verification spec;
they live here so that the snapshot has one description.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Literal
from uuid import UUID

from app.modules.reviews.application.context_provenance import SourceRole as SourceRole
from app.modules.reviews.application.verdict import SeverityThresholdValue

POLICY_SNAPSHOT_SCHEMA_VERSION = 1
DEFAULT_MAX_POLICY_TOKENS = 12_000
RULES_DOCUMENT_PATH = ".review/rules.md"
AGENTS_DOCUMENT_PATH = "AGENTS.md"


type StageName = Literal["conventions", "review", "verification"]
type RecoveryKind = Literal["none", "retry", "repair", "fallback"]


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
    verifier_version: str = "v2"
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
    max_policy_tokens: int = DEFAULT_MAX_POLICY_TOKENS
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


PINNED_PROMPT_CHECKSUMS: dict[str, str] = {
    "review.system:v2": "9177e358c23d1a7cd8987cbf667cf8b705c2afb27eaf8b2237480e4531431d75",
    "review.system:v4": "5738227c7352228d29975e0512b5b5d3354eff31c9d161ca72086a776e9c498f",
    "review.conventions:v2": "225a8f7d56835cf1307ddb25feac602c3d10e03c34ee51afc90a1f99af101454",
    "review.verifier:v1": "e00128b211ca64c9a2dd2bb902ade9abf4a29f30c7ec9066501dc7694ddc5940",
    "review.verifier:v2": "1f676b4fea8477d77b83a106a6c62e78f67d19f71937d4a02093d11a9d276088",
}


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
    severity_threshold: SeverityThresholdValue | None = None
    max_comments: int | None = None

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
            and self.prompt_bindings.is_complete
            and self.execution_profile.is_complete
        )


@dataclass(frozen=True)
class ManifestBlock:
    """One rendered block in a call manifest with exact coordinate boundaries."""

    block_id: str
    source_role: SourceRole
    source_kind: str
    path: str | None = None
    revision: str | None = None
    side: Literal["LEFT", "RIGHT"] | None = None
    content_sha256: str | None = None
    start_line: int | None = None
    end_line: int | None = None
    shown_line_count: int = 0
    shown_ranges: tuple[tuple[int, int], ...] = ()
    # Tokens of the rendered block, counted with the call's pinned counter; None when the
    # builder of this manifest does not count (#144 reads them per role).
    tokens: int | None = None
    # The repository a code block was read from; None for policy and generated blocks.
    repository_id: UUID | None = None
    # The block shows a prefix of its source, ending with the truncation trailer.
    truncated: bool = False
    # The selection metadata of a retrieved block, next to its source (ADR 0001 §9): why it
    # was chosen, the commit of the index that chose it and the edge it was reached by
    # (``EdgeKind`` value). None for every block that was not retrieved.
    selection_reason: str | None = None
    index_commit_sha: str | None = None
    edge_kind: str | None = None


@dataclass(frozen=True)
class CallManifest:
    """The exact record of blocks and coordinate ranges presented to an LLM call.

    Used by attribution and verification passes to validate evidence and requirement references.
    """

    call_id: UUID
    stage: StageName
    blocks: tuple[ManifestBlock, ...]
    recovery_kind: RecoveryKind = "none"
    run_id: UUID | None = None

    def get_block(self, block_id: str) -> ManifestBlock | None:
        for block in self.blocks:
            if block.block_id == block_id:
                return block
        return None

    def is_range_shown(self, block_id: str, start_line: int, end_line: int) -> bool:
        block = self.get_block(block_id)
        if block is None:
            return False
        if block.shown_ranges:
            return any(
                r_start <= start_line and end_line <= r_end for r_start, r_end in block.shown_ranges
            )
        if block.start_line is None or block.end_line is None:
            return False
        return block.start_line <= start_line and end_line <= block.end_line
