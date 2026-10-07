from __future__ import annotations

import asyncio
import csv
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from io import StringIO
from typing import Protocol

import httpx

ECB_SERIES = "EXR.D.USD.EUR.SP00.A"
ECB_URL = "https://data-api.ecb.europa.eu/service/data/EXR/D.USD.EUR.SP00.A"
MAX_CSV_BYTES = 65_536
ECB_TIMEOUT_SECONDS = 5.0
REFRESH_INTERVAL_SECONDS = 3_600.0
FAILURE_BACKOFF_SECONDS = 300.0
# Without a usable quote every EUrouter call fails before the provider, so this pause
# ends before the 30 s first run retry (PIPELINE_SPEC §4.2) and still sends at most one
# ECB request per process every 10 s.
COLD_FAILURE_BACKOFF_SECONDS = 10.0
MAX_OBSERVATION_AGE_DAYS = 7
logger = logging.getLogger(__name__)


class EcbFxError(ValueError):
    """The current ECB observation could not be safely obtained."""


@dataclass(frozen=True)
class FxQuote:
    rate_usd_per_eur: Decimal
    observation_date: date
    source: str
    retrieved_at: datetime


@dataclass(frozen=True)
class FxQuoteResult:
    quote: FxQuote | None
    stale_cache: bool


class FxQuoteFetcher(Protocol):
    async def fetch_latest(self) -> FxQuote: ...


class FxQuoteProvider(Protocol):
    async def get_quote(self) -> FxQuoteResult: ...


class EcbFxQuoteCache:
    def __init__(
        self,
        fetcher: FxQuoteFetcher,
        *,
        wall_clock: Callable[[], datetime] | None = None,
        monotonic_clock: Callable[[], float] | None = None,
    ) -> None:
        self._fetcher = fetcher
        self._wall_clock = wall_clock or (lambda: datetime.now(UTC))
        self._monotonic_clock = monotonic_clock or time.monotonic
        self._lock = asyncio.Lock()
        self._quote: FxQuote | None = None
        self._last_success_mono: float | None = None
        self._last_failure_mono: float | None = None

    async def get_quote(self) -> FxQuoteResult:
        async with self._lock:
            now = self._monotonic_clock()
            last_success = self._last_success_mono
            usable = self._usable_quote()
            if (
                self._quote is None
                or last_success is None
                or now - last_success >= REFRESH_INTERVAL_SECONDS
                or usable is None
            ):
                last_failure = self._last_failure_mono
                backoff = (
                    FAILURE_BACKOFF_SECONDS if usable is not None else COLD_FAILURE_BACKOFF_SECONDS
                )
                if last_failure is not None and now - last_failure < backoff:
                    return self._result(stale_cache=True)
                try:
                    quote = await self._fetcher.fetch_latest()
                    if (
                        self._quote is not None
                        and quote.observation_date < self._quote.observation_date
                    ):
                        raise EcbFxError("ECB returned an older observation")
                    if not self._is_usable(quote):
                        raise EcbFxError("ECB returned an expired observation")
                except Exception:
                    # CancelledError is a BaseException, so cancellation still releases the lock.
                    self._last_failure_mono = self._monotonic_clock()
                    logger.warning(
                        "ECB quote refresh failed; source=%s observation_date=%s",
                        ECB_SERIES,
                        self._quote.observation_date.isoformat() if self._quote else "none",
                    )
                    return self._result(stale_cache=True)
                self._quote = quote
                self._last_success_mono = self._monotonic_clock()
                self._last_failure_mono = None
                logger.info(
                    "ECB quote refresh succeeded; source=%s observation_date=%s",
                    ECB_SERIES,
                    quote.observation_date.isoformat(),
                )
            return self._result(stale_cache=False)

    def _is_usable(self, quote: FxQuote) -> bool:
        now = self._wall_clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise EcbFxError("ECB quote clock must be timezone-aware")
        age_days = (now.astimezone(UTC).date() - quote.observation_date).days
        return 0 <= age_days <= MAX_OBSERVATION_AGE_DAYS

    def _usable_quote(self) -> FxQuote | None:
        if self._quote is not None and self._is_usable(self._quote):
            return self._quote
        return None

    def _result(self, *, stale_cache: bool) -> FxQuoteResult:
        quote = self._usable_quote()
        return FxQuoteResult(quote, stale_cache=stale_cache and quote is not None)


class EcbFxRateAdapter:
    def __init__(
        self, client: httpx.AsyncClient, *, clock: Callable[[], datetime] | None = None
    ) -> None:
        self._client = client
        self._clock = clock or (lambda: datetime.now(UTC))

    async def fetch_latest(self) -> FxQuote:
        if self._client.auth is not None or any(
            key in self._client.headers
            for key in ("authorization", "proxy-authorization", "x-api-key")
        ):
            raise EcbFxError("ECB client must not carry credentials")
        try:
            async with asyncio.timeout(ECB_TIMEOUT_SECONDS):
                async with self._client.stream(
                    "GET",
                    ECB_URL,
                    params={"lastNObservations": "1", "format": "csvdata"},
                    headers={"Accept": "text/csv"},
                    timeout=ECB_TIMEOUT_SECONDS,
                ) as response:
                    response.raise_for_status()
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(body) + len(chunk) > MAX_CSV_BYTES:
                            raise EcbFxError("ECB CSV response is too large")
                        body.extend(chunk)
        except TimeoutError as error:
            raise EcbFxError("ECB request timed out") from error
        except httpx.HTTPError as error:
            raise EcbFxError("ECB request failed") from error

        retrieved_at = self._clock()
        if retrieved_at.tzinfo is None or retrieved_at.utcoffset() is None:
            raise EcbFxError("ECB quote clock must be timezone-aware")
        retrieved_at = retrieved_at.astimezone(UTC)
        try:
            reader = csv.DictReader(StringIO(body.decode("utf-8-sig"), newline=""), strict=True)
            required = {
                "KEY",
                "FREQ",
                "CURRENCY",
                "CURRENCY_DENOM",
                "EXR_TYPE",
                "EXR_SUFFIX",
                "TIME_PERIOD",
                "OBS_VALUE",
            }
            fields = reader.fieldnames
            if fields is None or not required.issubset(fields) or len(set(fields)) != len(fields):
                raise EcbFxError("ECB CSV header is invalid")
            rows = list(reader)
            if (
                len(rows) != 1
                or None in rows[0]
                or any(value is None for value in rows[0].values())
            ):
                raise EcbFxError("ECB CSV must contain exactly one complete observation")
            row = rows[0]
            if any(
                row[key] != expected
                for key, expected in (
                    ("KEY", ECB_SERIES),
                    ("FREQ", "D"),
                    ("CURRENCY", "USD"),
                    ("CURRENCY_DENOM", "EUR"),
                    ("EXR_TYPE", "SP00"),
                    ("EXR_SUFFIX", "A"),
                )
            ):
                raise EcbFxError("ECB CSV series does not match USD per EUR daily spot")
            date_text = row["TIME_PERIOD"]
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_text) is None:
                raise EcbFxError("ECB observation date is invalid")
            observation_date = date.fromisoformat(date_text)
            rate = Decimal(row["OBS_VALUE"])
            if observation_date > retrieved_at.date() or not rate.is_finite() or rate <= 0:
                raise EcbFxError("ECB observation date or rate is invalid")
        except (UnicodeDecodeError, csv.Error, ValueError, InvalidOperation) as error:
            if isinstance(error, EcbFxError):
                raise
            raise EcbFxError("ECB CSV is invalid") from error
        return FxQuote(
            rate_usd_per_eur=rate,
            observation_date=observation_date,
            source=ECB_SERIES,
            retrieved_at=retrieved_at,
        )
