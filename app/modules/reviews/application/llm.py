"""Provider-neutral contracts of one run attempt's LLM calls (docs/PIPELINE_SPEC.md §2-§6).

The worker binds a ``RunCallContext`` when it claims a run; the gateway uses it to check
the attempt deadline, the run cost limit and to write the ``llm.call`` trace and the
``usage_events`` rows through the ports below. No vendor SDK or HTTP detail lives here.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from hashlib import sha256
from typing import Any, Literal, Protocol
from uuid import UUID

from app.common.application.unit_of_work import UnitOfWork
from app.modules.reviews.application.policy_snapshot import (
    CallManifest,
    ExecutionProfile,
    PromptBindings,
    StageName,
)
from app.modules.reviews.application.prepare_policy_snapshot import pinned_to_json
from app.modules.reviews.application.run_failures import RETRYABLE_ERROR_CODES

type EngineName = Literal["fast", "deep"]


class LlmErrorCode(StrEnum):
    """The LLM part of the ``error_code`` catalog (docs/PIPELINE_SPEC.md §6)."""

    TIMEOUT = "llm_timeout"
    RATE_LIMITED = "llm_rate_limited"
    PAYMENT_REQUIRED = "llm_payment_required"
    UNAVAILABLE = "llm_unavailable"
    INVALID_OUTPUT = "llm_invalid_output"
    CONTEXT_OVERFLOW = "llm_context_overflow"
    BUDGET_EXCEEDED = "budget_exceeded"
    DEADLINE_EXCEEDED = "deadline_exceeded"
    # E-7: a snapshot-path call short of its timeout plus the time its task keeps for the
    # verifier; terminal, no unverified review is published (PIPELINE_SPEC §6).
    VERIFICATION_DEADLINE_EXCEEDED = "verification_deadline_exceeded"


class LlmCallKind(StrEnum):
    """Why a provider call was made; one ``llm.call`` record per call (§2)."""

    PRIMARY = "primary"
    RETRY = "retry"
    REPAIR = "repair"
    FALLBACK = "fallback"


@dataclass(frozen=True)
class LlmUsage:
    """Usage of one provider call; ``model`` is the model the provider answered with."""

    provider: str
    model: str
    operation: str
    tokens_in: int
    tokens_out: int
    cache_read_tokens: int
    cost_usd: Decimal


class LlmCallFailed(Exception):
    """A normalized gateway failure; the message never carries a key or a prompt."""

    def __init__(
        self,
        error_code: LlmErrorCode,
        message: str,
        *,
        usage: tuple[LlmUsage, ...] = (),
        calls: int = 0,
        retry_after_s: float | None = None,
    ) -> None:
        super().__init__(f"{error_code.value}: {message}")
        self.error_code = error_code
        self.usage = usage
        self.calls = calls
        # The provider Retry-After (#130): the longest of the gateway call's 429s when the
        # final error is a rate limit, else the last call's. ``classify_failure`` makes it
        # the run retry delay of an ``llm_rate_limited`` failure, up to one day (#247).
        self.retry_after_s = retry_after_s

    @property
    def run_retryable(self) -> bool:
        """Whether the worker may retry the run (T9) for this class."""
        return self.error_code.value in RETRYABLE_ERROR_CODES


@dataclass(frozen=True)
class RunCallContext:
    """What the worker knows about the claimed attempt (#34) and the gateway needs."""

    run_id: UUID
    workspace_id: UUID
    attempt: int
    engine: EngineName
    deadline: datetime
    prompt_version_id: UUID | None = None
    rule_version_id: UUID | None = None


@dataclass(frozen=True)
class LlmCallError:
    """The error half of an ``llm.call`` response: ``{error: {class, http_status, message}}``."""

    error_class: LlmErrorCode
    http_status: int | None
    message: str


@dataclass(frozen=True)
class FxProvenance:
    """The public ECB quote metadata attached to one provider call."""

    source: str
    observation_date: date
    rate_usd_per_eur: Decimal
    stale_cache: bool

    def as_json(self) -> dict[str, str | bool]:
        return {
            "source": self.source,
            "observation_date": self.observation_date.isoformat(),
            "rate_usd_per_eur": str(self.rate_usd_per_eur),
            "stale_cache": self.stale_cache,
        }


@dataclass(frozen=True)
class LlmCallRecord:
    """One provider call as the ``llm.call`` run action stores it (§2)."""

    kind: LlmCallKind
    model: str
    call_no: int
    attempt: int
    timeout_s: float
    prompt_version_id: UUID | None
    rule_version_id: UUID | None
    input_tokens_estimate: int
    started_at: datetime
    duration_ms: int
    response: Any = None
    error: LlmCallError | None = None
    fx: FxProvenance | None = None
    paid_metadata_error: bool = False
    operation: str | None = None
    # E-3, snapshot-path tasks only: the manifest call this record is, its stage, the
    # attempt's policy snapshot and the stored manifest (None while no store is wired).
    call_id: UUID | None = None
    stage: str | None = None
    policy_snapshot_id: UUID | None = None
    manifest_id: UUID | None = None

    def request_json(self) -> dict[str, object]:
        """Metadata only: the prompt itself is never copied into the trace."""
        metadata: dict[str, object] = {
            "kind": self.kind.value,
            "model": self.model,
            "call_no": self.call_no,
            "attempt": self.attempt,
            "timeout_s": self.timeout_s,
            "prompt_version_id": _uuid_or_none(self.prompt_version_id),
            "rule_version_id": _uuid_or_none(self.rule_version_id),
            "input_tokens_estimate": self.input_tokens_estimate,
        }
        if self.operation is not None:
            metadata["operation"] = self.operation
        if self.call_id is not None:
            metadata["call_id"] = str(self.call_id)
            metadata["stage"] = self.stage
            metadata["policy_snapshot_id"] = _uuid_or_none(self.policy_snapshot_id)
            metadata["manifest_id"] = _uuid_or_none(self.manifest_id)
        if self.fx is not None:
            metadata["fx"] = self.fx.as_json()
        if self.paid_metadata_error:
            metadata["paid_metadata_error"] = True
        return metadata

    def response_json(self) -> Any:
        """The provider answer as is (also an invalid one), or the normalized error."""
        if self.error is None:
            return self.response
        return {
            "error": {
                "class": self.error.error_class.value,
                "http_status": self.error.http_status,
                "message": self.error.message,
            }
        }


class UsageLedger(Protocol):
    """Insert-only ``usage_events`` (Р-8) and the run cost sum over all attempts (§4.5)."""

    async def run_cost_usd(self, run_id: UUID) -> Decimal: ...

    async def record(self, context: RunCallContext, usage: LlmUsage) -> None: ...


class LlmCallTrace(Protocol):
    """Writes one ``llm.call`` run action per provider call, outside any call transaction."""

    async def record_call(self, run_id: UUID, record: LlmCallRecord) -> None: ...


@dataclass(frozen=True)
class ManifestEntry:
    """The manifest of one snapshot-path provider call, stored before the call (CVS §5).

    ``manifest_id`` names the row in ``llm.call``; ``parent_call_id`` is the base call of a
    repair or a fallback; ``input_tokens`` is the call's pre-send count with the pinned
    counter. The CVS §5.1 identities are known before the call: the SHA-256 of the exact
    provider messages, the checksum of the prompt the stage pinned and
    ``model_profile_version`` of the pinned execution profile.
    """

    manifest_id: UUID
    attempt: int
    policy_snapshot_id: UUID
    manifest: CallManifest
    parent_call_id: UUID | None
    input_tokens: int
    started_at: datetime
    rendered_messages_sha256: str
    prompt_checksum: str
    model_profile_version: str


def stage_prompt_checksum(bindings: PromptBindings, stage: StageName) -> str:
    """The checksum of the prompt a call's stage pinned (CVS §5.1 ``prompt_checksum``)."""
    checksums: dict[StageName, str] = {
        "conventions": bindings.conventions_checksum,
        "review": bindings.review_system_checksum,
        "verification": bindings.verifier_checksum,
    }
    return checksums[stage]


def model_profile_version(profile: ExecutionProfile) -> str:
    """CVS §5.1 ``model_profile_version``: SHA-256 of the canonical JSON of the pinned
    execution profile as the snapshot stores it. It changes with any pinned model, route,
    counter or profile version and stays the same on a retry."""
    canonical = json.dumps(
        pinned_to_json(profile), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return sha256(canonical.encode("utf-8")).hexdigest()


class ManifestStore(Protocol):
    """Stores a manifest and its ``context.manifest`` step, committed before the call.

    A failure means the call is not made.
    """

    async def record_manifest(self, run_id: UUID, entry: ManifestEntry) -> None: ...


class ContextManifestRepository(Protocol):
    """Adds the ``context_manifests`` row and the ``context.manifest`` step; flushes only."""

    async def add(self, run_id: UUID, entry: ManifestEntry) -> None: ...


class ContextManifestUnitOfWork(UnitOfWork, Protocol):
    @property
    def manifests(self) -> ContextManifestRepository: ...


class TransactionalManifestStore:
    """``ManifestStore``: one short transaction per manifest, never around a provider call."""

    def __init__(self, uow_factory: Callable[[], ContextManifestUnitOfWork]) -> None:
        self._uow_factory = uow_factory

    async def record_manifest(self, run_id: UUID, entry: ManifestEntry) -> None:
        async with self._uow_factory() as uow:
            await uow.manifests.add(run_id, entry)
            await uow.commit()


def _uuid_or_none(value: UUID | None) -> str | None:
    return None if value is None else str(value)
