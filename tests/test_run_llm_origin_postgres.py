"""Production Run-origin protection at actual LLM HTTP and paid ledger boundaries."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.bootstrap.llm_gateway import build_gateway
from app.modules.analytics.infrastructure.usage_ledger import SqlAlchemyUsageLedger
from app.modules.reviews.application.llm import RunCallContext
from app.modules.reviews.application.run_failures import RunCancelled
from app.modules.reviews.infrastructure.llm.answers import InvalidAnswer
from app.modules.reviews.infrastructure.llm.ecb_fx import FxQuote, FxQuoteResult
from app.modules.reviews.infrastructure.llm.gateway import StructuredTask
from app.modules.reviews.infrastructure.llm.settings import (
    GatewayPolicy,
    LlmSettings,
    ModelPrice,
    ModelProfile,
)
from app.modules.reviews.infrastructure.llm.transport import ChatMessage, ResponseSchema
from tests.portal_postgres import INSTALL_A, REPO_A, RUN_ATTEMPTED, WS_A, Env, portal_schema
from tests.test_llm_gateway import completion
from tests.test_run_diff_origin_postgres import history
from tests.test_run_origin_guard_postgres import prepare
from tests.test_run_publication_origin_postgres import retire


@pytest.fixture
def llm_env() -> Iterator[Env]:
    database_url = os.environ.get("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("set TEST_DATABASE_URL to run PostgreSQL integration tests")
    with portal_schema(database_url, None) as env:
        yield env


@pytest.mark.integration
@pytest.mark.parametrize("stage", ["retry", "rotate", "fallback", "repair", "paid"])
@pytest.mark.parametrize("retired", [False, True])
def test_production_gateway_stops_retry_or_next_key_after_source_retirement(
    llm_env: Env, stage: str, retired: bool
) -> None:
    async def exercise() -> None:
        engine = llm_env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        calls: list[str] = []

        async def http(request: httpx.Request) -> httpx.Response:
            calls.append(request.headers["Authorization"])
            async with factory.begin() as session:
                for table, row_id in (
                    ("provider_installations", INSTALL_A),
                    ("repositories", REPO_A),
                    ("runs", RUN_ATTEMPTED),
                ):
                    await session.execute(
                        text(f"SELECT id FROM {table} WHERE id=:id FOR UPDATE NOWAIT"),
                        {"id": row_id},
                    )
            if len(calls) == 1:
                if retired:
                    await retire(factory)
                if stage == "paid":
                    return completion("{}")
                if stage == "repair":
                    return completion("bad JSON")
                return httpx.Response(
                    {"retry": 503, "rotate": 401, "fallback": 402}[stage],
                    json={"error": {"message": "try again"}},
                )
            return completion("{}")

        try:
            await prepare(factory, "active", "running")
            profile = ModelProfile(
                provider="self-hosted",
                base_url="https://model.test/v1",
                model="local-model",
                context_window=100_000,
                max_output_tokens=1000,
                price=ModelPrice(Decimal("1"), Decimal("2")),
                api_keys=("one", "two") if stage == "rotate" else ("one",),
            )
            settings = LlmSettings(
                primary=profile,
                fallback=replace(profile, model="fallback-model") if stage == "fallback" else None,
                policy=GatewayPolicy(unavailable_retry_delays_s=(0.001,), max_jitter_s=0),
            )
            async with httpx.AsyncClient(transport=httpx.MockTransport(http)) as client:
                gateway = build_gateway(settings, client, factory)
                task = StructuredTask(
                    operation="review",
                    messages=(ChatMessage("user", "Review this diff"),),
                    schema=ResponseSchema("ReviewOutput", {"type": "object"}),
                    validate=parse_object,
                )
                context = RunCallContext(
                    run_id=RUN_ATTEMPTED,
                    workspace_id=WS_A,
                    attempt=1,
                    engine="fast",
                    deadline=datetime.now(UTC) + timedelta(minutes=10),
                )
                if retired and stage != "paid":
                    with pytest.raises(RunCancelled, match="installation_unavailable"):
                        await gateway.generate(task, context)
                else:
                    assert (await gateway.generate(task, context)).output == {}
            assert calls == (
                ["Bearer one"]
                if retired or stage == "paid"
                else (
                    ["Bearer one", "Bearer two"]
                    if stage == "rotate"
                    else ["Bearer one", "Bearer one"]
                )
            )
            async with factory() as session:
                assert await session.scalar(
                    text("SELECT count(*) FROM usage_events WHERE run_id=:id"),
                    {"id": RUN_ATTEMPTED},
                ) == (1 if stage == "paid" else (int(not retired) + int(stage == "repair")))
                if stage in {"paid", "repair"}:
                    answers = list(
                        (
                            await session.scalars(
                                text(
                                    "SELECT response FROM run_actions WHERE run_id=:id "
                                    "AND tool='llm.call' ORDER BY index"
                                ),
                                {"id": RUN_ATTEMPTED},
                            )
                        ).all()
                    )
                    assert len(answers) == (1 if retired or stage == "paid" else 2)
                    assert answers[0]["choices"][0]["message"]["content"] == (
                        "bad JSON" if stage == "repair" else "{}"
                    )
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def parse_object(content: str) -> dict[str, object]:
    try:
        result = json.loads(content)
    except json.JSONDecodeError as error:
        raise InvalidAnswer([str(error)]) from error
    assert isinstance(result, dict)
    return result


def simple_task() -> StructuredTask:
    return StructuredTask(
        operation="review",
        messages=(ChatMessage("user", "Review this diff"),),
        schema=ResponseSchema("ReviewOutput", {"type": "object"}),
        validate=parse_object,
    )


def run_context(run_id: UUID = RUN_ATTEMPTED) -> RunCallContext:
    return RunCallContext(
        run_id=run_id,
        workspace_id=WS_A,
        attempt=1,
        engine="fast",
        deadline=datetime.now(UTC) + timedelta(minutes=10),
    )


@pytest.mark.integration
@pytest.mark.parametrize("origin", ["missing", "unknown", "rebound", "retired"])
def test_production_gateway_refuses_unproven_origin_before_http(llm_env: Env, origin: str) -> None:
    async def exercise() -> None:
        engine = llm_env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        calls: list[str] = []

        def http(request: httpx.Request) -> httpx.Response:
            calls.append(str(request.url))
            return completion("{}")

        try:
            await prepare(factory, "active" if origin == "missing" else origin, "running")
            settings = LlmSettings.from_env({"LLM_MODEL": "mistral-small-4", "LLM_API_KEYS": "one"})
            async with httpx.AsyncClient(transport=httpx.MockTransport(http)) as client:
                gateway = build_gateway(settings, client, factory)
                with pytest.raises(RunCancelled, match="installation_unavailable"):
                    await gateway.generate(
                        simple_task(),
                        run_context(uuid4() if origin == "missing" else RUN_ATTEMPTED),
                    )
            assert calls == []
            async with factory() as session:
                assert await session.scalar(text("SELECT count(*) FROM usage_events")) == 0
                assert (
                    await session.scalar(
                        text("SELECT count(*) FROM run_actions WHERE tool='llm.call'")
                    )
                    == 0
                )
        finally:
            await engine.dispose()

    asyncio.run(exercise())


@pytest.mark.integration
@pytest.mark.parametrize("boundary", ["fx", "ledger"])
@pytest.mark.parametrize("retired", [False, True])
def test_production_gateway_rechecks_after_awaited_pricing_reads(
    llm_env: Env, monkeypatch: pytest.MonkeyPatch, boundary: str, retired: bool
) -> None:
    async def exercise() -> None:
        engine = llm_env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        calls: list[str] = []
        reads: list[str] = []

        def http(request: httpx.Request) -> httpx.Response:
            calls.append(str(request.url))
            return completion("{}")

        original_cost = SqlAlchemyUsageLedger.run_cost_usd

        async def ledger_read(ledger: SqlAlchemyUsageLedger, run_id: UUID) -> Decimal:
            cost = await original_cost(ledger, run_id)
            reads.append("ledger")
            if retired and boundary == "ledger":
                await retire(factory)
            return cost

        class Quote:
            async def get_quote(self) -> FxQuoteResult:
                reads.append("fx")
                if retired and boundary == "fx":
                    await retire(factory)
                return FxQuoteResult(
                    FxQuote(Decimal("1.2"), datetime.now(UTC).date(), "test", datetime.now(UTC)),
                    stale_cache=False,
                )

        try:
            await prepare(factory, "active", "running")
            monkeypatch.setattr(SqlAlchemyUsageLedger, "run_cost_usd", ledger_read)
            settings = LlmSettings.from_env({"LLM_MODEL": "mistral-small-4", "LLM_API_KEYS": "one"})
            async with httpx.AsyncClient(transport=httpx.MockTransport(http)) as client:
                gateway = build_gateway(settings, client, factory, fx_provider=Quote())
                if retired:
                    with pytest.raises(RunCancelled, match="installation_unavailable"):
                        await gateway.generate(simple_task(), run_context())
                else:
                    assert (await gateway.generate(simple_task(), run_context())).output == {}
            assert boundary in reads
            assert len(calls) == int(not retired)
        finally:
            await engine.dispose()

    asyncio.run(exercise())


@pytest.mark.integration
def test_inflight_paid_answer_keeps_usage_and_audit_without_changing_terminal_history(
    llm_env: Env,
) -> None:
    async def exercise() -> None:
        engine = llm_env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        before: dict[str, object] = {}
        calls: list[str] = []

        async def http(request: httpx.Request) -> httpx.Response:
            nonlocal before
            calls.append(str(request.url))
            await retire(factory)
            async with factory.begin() as session:
                await session.execute(
                    text(
                        "UPDATE runs SET state='cancelled',error_code='installation_unavailable',"
                        "finished_at=clock_timestamp() WHERE id=:id"
                    ),
                    {"id": RUN_ATTEMPTED},
                )
            before = await history(factory)
            return completion("{}")

        try:
            await prepare(factory, "active", "running")
            profile = ModelProfile(
                "self-hosted",
                "https://model.test/v1",
                "local-model",
                100_000,
                1000,
                ModelPrice(Decimal("1"), Decimal("2")),
                api_keys=("one",),
            )
            async with httpx.AsyncClient(transport=httpx.MockTransport(http)) as client:
                gateway = build_gateway(
                    LlmSettings(primary=profile, fallback=None), client, factory
                )
                assert (await gateway.generate(simple_task(), run_context())).output == {}
            after = await history(factory)
            assert len(calls) == 1
            for key in ("run", "code_change_diffs", "findings", "comments"):
                assert after[key] == before[key]
            async with factory() as session:
                assert (
                    await session.scalar(
                        text("SELECT count(*) FROM usage_events WHERE run_id=:id"),
                        {"id": RUN_ATTEMPTED},
                    )
                    == 1
                )
                assert (
                    await session.scalar(
                        text(
                            "SELECT count(*) FROM run_actions WHERE run_id=:id AND tool='llm.call'"
                        ),
                        {"id": RUN_ATTEMPTED},
                    )
                    == 1
                )
        finally:
            await engine.dispose()

    asyncio.run(exercise())
