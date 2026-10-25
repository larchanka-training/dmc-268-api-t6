"""Recovery of snapshot-path tasks (#130 F1-9: E-1, E-7 time, E-12; plan DD8, TL-2, TL-6).

A snapshot-path review keeps one provider call and one verifier window (a 90 s call and 10 s of
local completion) of its attempt for the verifier; snapshot-path conventions keep two calls and
the time of a review call plus that window (CVS §7.4: they do not spend the reserve); the
snapshot-path verifier keeps its 10 s of local completion. Review and conventions repair with the
bounded envelope (RULES §8.1), and every snapshot-path validator decodes fail-closed. Legacy
tasks keep no slot, no time, the live decoder and the assistant-turn repair (F1-1 pins the live
chain). No network: provider answers come from ``httpx.MockTransport``.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

import httpx
import pytest

from app.modules.reviews.application.conventions import ConventionsRequest
from app.modules.reviews.application.llm import (
    LlmCallFailed,
    LlmErrorCode,
    ManifestEntry,
    ManifestStore,
    RunCallContext,
)
from app.modules.reviews.application.policy_snapshot import PolicySnapshot
from app.modules.reviews.application.reflection_pass import VerifierPromptBuilder
from app.modules.reviews.application.run_failures import classify_failure
from app.modules.reviews.infrastructure.llm.answers import (
    InvalidAnswer,
    conventions_answer_validator,
    validate_review_answer,
)
from app.modules.reviews.infrastructure.llm.ecb_fx import FxQuote, FxQuoteProvider, FxQuoteResult
from app.modules.reviews.infrastructure.llm.gateway import LlmGateway, StructuredTask
from app.modules.reviews.infrastructure.llm.memory import InMemoryUsageLedger
from app.modules.reviews.infrastructure.llm.models import (
    GatewayConventionsModel,
    GatewayReviewModel,
    GatewayVerifierModel,
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
from tests.test_gateway_verifier_model import CALL_ID as VERIFIER_CALL_ID
from tests.test_gateway_verifier_model import _candidate, _decision
from tests.test_gateway_verifier_model import _context as _verifier_context
from tests.test_gateway_verifier_model import _snapshot as _verifier_snapshot
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
# Conventions: their own call, then the review's call and the review's reserve.
CONVENTIONS_WINDOW_S = 90 + REVIEW_WINDOW_S
# The verifier: its 90 s call and 10 s of local completion.
VERIFIER_WINDOW_S = 90 + 10
DEEP_NESTING = "[" * 20_000  # json.loads: RecursionError
HUGE_INTEGER = '{"summary": ' + "1" * 5_000 + "}"  # json.loads: ValueError (int digit limit)

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
    settings: LlmSettings = SETTINGS
    fx_provider: FxQuoteProvider | None = None
    manifest_store: ManifestStore | None = None

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
                    self.settings,
                    OpenAICompatibleTransport(client),
                    self.ledger,
                    self.trace,
                    checkpoint=context_checkpoint,
                    clock=clock,
                    sleep=clock.sleep,
                    jitter=lambda: 0.0,
                    monotonic=clock.monotonic,
                    fx_provider=self.fx_provider,
                    manifest_store=self.manifest_store,
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


def _verifier_model(snapshot: PolicySnapshot | None) -> Stage:
    context = _verifier_context(snapshot)
    prompt, manifest = VerifierPromptBuilder().build_verifier_prompt(
        candidates=(_candidate("c-1"),), context=context, call_id=VERIFIER_CALL_ID
    )

    async def stage(gateway: LlmGateway, run: RunCallContext) -> object:
        return await GatewayVerifierModel(gateway, run).verify(
            system_prompt=prompt.system,
            user_prompt=prompt.user,
            manifest=manifest,
            context=context,
        )

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


def _verifier_answer() -> httpx.Response:
    return completion(json.dumps({"decisions": [_decision("c-1")]}))


def _unavailable() -> httpx.Response:
    return httpx.Response(503, json={"error": {"message": "overloaded"}})


@pytest.mark.parametrize(
    ("stage", "answer", "reserved_calls", "bounded_repair", "reserved_seconds"),
    [
        (_review(_snapshot()), _review_answer, 1, True, 100.0),
        (_review(None), _review_answer, 0, False, 0.0),
        (_conventions(_snapshot()), _conventions_answer, 2, True, 190.0),
        (_conventions(None), _conventions_answer, 0, False, 0.0),
        (_verifier_model(_verifier_snapshot()), _verifier_answer, 0, True, 10.0),
        (_verifier_model(None), _verifier_answer, 0, True, 0.0),
    ],
    ids=[
        "snapshot-review",
        "legacy-review",
        "snapshot-conventions",
        "legacy-conventions",
        "snapshot-verifier",
        "legacy-verifier",
    ],
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


@pytest.mark.parametrize("seconds_left", [CONVENTIONS_WINDOW_S - 1, CONVENTIONS_WINDOW_S])
def test_snapshot_conventions_leave_the_review_call_and_the_verifier_window(
    seconds_left: int,
) -> None:
    provider = _Provider([_conventions_answer()])

    _, [outcome] = provider.run(seconds_left, _conventions(_snapshot()))

    if seconds_left < CONVENTIONS_WINDOW_S:
        assert isinstance(outcome, LlmCallFailed)
        assert outcome.error_code is LlmErrorCode.VERIFICATION_DEADLINE_EXCEEDED
        assert str(outcome) == (
            "verification_deadline_exceeded: 279 s left before the attempt deadline, "
            "a call needs 90 s and 190 s are kept for later stages"
        )
        assert outcome.calls == 0
        assert provider.requests == []
    else:
        assert not isinstance(outcome, LlmCallFailed)
        assert len(provider.requests) == 1


@pytest.mark.parametrize("seconds_left", [VERIFIER_WINDOW_S - 1, VERIFIER_WINDOW_S])
def test_a_snapshot_verifier_keeps_its_local_completion(seconds_left: int) -> None:
    provider = _Provider([_verifier_answer()])

    _, [outcome] = provider.run(seconds_left, _verifier_model(_verifier_snapshot()))

    if seconds_left < VERIFIER_WINDOW_S:
        assert isinstance(outcome, LlmCallFailed)
        assert outcome.error_code is LlmErrorCode.VERIFICATION_DEADLINE_EXCEEDED
        assert str(outcome) == (
            "verification_deadline_exceeded: 99 s left before the attempt deadline, "
            "a call needs 90 s and 10 s are kept to complete the verification"
        )
        assert outcome.calls == 0
        assert provider.requests == []
    else:
        assert not isinstance(outcome, LlmCallFailed)
        assert len(provider.requests) == 1


@pytest.mark.parametrize(
    ("stage", "answer"),
    [
        (_review(None), _review_answer),
        (_conventions(None), _conventions_answer),
        (_verifier_model(None), _verifier_answer),
    ],
    ids=["legacy-review", "legacy-conventions", "legacy-verifier"],
)
@pytest.mark.parametrize("seconds_left", [89, 90])
def test_a_legacy_task_keeps_no_time_for_later_stages(
    stage: Stage, answer: Callable[[], httpx.Response], seconds_left: int
) -> None:
    provider = _Provider([answer()])

    _, [outcome] = provider.run(seconds_left, stage)

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


# The EUrouter host: each call is priced at an ECB quote fetched before it.
EUROUTER = replace(_profile("review-model"), base_url="https://api.eurouter.ai/api/v1")
EUROUTER_SETTINGS = replace(
    SETTINGS, primary=EUROUTER, fallback=replace(EUROUTER, model="fallback-model")
)


class _Slow:
    """The FX quote, the ledger read and the manifest write; the ``slow`` one of them takes
    ``seconds`` of the provider's fake clock while the gateway awaits it."""

    def __init__(self, provider: _Provider, slow: str, seconds: float) -> None:
        self._provider = provider
        self._slow = slow
        self._seconds = seconds
        self.quotes = 0
        self.entries: list[ManifestEntry] = []

    def take(self, site: str) -> None:
        if site == self._slow:
            assert self._provider.clock is not None
            self._provider.clock.current += timedelta(seconds=self._seconds)

    async def get_quote(self) -> FxQuoteResult:
        self.quotes += 1
        self.take("fx")
        quote = FxQuote(Decimal("1.1"), NOW.date(), "EXR.D.USD.EUR.SP00.A", NOW)
        return FxQuoteResult(quote, stale_cache=False)

    async def record_manifest(self, run_id: UUID, entry: ManifestEntry) -> None:
        self.entries.append(entry)
        self.take("store")


class _SlowLedger(InMemoryUsageLedger):
    def __init__(self, slow: _Slow) -> None:
        super().__init__()
        self._slow = slow
        self.reads = 0

    async def run_cost_usd(self, run_id: UUID) -> Decimal:
        self.reads += 1
        self._slow.take("ledger")
        return await super().run_cost_usd(run_id)


@pytest.mark.parametrize(
    ("site", "observed"),
    [
        # Refused right after the quote: the ledger is never read.
        pytest.param("fx", (1, 0, 0, 0), id="after the FX quote"),
        # Refused after the ledger read: no slot is claimed and no manifest stored.
        pytest.param("ledger", (1, 1, 0, 0), id="before the slot"),
        # The slot and its manifest are taken; the transport's checkpoint refuses the request.
        pytest.param("store", (1, 1, 1, 1), id="at the transport checkpoint"),
    ],
)
def test_every_admission_check_of_a_snapshot_review_keeps_the_verifier_window(
    site: str, observed: tuple[int, int, int, int]
) -> None:
    # 195 s at the entry pass the 190 s window; 10 s spent in the slow await leave 185 s.
    provider = _Provider([_review_answer()], settings=EUROUTER_SETTINGS)
    slow = _Slow(provider, site, 10)
    ledger = provider.ledger = _SlowLedger(slow)
    provider.fx_provider = provider.manifest_store = slow

    _, [outcome] = provider.run(REVIEW_WINDOW_S + 5, _review(_snapshot()))

    assert isinstance(outcome, LlmCallFailed)
    assert outcome.error_code is LlmErrorCode.VERIFICATION_DEADLINE_EXCEEDED
    assert str(outcome) == (
        "verification_deadline_exceeded: 185 s left before the attempt deadline; "
        "a call needs 90 s and 100 s are kept for later stages"
    )
    # (FX quotes, ledger reads, slots claimed, manifests stored)
    assert (slow.quotes, ledger.reads, outcome.calls, len(slow.entries)) == observed
    assert provider.requests == []


def test_a_negative_time_reserve_is_refused() -> None:
    task = StructuredTask(
        operation="review",
        messages=(ChatMessage("user", "USER"),),
        schema=ResponseSchema("Answer", {"type": "object"}),
        validate=lambda answer: {},
    )

    with pytest.raises(ValueError, match="reserved_seconds must not be negative: -1"):
        replace(task, reserved_seconds=-1)


# ---------- answers the JSON decoder cannot read (review of 172c8c5c..9e4b557c) ----------


@pytest.mark.parametrize(
    "answer",
    [pytest.param(DEEP_NESTING, id="deep nesting"), pytest.param(HUGE_INTEGER, id="huge integer")],
)
@pytest.mark.parametrize(
    ("stage", "valid", "kinds"),
    [
        (_review(_snapshot()), _review_answer, ["primary", "repair"]),
        # Two slots kept: the second conventions call is the fallback, not a repair.
        (_conventions(_snapshot()), _conventions_answer, ["primary", "fallback"]),
    ],
    ids=["snapshot-review", "snapshot-conventions"],
)
def test_a_snapshot_answer_the_decoder_cannot_read_is_recovered(
    stage: Stage, valid: Callable[[], httpx.Response], kinds: list[str], answer: str
) -> None:
    # InvalidAnswer, so the gateway recovers instead of an error escaping the paid call
    # (the run's internal_error).
    provider = _Provider([completion(answer), valid()])

    _, [outcome] = provider.run(3_600, stage)

    assert not isinstance(outcome, LlmCallFailed)
    assert provider.kinds() == kinds


@pytest.mark.parametrize(
    ("answer", "error"),
    [
        pytest.param(DEEP_NESTING, RecursionError, id="deep nesting"),
        pytest.param(HUGE_INTEGER, ValueError, id="huge integer"),
    ],
)
@pytest.mark.parametrize(
    "validate",
    [validate_review_answer, conventions_answer_validator(("app/main.py",))],
    ids=["legacy-review-v1", "legacy-conventions"],
)
def test_legacy_validators_keep_the_live_decoder(
    validate: Callable[[str], object], answer: str, error: type[Exception]
) -> None:
    # F1 dormancy: legacy runs decode as today; their escape is a follow-up outside F1.
    with pytest.raises(error) as raised:
        validate(answer)

    assert type(raised.value) is error
    assert not isinstance(raised.value, InvalidAnswer)


@pytest.mark.parametrize(
    ("answer", "error"),
    [
        pytest.param(DEEP_NESTING, RecursionError, id="deep nesting"),
        pytest.param(HUGE_INTEGER, ValueError, id="huge integer"),
    ],
)
def test_legacy_conventions_keep_the_live_decoder_through_the_adapter(
    answer: str, error: type[Exception]
) -> None:
    # The adapter, not only the validator default: a legacy conventions task decodes as today,
    # so the error escapes. A valid second answer is scripted, so a fail-closed legacy task
    # would recover with it and not raise.
    provider = _Provider([completion(answer), _conventions_answer()])

    with pytest.raises(error) as raised:
        provider.run(3_600, _conventions(None))

    assert type(raised.value) is error
    assert provider.kinds() == ["primary"]
