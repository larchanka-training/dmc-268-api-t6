"""Call slots a task leaves for the stages after it (#130 PR E, OQ-2 = A; CVS §7.3).

``StructuredTask.reserved_calls`` takes slots out of the shared cap of 4 provider calls
per (run, attempt). The review keeps them for the verifier; the cap itself is unchanged,
and the default of 0 keeps today's chain. F sets the values; nothing live sets them yet.
No network: provider answers come from ``httpx.MockTransport``.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Literal
from uuid import UUID

import httpx
import pytest

from app.modules.reviews.application.llm import LlmCallFailed, LlmErrorCode, RunCallContext
from app.modules.reviews.infrastructure.llm.answers import InvalidAnswer
from app.modules.reviews.infrastructure.llm.gateway import (
    GatewayResult,
    LlmGateway,
    StructuredTask,
)
from app.modules.reviews.infrastructure.llm.memory import InMemoryLlmCallTrace, InMemoryUsageLedger
from app.modules.reviews.infrastructure.llm.settings import LlmSettings, ModelPrice, ModelProfile
from app.modules.reviews.infrastructure.llm.transport import (
    ChatMessage,
    OpenAICompatibleTransport,
    ResponseSchema,
)
from tests.llm_fakes import context_checkpoint

NOW = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)
RUN = RunCallContext(
    run_id=UUID("00000000-0000-0000-0000-000000014001"),
    workspace_id=UUID("00000000-0000-0000-0000-000000014002"),
    attempt=1,
    engine="fast",
    deadline=NOW + timedelta(seconds=480),
)
_BASE = ModelProfile(
    provider="router",
    base_url="https://router.test/v1",
    model="",
    context_window=100_000,
    max_output_tokens=1_000,
    price=ModelPrice(Decimal("1"), Decimal("2")),
    api_keys=("sk-test-1",),
)
SETTINGS = LlmSettings(
    primary=replace(_BASE, model="review-model"),
    fallback=replace(_BASE, model="review-fallback-model"),
    verifier=replace(_BASE, model="verifier-model"),
    verifier_fallback=replace(_BASE, model="verifier-fallback-model"),
)
ANSWER = {"ok": True}


def _validate(answer: str) -> dict[str, object]:
    try:
        parsed = json.loads(answer)
    except json.JSONDecodeError:
        raise InvalidAnswer(["the answer is not valid JSON"]) from None
    if parsed != ANSWER:
        raise InvalidAnswer(['the answer must be {"ok": true}'])
    return {"ok": True}


def _task(
    operation: Literal["conventions", "review", "verification"], *, reserved_calls: int = 0
) -> StructuredTask:
    return StructuredTask(
        operation=operation,
        messages=(ChatMessage("system", "SYS"), ChatMessage("user", "USER")),
        schema=ResponseSchema("Answer", {"type": "object"}),
        validate=_validate,
        route="verifier" if operation == "verification" else "review",
        reserved_calls=reserved_calls,
    )


def _completion(content: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "model": "answering-model",
            "choices": [{"message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        },
    )


def valid() -> httpx.Response:
    return _completion(json.dumps(ANSWER))


def invalid() -> httpx.Response:
    return _completion("not json")


class _Clock:
    def __init__(self) -> None:
        self.current = NOW

    def now(self) -> datetime:
        return self.current

    def monotonic(self) -> float:
        return (self.current - NOW).total_seconds()

    async def sleep(self, seconds: float) -> None:
        self.current += timedelta(seconds=seconds)


@dataclass
class _Provider:
    replies: list[httpx.Response]
    requests: list[httpx.Request] = field(default_factory=list)
    trace: InMemoryLlmCallTrace = field(default_factory=InMemoryLlmCallTrace)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if not self.replies:
            raise AssertionError("unexpected provider call")
        return self.replies.pop(0)

    def run(self, *tasks: StructuredTask) -> list[GatewayResult | LlmCallFailed]:
        """Run the stages one after another in the same (run, attempt)."""

        async def exercise() -> list[GatewayResult | LlmCallFailed]:
            clock = _Clock()
            async with httpx.AsyncClient(transport=httpx.MockTransport(self._handle)) as client:
                gateway = LlmGateway(
                    SETTINGS,
                    OpenAICompatibleTransport(client),
                    InMemoryUsageLedger(),
                    self.trace,
                    checkpoint=context_checkpoint,
                    clock=clock,
                    sleep=clock.sleep,
                    jitter=lambda: 0.0,
                    monotonic=clock.monotonic,
                )
                outcomes: list[GatewayResult | LlmCallFailed] = []
                for task in tasks:
                    try:
                        outcomes.append(await gateway.generate(task, RUN))
                    except LlmCallFailed as failure:
                        outcomes.append(failure)
                return outcomes

        return asyncio.run(exercise())

    def models(self) -> list[str]:
        return [json.loads(request.content)["model"] for request in self.requests]

    def kinds(self) -> list[str]:
        return [record.kind.value for _, record in self.trace.records]


def test_without_a_reserve_the_review_takes_the_verifier_slot() -> None:
    # Pin of today's chain at the default reserved_calls=0 (plan, "Call budget").
    provider = _Provider([valid(), invalid(), invalid(), invalid()])

    conventions, review, verifier = provider.run(
        _task("conventions"), _task("review"), _task("verification")
    )

    assert _task("review").reserved_calls == 0
    assert isinstance(conventions, GatewayResult)
    assert isinstance(review, LlmCallFailed)
    assert review.error_code is LlmErrorCode.INVALID_OUTPUT
    assert review.calls == 3
    assert isinstance(verifier, LlmCallFailed)
    assert verifier.error_code is LlmErrorCode.UNAVAILABLE
    assert str(verifier) == "llm_unavailable: attempt 1 already made 4 provider calls"
    assert verifier.calls == 0
    assert provider.kinds() == ["primary", "primary", "repair", "fallback"]
    assert provider.models() == [
        "review-model",
        "review-model",
        "review-model",
        "review-fallback-model",
    ]


def test_a_reserved_slot_after_conventions_keeps_the_verifier_call() -> None:
    # C1, then the review has two slots: the gateway gives the second to its fallback.
    provider = _Provider([valid(), invalid(), invalid(), valid()])

    conventions, review, verifier = provider.run(
        _task("conventions"), _task("review", reserved_calls=1), _task("verification")
    )

    assert isinstance(conventions, GatewayResult)
    assert isinstance(review, LlmCallFailed)
    assert review.error_code is LlmErrorCode.INVALID_OUTPUT
    assert review.calls == 2
    assert isinstance(verifier, GatewayResult)
    assert verifier.calls == 1
    assert provider.kinds() == ["primary", "primary", "fallback", "primary"]
    assert provider.models() == [
        "review-model",
        "review-model",
        "review-fallback-model",
        "verifier-model",
    ]


def test_a_reserved_slot_is_not_given_to_the_review_fallback() -> None:
    # C1 + C2 repair, then the review has one slot left: no fallback, its own error.
    provider = _Provider([invalid(), valid(), invalid(), valid()])

    conventions, review, verifier = provider.run(
        _task("conventions"), _task("review", reserved_calls=1), _task("verification")
    )

    assert isinstance(conventions, GatewayResult)
    assert conventions.calls == 2
    assert isinstance(review, LlmCallFailed)
    assert review.error_code is LlmErrorCode.INVALID_OUTPUT
    assert review.calls == 1
    assert isinstance(verifier, GatewayResult)
    assert verifier.calls == 1
    assert provider.kinds() == ["primary", "repair", "primary", "primary"]
    assert provider.models() == ["review-model", "review-model", "review-model", "verifier-model"]


def test_a_review_cannot_start_on_the_reserved_slots() -> None:
    # C1, C2 repair, C3 fallback: only the reserved slot is left when the review starts.
    provider = _Provider([invalid(), invalid(), valid(), valid()])

    conventions, review, verifier = provider.run(
        _task("conventions"), _task("review", reserved_calls=1), _task("verification")
    )

    assert isinstance(conventions, GatewayResult)
    assert conventions.calls == 3
    assert isinstance(review, LlmCallFailed)
    assert review.error_code is LlmErrorCode.UNAVAILABLE
    assert str(review) == (
        "llm_unavailable: attempt 1 already made 3 provider calls, 1 kept for later stages"
    )
    assert review.calls == 0
    assert isinstance(verifier, GatewayResult)
    assert verifier.calls == 1
    assert provider.kinds() == ["primary", "repair", "fallback", "primary"]
    assert provider.models()[3:] == ["verifier-model"]


def test_a_negative_reserve_is_refused() -> None:
    with pytest.raises(ValueError, match="reserved_calls must not be negative: -1"):
        _task("review", reserved_calls=-1)
