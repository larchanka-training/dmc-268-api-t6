from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from dataclasses import FrozenInstanceError
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal

import httpx
import pytest

from app.modules.reviews.infrastructure.llm import ecb_fx
from app.modules.reviews.infrastructure.llm.ecb_fx import (
    EcbFxError,
    EcbFxQuoteCache,
    EcbFxRateAdapter,
    FxQuote,
)

ECB_CSV = (
    "KEY,FREQ,CURRENCY,CURRENCY_DENOM,EXR_TYPE,EXR_SUFFIX,TIME_PERIOD,OBS_VALUE,OBS_STATUS\n"
    "EXR.D.USD.EUR.SP00.A,D,USD,EUR,SP00,A,2026-10-05,1.1204,A\n"
)
NOW = datetime(2026, 10, 6, 12, tzinfo=UTC)


class ControlledClock:
    def __init__(self, wall: datetime = NOW, monotonic: float = 100.0) -> None:
        self.wall = wall
        self.monotonic = monotonic

    def wall_now(self) -> datetime:
        return self.wall

    def monotonic_now(self) -> float:
        return self.monotonic

    def advance(self, seconds: float) -> None:
        self.wall += timedelta(seconds=seconds)
        self.monotonic += seconds


def test_cold_cache_fetch_is_single_flight() -> None:
    quote = FxQuote(Decimal("1.1204"), date(2026, 10, 5), "EXR.D.USD.EUR.SP00.A", NOW)

    async def exercise() -> None:
        started = asyncio.Event()
        release = asyncio.Event()
        calls = 0

        class Fetcher:
            async def fetch_latest(self) -> FxQuote:
                nonlocal calls
                calls += 1
                started.set()
                await release.wait()
                return quote

        cache = EcbFxQuoteCache(Fetcher(), wall_clock=lambda: NOW, monotonic_clock=lambda: 100.0)
        tasks = [asyncio.create_task(cache.get_quote()) for _ in range(4)]
        await started.wait()
        assert calls == 1
        release.set()
        results = await asyncio.gather(*tasks)
        assert calls == 1
        assert all(result.quote is quote and result.stale_cache is False for result in results)

    asyncio.run(exercise())


def test_successful_refresh_logs_source_and_date_without_provider_response(
    caplog: pytest.LogCaptureFixture,
) -> None:
    quote = FxQuote(Decimal("1.1204"), date(2026, 10, 5), "EXR.D.USD.EUR.SP00.A", NOW)

    class Fetcher:
        async def fetch_latest(self) -> FxQuote:
            return quote

    cache = EcbFxQuoteCache(Fetcher(), wall_clock=lambda: NOW, monotonic_clock=lambda: 100.0)
    with caplog.at_level(logging.INFO):
        result = asyncio.run(cache.get_quote())

    assert result.quote is quote
    assert "ECB quote refresh succeeded" in caplog.text
    assert "source=EXR.D.USD.EUR.SP00.A" in caplog.text
    assert "observation_date=2026-10-05" in caplog.text
    assert "Authorization" not in caplog.text


def test_refresh_after_one_hour_is_single_flight() -> None:
    first = FxQuote(Decimal("1.1204"), date(2026, 10, 5), "EXR.D.USD.EUR.SP00.A", NOW)
    second = FxQuote(Decimal("1.1300"), date(2026, 10, 6), "EXR.D.USD.EUR.SP00.A", NOW)

    async def exercise() -> None:
        clock = ControlledClock()
        refreshing = asyncio.Event()
        release = asyncio.Event()
        calls = 0

        class Fetcher:
            async def fetch_latest(self) -> FxQuote:
                nonlocal calls
                calls += 1
                if calls == 2:
                    refreshing.set()
                    await release.wait()
                    return second
                return first

        cache = EcbFxQuoteCache(
            Fetcher(), wall_clock=clock.wall_now, monotonic_clock=clock.monotonic_now
        )
        assert (await cache.get_quote()).quote is first
        clock.advance(3599)
        assert (await cache.get_quote()).quote is first
        assert calls == 1
        clock.advance(1)
        tasks = [asyncio.create_task(cache.get_quote()) for _ in range(4)]
        await refreshing.wait()
        assert calls == 2
        release.set()
        results = await asyncio.gather(*tasks)
        assert calls == 2
        assert all(result.quote is second and result.stale_cache is False for result in results)

    asyncio.run(asyncio.wait_for(exercise(), timeout=1.0))


def test_failed_refresh_keeps_quote_and_backs_off_without_logging_body(
    caplog: pytest.LogCaptureFixture,
) -> None:
    first = FxQuote(Decimal("1.1204"), date(2026, 10, 5), "EXR.D.USD.EUR.SP00.A", NOW)
    second = FxQuote(Decimal("1.1300"), date(2026, 10, 6), "EXR.D.USD.EUR.SP00.A", NOW)

    async def exercise() -> None:
        clock = ControlledClock()
        calls = 0

        class Fetcher:
            async def fetch_latest(self) -> FxQuote:
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise EcbFxError("API_KEY=secret raw response body")
                return first if calls == 1 else second

        cache = EcbFxQuoteCache(
            Fetcher(), wall_clock=clock.wall_now, monotonic_clock=clock.monotonic_now
        )
        assert (await cache.get_quote()).quote is first
        clock.advance(3600)
        stale = await cache.get_quote()
        assert stale.quote is first and stale.stale_cache is True
        assert calls == 2
        clock.advance(299)
        stale = await cache.get_quote()
        assert stale.quote is first and stale.stale_cache is True
        assert calls == 2
        clock.advance(1)
        fresh = await cache.get_quote()
        assert fresh.quote is second and fresh.stale_cache is False
        assert calls == 3

    asyncio.run(exercise())
    assert "EXR.D.USD.EUR.SP00.A" in caplog.text
    assert "2026-10-05" in caplog.text
    assert "API_KEY" not in caplog.text
    assert "raw response body" not in caplog.text


def test_cold_fetch_failure_returns_unavailable_and_retries_after_backoff() -> None:
    quote = FxQuote(Decimal("1.1204"), date(2026, 10, 5), "EXR.D.USD.EUR.SP00.A", NOW)

    async def exercise() -> None:
        clock = ControlledClock()
        calls = 0

        class Fetcher:
            async def fetch_latest(self) -> FxQuote:
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise EcbFxError("ECB offline")
                return quote

        cache = EcbFxQuoteCache(
            Fetcher(), wall_clock=clock.wall_now, monotonic_clock=clock.monotonic_now
        )
        unavailable = await cache.get_quote()
        assert unavailable.quote is None and unavailable.stale_cache is False
        clock.advance(299)
        assert (await cache.get_quote()).quote is None
        assert calls == 1
        clock.advance(1)
        recovered = await cache.get_quote()
        assert recovered.quote is quote and recovered.stale_cache is False
        assert calls == 2

    asyncio.run(exercise())


def test_unexpected_fetch_failure_is_single_flight_and_recovers_after_backoff(
    caplog: pytest.LogCaptureFixture,
) -> None:
    quote = FxQuote(Decimal("1.1204"), date(2026, 10, 5), "EXR.D.USD.EUR.SP00.A", NOW)

    async def exercise() -> None:
        clock = ControlledClock()
        started = asyncio.Event()
        release = asyncio.Event()
        calls = 0

        class Fetcher:
            async def fetch_latest(self) -> FxQuote:
                nonlocal calls
                calls += 1
                if calls == 1:
                    started.set()
                    await release.wait()
                    raise RuntimeError("API_KEY=secret raw response body")
                return quote

        cache = EcbFxQuoteCache(
            Fetcher(), wall_clock=clock.wall_now, monotonic_clock=clock.monotonic_now
        )
        tasks = [asyncio.create_task(cache.get_quote()) for _ in range(4)]
        await started.wait()
        release.set()
        results = await asyncio.gather(*tasks)
        assert calls == 1
        assert all(result.quote is None and result.stale_cache is False for result in results)
        clock.advance(299)
        assert (await cache.get_quote()).quote is None
        assert calls == 1
        clock.advance(1)
        assert (await cache.get_quote()).quote is quote
        assert calls == 2

    asyncio.run(asyncio.wait_for(exercise(), timeout=1.0))
    assert "EXR.D.USD.EUR.SP00.A" in caplog.text
    assert "observation_date=none" in caplog.text
    assert "API_KEY" not in caplog.text
    assert "raw response body" not in caplog.text


def test_quote_age_allows_weekend_and_holiday_but_expires_after_seven_calendar_days() -> None:
    friday = datetime(2026, 10, 2, 12, tzinfo=UTC)
    quote = FxQuote(Decimal("1.1204"), date(2026, 10, 2), "EXR.D.USD.EUR.SP00.A", friday)

    async def exercise() -> None:
        clock = ControlledClock(wall=friday)
        calls = 0

        class Fetcher:
            async def fetch_latest(self) -> FxQuote:
                nonlocal calls
                calls += 1
                if calls > 1:
                    raise EcbFxError("ECB closed")
                return quote

        cache = EcbFxQuoteCache(
            Fetcher(), wall_clock=clock.wall_now, monotonic_clock=clock.monotonic_now
        )
        assert (await cache.get_quote()).quote is quote
        for days, expected_quote in ((3, quote), (6, quote), (7, quote), (8, None)):
            clock.advance((days - (clock.wall.date() - quote.observation_date).days) * 86_400)
            result = await cache.get_quote()
            assert result.quote is expected_quote
            if expected_quote is not None:
                assert result.stale_cache is True
        assert calls == 5

    asyncio.run(exercise())


def test_observation_age_uses_utc_calendar_date() -> None:
    quote = FxQuote(Decimal("1.1204"), date(2026, 10, 2), "EXR.D.USD.EUR.SP00.A", NOW)

    async def exercise() -> None:
        clock = ControlledClock(
            wall=datetime(2026, 10, 10, 1, 30, tzinfo=timezone(timedelta(hours=2)))
        )

        class Fetcher:
            async def fetch_latest(self) -> FxQuote:
                return quote

        cache = EcbFxQuoteCache(
            Fetcher(), wall_clock=clock.wall_now, monotonic_clock=clock.monotonic_now
        )
        assert (await cache.get_quote()).quote is quote
        clock.advance(3600)
        assert (await cache.get_quote()).quote is None

    asyncio.run(exercise())


def test_older_refresh_cannot_replace_newer_cached_quote() -> None:
    newer = FxQuote(Decimal("1.1204"), date(2026, 10, 6), "EXR.D.USD.EUR.SP00.A", NOW)
    older = FxQuote(Decimal("1.1000"), date(2026, 10, 5), "EXR.D.USD.EUR.SP00.A", NOW)

    async def exercise() -> None:
        clock = ControlledClock()
        calls = 0

        class Fetcher:
            async def fetch_latest(self) -> FxQuote:
                nonlocal calls
                calls += 1
                return newer if calls == 1 else older

        cache = EcbFxQuoteCache(
            Fetcher(), wall_clock=clock.wall_now, monotonic_clock=clock.monotonic_now
        )
        assert (await cache.get_quote()).quote is newer
        clock.advance(3600)
        result = await cache.get_quote()
        assert result.quote is newer and result.stale_cache is True
        clock.advance(299)
        assert (await cache.get_quote()).quote is newer
        assert calls == 2

    asyncio.run(exercise())


def test_cancelled_fetch_releases_lock_and_later_call_recovers() -> None:
    quote = FxQuote(Decimal("1.1204"), date(2026, 10, 5), "EXR.D.USD.EUR.SP00.A", NOW)

    async def exercise() -> None:
        started = asyncio.Event()
        calls = 0

        class Fetcher:
            async def fetch_latest(self) -> FxQuote:
                nonlocal calls
                calls += 1
                if calls == 1:
                    started.set()
                    await asyncio.Event().wait()
                return quote

        cache = EcbFxQuoteCache(Fetcher(), wall_clock=lambda: NOW)
        first = asyncio.create_task(cache.get_quote())
        await started.wait()
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        result = await asyncio.wait_for(cache.get_quote(), timeout=1.0)
        assert result.quote is quote and result.stale_cache is False
        assert calls == 2

    asyncio.run(asyncio.wait_for(exercise(), timeout=2.0))


def test_fetch_latest_returns_exact_usd_per_eur_quote() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/service/data/EXR/D.USD.EUR.SP00.A"
        assert dict(request.url.params) == {"lastNObservations": "1", "format": "csvdata"}
        assert "authorization" not in request.headers
        return httpx.Response(200, text=ECB_CSV)

    async def fetch() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            quote = await EcbFxRateAdapter(client, clock=lambda: NOW).fetch_latest()
        assert quote.rate_usd_per_eur == Decimal("1.1204")
        assert quote.observation_date == date(2026, 10, 5)
        assert quote.source == "EXR.D.USD.EUR.SP00.A"
        assert quote.retrieved_at == NOW

    asyncio.run(fetch())


def test_quote_is_immutable() -> None:
    async def fetch() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, text=ECB_CSV))
        ) as client:
            quote = await EcbFxRateAdapter(client, clock=lambda: NOW).fetch_latest()
        with pytest.raises(FrozenInstanceError):
            quote.rate_usd_per_eur = Decimal("2")  # type: ignore[misc]

    asyncio.run(fetch())


@pytest.mark.parametrize(
    "csv_body",
    [
        "<html>upstream error</html>",
        ECB_CSV.splitlines()[0] + "\n",
        ECB_CSV + ECB_CSV.splitlines()[1] + "\n",
        ECB_CSV.replace("EXR.D.USD.EUR.SP00.A", "EXR.D.GBP.EUR.SP00.A"),
        ECB_CSV.replace(",D,USD,EUR,SP00,A,", ",M,USD,EUR,SP00,A,"),
        ECB_CSV.replace(",D,USD,EUR,SP00,A,", ",D,GBP,EUR,SP00,A,"),
        ECB_CSV.replace(",2026-10-05,", ",2026-10-07,"),
        ECB_CSV.replace(",2026-10-05,", ",not-a-date,"),
        ECB_CSV.replace(",1.1204,", ",0,"),
        ECB_CSV.replace(",1.1204,", ",-1,"),
        ECB_CSV.replace(",1.1204,", ",NaN,"),
        ECB_CSV.replace(",1.1204,", ",Infinity,"),
        ECB_CSV.replace(",1.1204,", ",no-rate,"),
        ECB_CSV.replace(",1.1204,A", ",1.1204,A,extra"),
        '"unterminated',
    ],
)
def test_fetch_latest_rejects_invalid_csv(csv_body: str) -> None:
    async def fetch() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, text=csv_body))
        ) as client:
            with pytest.raises(EcbFxError):
                await EcbFxRateAdapter(client, clock=lambda: NOW).fetch_latest()

    asyncio.run(fetch())


def test_fetch_latest_rejects_http_failure() -> None:
    async def fetch() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(503, text="unavailable"))
        ) as client:
            with pytest.raises(EcbFxError):
                await EcbFxRateAdapter(client, clock=lambda: NOW).fetch_latest()

    asyncio.run(fetch())


def test_fetch_latest_rejects_network_and_encoding_errors() -> None:
    async def fetch() -> None:
        def disconnected(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("offline", request=request)

        async with httpx.AsyncClient(transport=httpx.MockTransport(disconnected)) as client:
            with pytest.raises(EcbFxError):
                await EcbFxRateAdapter(client, clock=lambda: NOW).fetch_latest()

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, content=b"\xff"))
        ) as client:
            with pytest.raises(EcbFxError):
                await EcbFxRateAdapter(client, clock=lambda: NOW).fetch_latest()

    asyncio.run(fetch())


def test_fetch_latest_limits_body_and_timeout() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        assert request.extensions["timeout"]["read"] == 5.0
        return httpx.Response(200, content=b"x" * 65_537)

    async def fetch() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            with pytest.raises(EcbFxError):
                await EcbFxRateAdapter(client, clock=lambda: NOW).fetch_latest()

    asyncio.run(fetch())


def test_fetch_latest_limits_total_time_for_slow_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ecb_fx, "ECB_TIMEOUT_SECONDS", 0.05, raising=False)
    payload = ECB_CSV.encode()

    class SlowStream(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            for chunk in (payload[:30], payload[30:60], payload[60:]):
                await asyncio.sleep(0.03)
                yield chunk

    async def fetch() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=SlowStream()))
        ) as client:
            with pytest.raises(EcbFxError, match="timed out"):
                await EcbFxRateAdapter(client, clock=lambda: NOW).fetch_latest()

    asyncio.run(fetch())


def test_fetch_latest_never_sends_llm_authorization() -> None:
    requests = 0

    def respond(_: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(200, text=ECB_CSV)

    async def fetch() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(respond), headers={"Authorization": "Bearer secret"}
        ) as client:
            with pytest.raises(EcbFxError):
                await EcbFxRateAdapter(client, clock=lambda: NOW).fetch_latest()

    asyncio.run(fetch())
    assert requests == 0


def test_fetch_latest_rejects_client_auth_before_request() -> None:
    requests = 0

    def respond(_: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(200, text=ECB_CSV)

    async def fetch() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(respond), auth=httpx.BasicAuth("llm", "secret")
        ) as client:
            with pytest.raises(EcbFxError):
                await EcbFxRateAdapter(client, clock=lambda: NOW).fetch_latest()

    asyncio.run(fetch())
    assert requests == 0
