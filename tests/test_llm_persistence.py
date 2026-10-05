"""``usage_events`` and ``llm.call`` adapters: one short transaction per write (Р-8, §2)."""

from __future__ import annotations

import asyncio
from contextlib import AbstractAsyncContextManager
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, cast
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.analytics.infrastructure.models import UsageEvent
from app.modules.analytics.infrastructure.usage_ledger import SqlAlchemyUsageLedger
from app.modules.reviews.application.llm import (
    FxProvenance,
    LlmCallError,
    LlmCallKind,
    LlmCallRecord,
    LlmErrorCode,
    LlmUsage,
    RunCallContext,
)
from app.modules.reviews.infrastructure.llm_call_trace import RunTraceLlmCalls

RUN_ID = UUID("00000000-0000-0000-0000-000000003311")
WORKSPACE_ID = UUID("00000000-0000-0000-0000-000000003312")
STARTED = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)


class FakeSession:
    def __init__(self, scalar: Any) -> None:
        self.scalar_value = scalar
        self.added: list[object] = []
        self.statements: list[object] = []

    async def scalar(self, statement: object) -> Any:
        self.statements.append(statement)
        return self.scalar_value

    def add(self, item: object) -> None:
        self.added.append(item)


class FakeContext(AbstractAsyncContextManager[FakeSession]):
    def __init__(self, factory: FakeFactory, kind: str) -> None:
        self._factory = factory
        self._kind = kind

    async def __aenter__(self) -> FakeSession:
        self._factory.opened.append(self._kind)
        return self._factory.session

    async def __aexit__(self, *args: object) -> None:
        return None


class FakeFactory:
    def __init__(self, scalar: Any) -> None:
        self.session = FakeSession(scalar)
        self.opened: list[str] = []

    def __call__(self) -> FakeContext:
        return FakeContext(self, "read")

    def begin(self) -> FakeContext:
        return FakeContext(self, "transaction")


def _factory(factory: FakeFactory) -> async_sessionmaker[AsyncSession]:
    return cast(async_sessionmaker[AsyncSession], factory)


def test_usage_ledger_sums_the_run_cost_and_inserts_one_event_per_call() -> None:
    factory = FakeFactory(Decimal("0.125000"))
    ledger = SqlAlchemyUsageLedger(_factory(factory))
    context = RunCallContext(RUN_ID, WORKSPACE_ID, 1, "fast", STARTED + timedelta(minutes=8))

    spent = asyncio.run(ledger.run_cost_usd(RUN_ID))
    asyncio.run(
        ledger.record(
            context,
            LlmUsage("eurouter", "gpt-4.1-mini-2025", "review", 1000, 200, 300, Decimal("0.0011")),
        )
    )

    assert spent == Decimal("0.125000")
    assert "sum(usage_events.cost_usd)" in str(factory.session.statements[0])
    assert factory.opened == ["read", "transaction"]
    (event,) = factory.session.added
    assert isinstance(event, UsageEvent)
    assert (event.run_id, event.workspace_id) == (RUN_ID, WORKSPACE_ID)
    assert (event.provider, event.model, event.operation) == (
        "eurouter",
        "gpt-4.1-mini-2025",
        "review",
    )
    assert (event.tokens_in, event.tokens_out, event.cache_read_tokens) == (1000, 200, 300)
    assert event.cost_usd == Decimal("0.0011")


def test_usage_ledger_converts_a_float_sum_without_binary_noise() -> None:
    spent = asyncio.run(SqlAlchemyUsageLedger(_factory(FakeFactory(0.1))).run_cost_usd(RUN_ID))

    assert spent == Decimal("0.1")


def test_usage_ledger_reads_zero_for_a_run_without_events() -> None:
    assert asyncio.run(SqlAlchemyUsageLedger(_factory(FakeFactory(None))).run_cost_usd(RUN_ID)) == 0


def _record(**overrides: Any) -> LlmCallRecord:
    values: dict[str, Any] = dict(
        kind=LlmCallKind.FALLBACK,
        model="mistral-small-4",
        call_no=3,
        attempt=2,
        timeout_s=90.0,
        prompt_version_id=None,
        rule_version_id=None,
        input_tokens_estimate=4200,
        started_at=STARTED,
        duration_ms=1234,
    )
    values.update(overrides)
    return LlmCallRecord(**values)


class RecordingRunTrace:
    def __init__(self) -> None:
        self.records: list[tuple[UUID, str, dict[str, Any], Any, datetime, int]] = []

    async def record(
        self,
        run_id: UUID,
        tool: str,
        request: dict[str, Any],
        response: Any,
        started_at: datetime,
        duration_ms: int,
    ) -> None:
        self.records.append((run_id, tool, request, response, started_at, duration_ms))


def test_llm_call_goes_through_the_shared_run_trace() -> None:
    trace = RecordingRunTrace()
    record = _record(
        error=LlmCallError(LlmErrorCode.TIMEOUT, None, "no answer within 90 s (ReadTimeout)")
    )

    asyncio.run(RunTraceLlmCalls(trace).record_call(RUN_ID, record))

    assert trace.records == [
        (
            RUN_ID,
            "llm.call",
            {
                "kind": "fallback",
                "model": "mistral-small-4",
                "call_no": 3,
                "attempt": 2,
                "timeout_s": 90.0,
                "prompt_version_id": None,
                "rule_version_id": None,
                "input_tokens_estimate": 4200,
            },
            {
                "error": {
                    "class": "llm_timeout",
                    "http_status": None,
                    "message": "no answer within 90 s (ReadTimeout)",
                }
            },
            STARTED,
            1234,
        )
    ]


def test_llm_call_persists_fx_in_request_metadata_without_changing_raw_response() -> None:
    trace = RecordingRunTrace()
    raw_response = {"usage": {"cost": 0.125, "cost_currency": "EUR"}}
    record = _record(
        response=raw_response,
        fx=FxProvenance(
            "EXR.D.USD.EUR.SP00.A", date(2026, 10, 5), Decimal("1.20"), stale_cache=True
        ),
    )

    asyncio.run(RunTraceLlmCalls(trace).record_call(RUN_ID, record))

    stored = trace.records[0]
    assert stored[2]["fx"] == {
        "source": "EXR.D.USD.EUR.SP00.A",
        "observation_date": "2026-10-05",
        "rate_usd_per_eur": "1.20",
        "stale_cache": True,
    }
    assert stored[3] is raw_response
