"""LLM Gateway: failure policy of one run attempt (docs/PIPELINE_SPEC.md §3, §4.5, §5.1).

Per attempt: at most ``max_calls_per_attempt`` provider calls in total (primary, retries,
repair, fallback); key rotation inside the transport is not a call. Before every call the
gateway checks the attempt deadline, the context size and the run cost over all attempts
(``usage_events``) plus the estimate of that call. Each call is traced as ``llm.call`` and
its usage recorded as soon as the call returns, outside any database transaction.

| class           | same model                         | then              |
|-----------------|------------------------------------|-------------------|
| timeout         | 1 retry after 2 s + jitter         | fallback once     |
| 429             | 1 retry after Retry-After <= 30 s, | fallback once     |
|                 | or 2 s + jitter without the header |                   |
| 5xx, connection | 2 retries after 2 s, 8 s + jitter  | fallback once     |
| invalid answer  | 1 repair call with validator errors| fallback once     |
|                 | (skipped if it would overflow)     |                   |
| context overflow| none                               | none              |
"""

from __future__ import annotations

import asyncio
import logging
import math
import random
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, DecimalException
from typing import Protocol
from uuid import UUID

from app.modules.reviews.application.llm import (
    LlmCallError,
    LlmCallFailed,
    LlmCallKind,
    LlmCallRecord,
    LlmCallTrace,
    LlmErrorCode,
    LlmUsage,
    RunCallContext,
    UsageLedger,
)
from app.modules.reviews.infrastructure.llm.answers import InvalidAnswer
from app.modules.reviews.infrastructure.llm.settings import LlmSettings, ModelProfile
from app.modules.reviews.infrastructure.llm.transport import (
    ChatMessage,
    ChatRequest,
    ChatResponse,
    ChatTransport,
    ResponseSchema,
    TransportError,
    TransportPaidAnswerError,
)

logger = logging.getLogger(__name__)

_REPAIR_INSTRUCTION = (
    "Your previous answer violated the output contract. Validator errors:\n{errors}\n"
    "Answer again with the corrected JSON object only: no prose, no code fence."
)
_USD_QUANTUM = Decimal("0.000001")


class _CostMetadataError(ValueError):
    """The provider amount cannot be converted to USD for this paid answer."""


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class HeuristicTokenCounter:
    """Conservative pre-send estimate: characters / ``chars_per_token``, rounded up.

    Exact tokenizers of the routed models are not available offline; the provider's
    ``prompt_tokens`` is the truth recorded in ``usage_events``, and an HTTP 400 about
    the context length is classified as overflow as well.
    """

    def __init__(self, chars_per_token: float) -> None:
        self._chars_per_token = chars_per_token

    def count(self, text: str) -> int:
        return math.ceil(len(text) / self._chars_per_token)


@dataclass(frozen=True)
class StructuredTask:
    """One structured generation: its messages, the strict schema and the validator."""

    operation: str
    messages: tuple[ChatMessage, ...]
    schema: ResponseSchema
    validate: Callable[[str], dict[str, object]]


@dataclass(frozen=True)
class GatewayResult:
    output: dict[str, object]
    provider: str
    model: str
    usage: tuple[LlmUsage, ...]
    calls: int


@dataclass
class _Outcome:
    output: dict[str, object] | None = None
    error: LlmErrorCode | None = None
    message: str = ""
    retryable: bool = True
    retry_after_s: float | None = None
    answer: str = ""
    errors: list[str] = field(default_factory=list)
    refused: bool = False


@dataclass
class _AttemptState:
    """One ``generate()``: its own calls and usage, returned to the caller."""

    calls: int = 0
    usage: list[LlmUsage] = field(default_factory=list)


@dataclass
class _AttemptCalls:
    """Provider calls of one run attempt across every ``generate()`` (§4.5)."""

    attempt: int
    calls: int = 0


_TRACKED_RUNS = 4096


class LlmGateway:
    def __init__(
        self,
        settings: LlmSettings,
        transport: ChatTransport,
        ledger: UsageLedger,
        trace: LlmCallTrace,
        *,
        clock: Clock | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        jitter: Callable[[], float] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._settings = settings
        self._transport = transport
        self._ledger = ledger
        self._trace = trace
        self._clock = clock or SystemClock()
        self._sleep = sleep
        policy = settings.policy
        self._jitter = jitter or (lambda: random.uniform(0, policy.max_jitter_s))
        self._monotonic = monotonic
        # run_id -> calls of its current attempt; conventions and review share it.
        self._attempts: OrderedDict[UUID, _AttemptCalls] = OrderedDict()

    @property
    def settings(self) -> LlmSettings:
        return self._settings

    def token_counter(self, profile: ModelProfile | None = None) -> HeuristicTokenCounter:
        return HeuristicTokenCounter((profile or self._settings.primary).chars_per_token)

    def context_limit(self, profile: ModelProfile, engine: str) -> int:
        """``prompt + reserved answer`` must not exceed this (SD §13, model window)."""
        limit = self._settings.policy.input_token_limit["deep" if engine == "deep" else "fast"]
        return min(profile.context_window, limit)

    def max_prompt_tokens(self, engine: str) -> int:
        """The budget ``PromptBuilder`` input is fitted to, for the primary model."""
        primary = self._settings.primary
        return self.context_limit(primary, engine) - primary.max_output_tokens

    async def generate(self, task: StructuredTask, context: RunCallContext) -> GatewayResult:
        policy = self._settings.policy
        primary = self._settings.primary
        fallback = self._settings.fallback
        state = _AttemptState()
        attempt = self._attempt_calls(context)
        max_calls = policy.max_calls_per_attempt
        if attempt.calls >= max_calls:
            raise self._failed(
                LlmErrorCode.UNAVAILABLE,
                f"attempt {context.attempt} already made {attempt.calls} provider calls",
                state,
            )
        primary_limit = max_calls - (1 if fallback is not None else 0)
        timeout_s = policy.call_timeout_s[context.engine]
        messages = task.messages
        kind = LlmCallKind.PRIMARY
        timeouts = unavailable = 0
        rate_limit_waited = repaired = False

        while True:
            last = await self._call(primary, kind, messages, task, context, state, attempt)
            if last.output is not None:
                return self._result(primary, last, state)
            assert last.error is not None
            if attempt.calls >= primary_limit:
                break
            delay: float | None = None
            if last.error is LlmErrorCode.TIMEOUT and timeouts < len(policy.timeout_retry_delays_s):
                delay = policy.timeout_retry_delays_s[timeouts] + self._jitter()
                timeouts += 1
            elif (
                last.error is LlmErrorCode.UNAVAILABLE
                and last.retryable
                and unavailable < len(policy.unavailable_retry_delays_s)
            ):
                delay = policy.unavailable_retry_delays_s[unavailable] + self._jitter()
                unavailable += 1
            elif (
                last.error is LlmErrorCode.RATE_LIMITED
                and not rate_limit_waited
                and (last.retry_after_s is None or last.retry_after_s <= policy.max_retry_after_s)
            ):
                # Without Retry-After (common behind routers and self-hosted servers)
                # one default backoff replaces the provider's hint.
                delay = (
                    policy.rate_limit_default_delay_s + self._jitter()
                    if last.retry_after_s is None
                    else last.retry_after_s
                )
                rate_limit_waited = True
            elif last.error is LlmErrorCode.INVALID_OUTPUT and not repaired and not last.refused:
                repaired = True
                repair_messages = (
                    *messages,
                    ChatMessage("assistant", last.answer),
                    ChatMessage(
                        "user",
                        _REPAIR_INSTRUCTION.format(
                            errors="\n".join(f"- {item}" for item in last.errors)
                        ),
                    ),
                )
                if not self._fits_context(primary, repair_messages, context.engine):
                    # The repair conversation would overflow where the original prompt
                    # did not: skip the repair, the fallback gets the original prompt.
                    break
                messages = repair_messages
                kind = LlmCallKind.REPAIR
                continue
            else:
                break
            if self._seconds_left(context) - delay < timeout_s:
                # Sleeping would leave less than one call timeout: no same-model
                # retry; the fallback still gets the time that is left.
                break
            await self._sleep(delay)
            kind = LlmCallKind.RETRY

        if fallback is not None and attempt.calls < max_calls:
            if not self._fits_context(fallback, task.messages, context.engine):
                # A smaller fallback window must not turn a retryable primary error
                # into a non-retryable context overflow.
                raise self._failed(last.error, last.message, state)
            last = await self._call(
                fallback, LlmCallKind.FALLBACK, task.messages, task, context, state, attempt
            )
            if last.output is not None:
                return self._result(fallback, last, state)
        assert last.error is not None
        raise self._failed(last.error, last.message, state)

    def _attempt_calls(self, context: RunCallContext) -> _AttemptCalls:
        current = self._attempts.get(context.run_id)
        if current is None or current.attempt != context.attempt:
            current = _AttemptCalls(context.attempt)
            self._attempts[context.run_id] = current
        self._attempts.move_to_end(context.run_id)
        while len(self._attempts) > _TRACKED_RUNS:
            self._attempts.popitem(last=False)
        return current

    def _seconds_left(self, context: RunCallContext) -> float:
        return (context.deadline - self._clock.now()).total_seconds()

    def _estimate(self, profile: ModelProfile, messages: tuple[ChatMessage, ...]) -> int:
        counter = self.token_counter(profile)
        return sum(counter.count(message.content) for message in messages)

    def _fits_context(
        self, profile: ModelProfile, messages: tuple[ChatMessage, ...], engine: str
    ) -> bool:
        estimate = self._estimate(profile, messages)
        return estimate + profile.max_output_tokens <= self.context_limit(profile, engine)

    async def _call(
        self,
        profile: ModelProfile,
        kind: LlmCallKind,
        messages: tuple[ChatMessage, ...],
        task: StructuredTask,
        context: RunCallContext,
        state: _AttemptState,
        attempt: _AttemptCalls,
    ) -> _Outcome:
        """Check deadline, context and budget, then make one provider call."""
        policy = self._settings.policy
        timeout_s = policy.call_timeout_s[context.engine]
        remaining = self._seconds_left(context)
        if remaining < timeout_s:
            raise self._failed(
                LlmErrorCode.DEADLINE_EXCEEDED,
                f"{remaining:.0f} s left before the attempt deadline, a call needs {timeout_s:g} s",
                state,
            )
        estimate = self._estimate(profile, messages)
        limit = self.context_limit(profile, context.engine)
        if estimate + profile.max_output_tokens > limit:
            raise self._failed(
                LlmErrorCode.CONTEXT_OVERFLOW,
                f"prompt ~{estimate} tokens + {profile.max_output_tokens} reserved exceeds {limit}",
                state,
            )
        spent = await self._ledger.run_cost_usd(context.run_id)
        next_cost = profile.price.cost_usd(tokens_in=estimate, tokens_out=profile.max_output_tokens)
        cost_limit = policy.run_cost_limit_usd[context.engine]
        if spent + next_cost > cost_limit:
            raise self._failed(
                LlmErrorCode.BUDGET_EXCEEDED,
                f"run spent ${spent} and the next call may cost ${next_cost}, limit ${cost_limit}",
                state,
            )

        if attempt.calls >= policy.max_calls_per_attempt:
            # Guards concurrent generations of one attempt, not only their entry.
            raise self._failed(
                LlmErrorCode.UNAVAILABLE,
                f"attempt {context.attempt} already made {attempt.calls} provider calls",
                state,
            )
        state.calls += 1
        attempt.calls += 1
        started_at = self._clock.now()
        started = self._monotonic()
        record = _RecordDraft(
            kind=kind,
            model=profile.model,
            call_no=attempt.calls,
            context=context,
            timeout_s=timeout_s,
            estimate=estimate,
            started_at=started_at,
        )
        try:
            response = await self._transport.complete(
                ChatRequest(
                    profile=profile,
                    messages=messages,
                    response_schema=task.schema,
                    timeout_s=timeout_s,
                )
            )
        except TransportPaidAnswerError as error:
            duration_ms = _elapsed_ms(started, self._monotonic())
            paid_response = error.paid_response
            usage = self._usage(profile, task.operation, paid_response, estimate, conservative=True)
            await self._record_paid_answer(
                context, state, record, duration_ms, paid_response, usage
            )
            raise self._failed(LlmErrorCode.INVALID_OUTPUT, error.message, state) from None
        except TransportError as error:
            duration_ms = _elapsed_ms(started, self._monotonic())
            await self._record_trace(
                context,
                record.finish(
                    duration_ms,
                    error=LlmCallError(error.error_class, error.http_status, error.message),
                ),
            )
            logger.info(
                "llm call failed",
                extra={
                    "run_id": str(context.run_id),
                    "kind": kind.value,
                    "model": profile.model,
                    "error_class": error.error_class.value,
                    "http_status": error.http_status,
                    "duration_ms": duration_ms,
                },
            )
            if error.error_class is LlmErrorCode.CONTEXT_OVERFLOW:
                raise self._failed(error.error_class, error.message, state) from None
            return _Outcome(
                error=error.error_class,
                message=error.message,
                retryable=error.retryable,
                retry_after_s=error.retry_after_s,
            )

        duration_ms = _elapsed_ms(started, self._monotonic())
        try:
            usage = self._usage(profile, task.operation, response, estimate)
        except _CostMetadataError as error:
            usage = self._usage(profile, task.operation, response, estimate, conservative=True)
            await self._record_paid_answer(context, state, record, duration_ms, response, usage)
            raise self._failed(LlmErrorCode.INVALID_OUTPUT, str(error), state) from None
        await self._record_paid_answer(context, state, record, duration_ms, response, usage)
        logger.info(
            "llm call answered",
            extra={
                "run_id": str(context.run_id),
                "kind": kind.value,
                "model": usage.model,
                "tokens_in": usage.tokens_in,
                "tokens_out": usage.tokens_out,
                "duration_ms": duration_ms,
            },
        )
        return _validated(response, task)

    async def _record_paid_answer(
        self,
        context: RunCallContext,
        state: _AttemptState,
        record: _RecordDraft,
        duration_ms: int,
        response: ChatResponse,
        usage: LlmUsage,
    ) -> None:
        state.usage.append(usage)
        await self._ledger.record(context, usage)
        await self._record_trace(context, record.finish(duration_ms, response=response.raw))

    async def _record_trace(self, context: RunCallContext, record: LlmCallRecord) -> None:
        """A failed trace write is logged, never allowed to discard a paid answer."""
        try:
            await self._trace.record_call(context.run_id, record)
        except Exception:
            logger.exception(
                "llm.call trace write failed",
                extra={"run_id": str(context.run_id), "call_no": record.call_no},
            )

    def _usage(
        self,
        profile: ModelProfile,
        operation: str,
        response: ChatResponse,
        estimate: int,
        *,
        conservative: bool = False,
    ) -> LlmUsage:
        tokens_in = response.prompt_tokens if response.prompt_tokens is not None else estimate
        tokens_out = (
            response.completion_tokens
            if response.completion_tokens is not None
            else profile.max_output_tokens
        )
        cached_tokens = response.cached_tokens if response.cached_tokens is not None else 0
        if tokens_in == 0 and tokens_out == 0 and response.cost is None:
            # No usage block (some self-hosted servers): count the pre-send estimate and
            # the answer, so the run cost limit does not fail open.
            tokens_in = estimate
            tokens_out = self.token_counter(profile).count(response.content or "")
        if conservative:
            # The provider answered but its amount cannot be trusted as USD. Charge at
            # least the pre-call reservation, without a cache discount, then fail.
            cost = profile.price.cost_usd(
                tokens_in=max(tokens_in, estimate),
                tokens_out=max(tokens_out, profile.max_output_tokens),
            )
        elif response.cost is not None:
            try:
                if response.cost_currency in (None, "USD"):
                    cost_usd = response.cost
                elif response.cost_currency == "EUR":
                    rate = self._settings.eur_to_usd_rate
                    if rate is None or not rate.is_finite() or rate <= 0:
                        raise _CostMetadataError(
                            "LLM_EUR_TO_USD_RATE is required for EUR usage.cost"
                        )
                    cost_usd = response.cost * rate
                else:
                    raise _CostMetadataError("unsupported usage.cost_currency")
                if not cost_usd.is_finite():
                    raise _CostMetadataError("usage.cost cannot be represented in USD")
                cost = cost_usd.quantize(_USD_QUANTUM)
            except DecimalException:
                raise _CostMetadataError("usage.cost cannot be represented in USD") from None
        else:
            cost = profile.price.cost_usd(
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                cache_read_tokens=cached_tokens,
            )
        return LlmUsage(
            provider=profile.provider,
            model=response.model or profile.model,
            operation=operation,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cache_read_tokens=cached_tokens,
            cost_usd=cost,
        )

    @staticmethod
    def _result(profile: ModelProfile, outcome: _Outcome, state: _AttemptState) -> GatewayResult:
        assert outcome.output is not None
        usage = tuple(state.usage)
        return GatewayResult(
            output=outcome.output,
            provider=profile.provider,
            model=usage[-1].model if usage else profile.model,
            usage=usage,
            calls=state.calls,
        )

    @staticmethod
    def _failed(code: LlmErrorCode, message: str, state: _AttemptState) -> LlmCallFailed:
        return LlmCallFailed(code, message, usage=tuple(state.usage), calls=state.calls)


@dataclass(frozen=True)
class _RecordDraft:
    kind: LlmCallKind
    model: str
    call_no: int
    context: RunCallContext
    timeout_s: float
    estimate: int
    started_at: datetime

    def finish(
        self, duration_ms: int, *, response: object = None, error: LlmCallError | None = None
    ) -> LlmCallRecord:
        return LlmCallRecord(
            kind=self.kind,
            model=self.model,
            call_no=self.call_no,
            attempt=self.context.attempt,
            timeout_s=self.timeout_s,
            prompt_version_id=self.context.prompt_version_id,
            rule_version_id=self.context.rule_version_id,
            input_tokens_estimate=self.estimate,
            started_at=self.started_at,
            duration_ms=duration_ms,
            response=response,
            error=error,
        )


def _validated(response: ChatResponse, task: StructuredTask) -> _Outcome:
    answer = response.content or ""
    if response.refusal is not None:
        # A refusal is not a format error: repeating it with validator errors is wasted.
        return _Outcome(
            error=LlmErrorCode.INVALID_OUTPUT,
            message=f"the model refused: {response.refusal[:200]}",
            answer=answer,
            refused=True,
        )
    if response.finish_reason == "length":
        errors = ["the answer was cut at the output token limit; return a shorter JSON object"]
    elif response.content is None:
        errors = ["the answer has no text content"]
    else:
        try:
            return _Outcome(output=task.validate(answer))
        except InvalidAnswer as error:
            errors = error.errors
    return _Outcome(
        error=LlmErrorCode.INVALID_OUTPUT,
        message="; ".join(errors)[:500],
        answer=answer,
        errors=errors,
    )


def _elapsed_ms(started: float, finished: float) -> int:
    return max(int((finished - started) * 1000), 0)
