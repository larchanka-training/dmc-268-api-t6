"""Recovery of snapshot-path tasks (#130 F1-9: E-1, E-7 time, E-12; plan DD8, TL-2, TL-6).

A snapshot-path review keeps one provider call and one verifier window (a 90 s call and 10 s of
local completion) of its attempt for the verifier; snapshot-path conventions keep two calls,
for the review and the verifier; both repair with the bounded envelope (RULES §8.1). Legacy
tasks keep no slot, no time and the assistant-turn repair (F1-1 pins the live chain).
No network: provider answers come from ``httpx.MockTransport``.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from app.modules.reviews.application.conventions import ConventionsRequest
from app.modules.reviews.application.llm import LlmCallFailed, LlmErrorCode, RunCallContext
from app.modules.reviews.application.policy_snapshot import PolicySnapshot
from app.modules.reviews.application.run_failures import classify_failure
from app.modules.reviews.infrastructure.llm.gateway import LlmGateway, StructuredTask
from app.modules.reviews.infrastructure.llm.models import (
    GatewayConventionsModel,
    GatewayReviewModel,
)
from app.modules.reviews.infrastructure.llm.settings import LlmSettings
from app.modules.reviews.infrastructure.llm.transport import (
    ChatMessage,
    OpenAICompatibleTransport,
    ResponseSchema,
)
from tests.llm_fakes import FakeClock, ScriptedProvider, completion, context_checkpoint
from tests.test_gateway_review_model import (
    EMPTY_ANSWER,
    RUN_ID,
    _context,
    _NoMeta,
    _one_line,
    _profile,
    _snapshot,
)
from tests.test_repo_conventions import draft

NOW = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)
SETTINGS = LlmSettings(
    primary=_profile("review-model"),
    fallback=_profile("fallback-model"),
    verifier=_profile("verifier-model"),
    verifier_fallback=_profile("verifier-fallback-model"),
)
# The fast engine: a review call needs 90 s; the review keeps 90 s + 10 s for the verifier.
REVIEW_WINDOW_S = 90 + 90 + 10

type Stage = Callable[[LlmGateway, RunCallContext], Awaitable[object]]


class _Recording(LlmGateway):
    """The real gateway; it also keeps every task it was given."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.tasks: list[StructuredTask] = []

    async def generate(self, task: StructuredTask, context: RunCallContext) -> Any:
        self.tasks.append(task)
        return await super().generate(task, context)


@dataclass
class _Provider(ScriptedProvider):
    """Scripted answers; each provider call takes ``seconds_per_call`` of the fake clock."""

    seconds_per_call: float = 0.0
    clock: FakeClock | None = None

    def handle(self, request: httpx.Request) -> httpx.Response:
        if self.clock is not None:
            self.clock.current += timedelta(seconds=self.seconds_per_call)
        return super().handle(request)

    def run(self, seconds_left: float, *stages: Stage) -> tuple[list[StructuredTask], list[object]]:
        """Run the stages one after another in the same (run, attempt)."""
        clock = self.clock = FakeClock(NOW)
        run = RunCallContext(
            run_id=RUN_ID,
            workspace_id=RUN_ID,
            attempt=1,
            engine="fast",
            deadline=NOW + timedelta(seconds=seconds_left),
        )

        async def exercise() -> tuple[list[StructuredTask], list[object]]:
            async with httpx.AsyncClient(transport=httpx.MockTransport(self.handle)) as client:
                gateway = _Recording(
                    SETTINGS,
                    OpenAICompatibleTransport(client),
                    self.ledger,
                    self.trace,
                    checkpoint=context_checkpoint,
                    clock=clock,
                    sleep=clock.sleep,
                    jitter=lambda: 0.0,
                    monotonic=clock.monotonic,
                )
                outcomes: list[object] = []
                for stage in stages:
                    try:
                        outcomes.append(await stage(gateway, run))
                    except LlmCallFailed as failure:
                        outcomes.append(failure)
                return gateway.tasks, outcomes

        return asyncio.run(exercise())

    def operations(self) -> list[str | None]:
        return [record.operation for _, record in self.trace.records]


def _conventions(snapshot: PolicySnapshot | None) -> Stage:
    request = ConventionsRequest("P", (), None, (), (), {}, ("app/main.py",), snapshot=snapshot)

    async def stage(gateway: LlmGateway, run: RunCallContext) -> object:
        return await GatewayConventionsModel(gateway, run).draft_conventions(request=request)

    return stage


def _review(snapshot: PolicySnapshot | None) -> Stage:
    async def stage(gateway: LlmGateway, run: RunCallContext) -> object:
        model = GatewayReviewModel(gateway, run, _NoMeta())
        return await model.draft_review(context=_context(_one_line(), snapshot))

    return stage


async def _verifier(gateway: LlmGateway, run: RunCallContext) -> object:
    return await gateway.generate(
        StructuredTask(
            operation="verification",
            messages=(ChatMessage("system", "SYS"), ChatMessage("user", "USER")),
            schema=ResponseSchema("Answer", {"type": "object"}),
            validate=lambda answer: dict(json.loads(answer)),
            route="verifier",
        ),
        run,
    )


def _review_answer() -> httpx.Response:
    return completion(json.dumps(EMPTY_ANSWER))


def _conventions_answer() -> httpx.Response:
    return completion(json.dumps(draft()))


def _unavailable() -> httpx.Response:
    return httpx.Response(503, json={"error": {"message": "overloaded"}})


@pytest.mark.parametrize(
    ("stage", "answer", "reserved_calls", "bounded_repair", "reserved_seconds"),
    [
        (_review(_snapshot()), _review_answer, 1, True, 100.0),
        (_review(None), _review_answer, 0, False, 0.0),
        (_conventions(_snapshot()), _conventions_answer, 2, True, 0.0),
        (_conventions(None), _conventions_answer, 0, False, 0.0),
    ],
    ids=["snapshot-review", "legacy-review", "snapshot-conventions", "legacy-conventions"],
)
def test_snapshot_tasks_keep_slots_and_time_and_repair_bounded(
    stage: Stage,
    answer: Callable[[], httpx.Response],
    reserved_calls: int,
    bounded_repair: bool,
    reserved_seconds: float,
) -> None:
    provider = _Provider([answer()])

    [task], [outcome] = provider.run(3_600, stage)

    assert not isinstance(outcome, LlmCallFailed)
    assert task.reserved_calls == reserved_calls
    assert task.bounded_repair is bounded_repair
    assert task.reserved_seconds == reserved_seconds


def test_a_snapshot_cache_miss_chain_is_conventions_review_fallback_verifier() -> None:
    # TL-2: with a fallback the review's second slot goes to it; the verifier keeps the fourth.
    provider = _Provider(
        [_conventions_answer(), completion("{not json"), _review_answer(), completion("{}")]
    )

    _, outcomes = provider.run(3_600, _conventions(_snapshot()), _review(_snapshot()), _verifier)

    assert not any(isinstance(outcome, LlmCallFailed) for outcome in outcomes)
    assert provider.operations() == ["conventions", "review", "review", "verification"]
    assert provider.kinds() == ["primary", "primary", "fallback", "primary"]
    assert provider.models() == ["review-model", "review-model", "fallback-model", "verifier-model"]


def test_snapshot_conventions_keep_two_slots_so_a_failed_answer_goes_to_the_fallback() -> None:
    # Two slots kept: conventions may spend two, so a failed C1 skips the repair for the fallback.
    provider = _Provider(
        [completion("{not json"), _conventions_answer(), _review_answer(), completion("{}")]
    )

    _, outcomes = provider.run(3_600, _conventions(_snapshot()), _review(_snapshot()), _verifier)

    assert not any(isinstance(outcome, LlmCallFailed) for outcome in outcomes)
    assert provider.operations() == ["conventions", "conventions", "review", "verification"]
    assert provider.kinds() == ["primary", "fallback", "primary", "primary"]


@pytest.mark.parametrize("seconds_left", [REVIEW_WINDOW_S - 1, REVIEW_WINDOW_S])
def test_a_snapshot_review_is_refused_without_the_verifier_window(seconds_left: int) -> None:
    provider = _Provider([_review_answer()])

    _, [outcome] = provider.run(seconds_left, _review(_snapshot()))

    if seconds_left < REVIEW_WINDOW_S:
        assert isinstance(outcome, LlmCallFailed)
        assert outcome.error_code is LlmErrorCode.VERIFICATION_DEADLINE_EXCEEDED
        assert str(outcome) == (
            "verification_deadline_exceeded: 189 s left before the attempt deadline, "
            "a call needs 90 s and 100 s are kept for later stages"
        )
        assert outcome.calls == 0
        assert provider.requests == []
        # Terminal, published as such: no unverified review (PIPELINE_SPEC §6).
        assert outcome.run_retryable is False
        assert classify_failure(outcome).error_code == "verification_deadline_exceeded"
    else:
        assert not isinstance(outcome, LlmCallFailed)
        assert len(provider.requests) == 1


@pytest.mark.parametrize("seconds_left", [89, 100])
def test_a_legacy_review_keeps_no_time_for_the_verifier(seconds_left: int) -> None:
    provider = _Provider([_review_answer()])

    _, [outcome] = provider.run(seconds_left, _review(None))

    if seconds_left < 90:
        assert isinstance(outcome, LlmCallFailed)
        assert str(outcome) == (
            "deadline_exceeded: 89 s left before the attempt deadline, a call needs 90 s"
        )
        assert provider.requests == []
    else:
        assert not isinstance(outcome, LlmCallFailed)
        assert len(provider.requests) == 1


def test_every_recovery_of_a_snapshot_review_keeps_the_verifier_window() -> None:
    # R1 takes 70 s of 250: 180 s are left, under 90 s + 100 s, so no repair is sent.
    provider = _Provider([completion("{not json"), _review_answer()], seconds_per_call=70)

    _, [outcome] = provider.run(250, _review(_snapshot()))

    assert isinstance(outcome, LlmCallFailed)
    assert outcome.error_code is LlmErrorCode.VERIFICATION_DEADLINE_EXCEEDED
    assert outcome.calls == 1
    assert len(provider.requests) == 1


def test_a_snapshot_review_does_not_sleep_into_the_verifier_window() -> None:
    # 191 s left: a 2 s backoff would leave 189 s, under the window, so the fallback answers now.
    provider = _Provider([_unavailable(), _review_answer()])

    _, [outcome] = provider.run(REVIEW_WINDOW_S + 1, _review(_snapshot()))

    assert not isinstance(outcome, LlmCallFailed)
    assert provider.kinds() == ["primary", "fallback"]
    assert provider.models() == ["review-model", "fallback-model"]


def test_a_negative_time_reserve_is_refused() -> None:
    task = StructuredTask(
        operation="review",
        messages=(ChatMessage("user", "USER"),),
        schema=ResponseSchema("Answer", {"type": "object"}),
        validate=lambda answer: {},
    )

    with pytest.raises(ValueError, match="reserved_seconds must not be negative: -1"):
        replace(task, reserved_seconds=-1)
