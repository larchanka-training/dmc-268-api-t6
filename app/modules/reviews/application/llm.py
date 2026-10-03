"""Provider-neutral contracts of one run attempt's LLM calls (docs/PIPELINE_SPEC.md §2-§6).

The worker binds a ``RunCallContext`` when it claims a run; the gateway uses it to check
the attempt deadline, the run cost limit and to write the ``llm.call`` trace and the
``usage_events`` rows through the ports below. No vendor SDK or HTTP detail lives here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal, Protocol
from uuid import UUID

from app.modules.reviews.application.run_failures import RETRYABLE_ERROR_CODES

type EngineName = Literal["fast", "deep"]


class LlmErrorCode(StrEnum):
    """The LLM part of the ``error_code`` catalog (docs/PIPELINE_SPEC.md §6)."""

    TIMEOUT = "llm_timeout"
    RATE_LIMITED = "llm_rate_limited"
    UNAVAILABLE = "llm_unavailable"
    INVALID_OUTPUT = "llm_invalid_output"
    CONTEXT_OVERFLOW = "llm_context_overflow"
    BUDGET_EXCEEDED = "budget_exceeded"
    DEADLINE_EXCEEDED = "deadline_exceeded"


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
    ) -> None:
        super().__init__(f"{error_code.value}: {message}")
        self.error_code = error_code
        self.usage = usage
        self.calls = calls

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

    def request_json(self) -> dict[str, object]:
        """Metadata only: the prompt itself is never copied into the trace."""
        return {
            "kind": self.kind.value,
            "model": self.model,
            "call_no": self.call_no,
            "attempt": self.attempt,
            "timeout_s": self.timeout_s,
            "prompt_version_id": _uuid_or_none(self.prompt_version_id),
            "rule_version_id": _uuid_or_none(self.rule_version_id),
            "input_tokens_estimate": self.input_tokens_estimate,
        }

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


def _uuid_or_none(value: UUID | None) -> str | None:
    return None if value is None else str(value)
