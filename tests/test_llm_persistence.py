"""``usage_events`` and ``llm.call`` adapters: one short transaction per write (Р-8, §2)."""

from __future__ import annotations

import asyncio
import json
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, cast
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.analytics.infrastructure.models import UsageEvent
from app.modules.analytics.infrastructure.usage_ledger import SqlAlchemyUsageLedger
from app.modules.reviews.application.llm import (
    LlmCallError,
    LlmCallKind,
    LlmCallRecord,
    LlmErrorCode,
    LlmUsage,
    RunCallContext,
    serialized_size,
)
from app.modules.reviews.infrastructure.llm_call_trace import (
    INLINE_RESPONSE_LIMIT,
    SqlAlchemyLlmCallTrace,
    inline_response,
)
from app.modules.reviews.infrastructure.models import RunAction

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


def test_llm_call_is_appended_as_the_next_run_action() -> None:
    factory = FakeFactory(6)
    record = _record(
        error=LlmCallError(LlmErrorCode.TIMEOUT, None, "no answer within 90 s (ReadTimeout)")
    )

    asyncio.run(SqlAlchemyLlmCallTrace(_factory(factory)).record_call(RUN_ID, record))

    assert factory.opened == ["transaction"]
    (action,) = factory.session.added
    assert isinstance(action, RunAction)
    assert (action.run_id, action.index, action.tool) == (RUN_ID, 7, "llm.call")
    assert action.request == {
        "kind": "fallback",
        "model": "mistral-small-4",
        "call_no": 3,
        "attempt": 2,
        "timeout_s": 90.0,
        "prompt_version_id": None,
        "rule_version_id": None,
        "input_tokens_estimate": 4200,
    }
    assert action.response == {
        "error": {
            "class": "llm_timeout",
            "http_status": None,
            "message": "no answer within 90 s (ReadTimeout)",
        }
    }
    assert action.response_ref is None
    assert (action.started_at, action.duration_ms) == (STARTED, 1234)


def test_response_up_to_64_kb_is_stored_inline_as_is() -> None:
    body = {"choices": [{"message": {"content": "x" * 1000}}]}

    assert inline_response(body) is body


def test_larger_response_is_cut_to_a_marked_utf8_safe_prefix() -> None:
    body = {"content": "я" * 50_000}
    original = serialized_size(body)

    stored = inline_response(body)

    assert original > INLINE_RESPONSE_LIMIT
    assert stored["truncated"] is True
    assert stored["original_bytes"] == original
    assert serialized_size(stored) <= INLINE_RESPONSE_LIMIT
    assert serialized_size(stored) > INLINE_RESPONSE_LIMIT - 8
    assert json.dumps(body, ensure_ascii=False, separators=(",", ":")).startswith(stored["text"])
