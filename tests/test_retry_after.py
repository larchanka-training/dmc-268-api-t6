"""``Retry-After`` of a GitHub read: delta-seconds or an HTTP-date, capped at a day (#168)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

# Through the module, so each case fails on its own while the parser is missing.
from app.modules.reviews.application import run_failures

# The response Date of the dated cases below.
_DATE = "Tue, 27 Oct 2026 07:00:00 GMT"


def _never() -> datetime:
    raise AssertionError("now() is read only for an HTTP-date without a readable Date")


@pytest.mark.parametrize(
    ("value", "delay"),
    [
        pytest.param("0", timedelta(0), id="zero"),
        pytest.param("120", timedelta(seconds=120), id="seconds"),
        pytest.param("000120", timedelta(seconds=120), id="leading-zeros"),
        pytest.param("0" * 5000 + "120", timedelta(seconds=120), id="many-leading-zeros"),
        pytest.param("86400", timedelta(days=1), id="one-day"),
        pytest.param("86401", timedelta(days=1), id="beyond-a-day"),
        pytest.param("86399999999999", timedelta(days=1), id="largest-timedelta-seconds"),
        pytest.param("9" * 5000, timedelta(days=1), id="integer-digit-limit"),
    ],
)
def test_ascii_seconds_are_the_delay_up_to_the_ceiling(value: str, delay: timedelta) -> None:
    assert run_failures.parse_retry_after(value, date=_DATE, now=_never) == delay


@pytest.mark.parametrize(
    ("value", "delay"),
    [
        pytest.param("Tue, 27 Oct 2026 07:02:00 GMT", timedelta(seconds=120), id="two-minutes"),
        pytest.param("Tue, 27 Oct 2026 09:02:00 +0200", timedelta(seconds=120), id="other-zone"),
        pytest.param("Tue, 27 Oct 2026 07:00:00 GMT", timedelta(0), id="same-instant"),
        pytest.param("Tue, 27 Oct 2026 06:59:00 GMT", timedelta(0), id="past"),
        pytest.param("Wed, 28 Oct 2026 07:00:00 GMT", timedelta(days=1), id="one-day"),
        pytest.param("Thu, 29 Oct 2026 07:00:00 GMT", timedelta(days=1), id="beyond-a-day"),
        pytest.param("Fri, 31 Dec 9999 23:59:59 -2359", timedelta(days=1), id="latest-date"),
        # RFC 9110 5.6.7: every HTTP-date is GMT and all three formats must be accepted.
        pytest.param("Tuesday, 27-Oct-26 07:02:00 GMT", timedelta(seconds=120), id="rfc850"),
        pytest.param("Tue Oct 27 07:02:00 2026", timedelta(seconds=120), id="asctime"),
        pytest.param("Tue, 27 Oct 2026 07:02:00 -0000", timedelta(seconds=120), id="minus-zero"),
        pytest.param("Tue, 27 Oct 2026 07:02:00", timedelta(seconds=120), id="without-zone"),
    ],
)
def test_an_http_date_counts_from_the_response_date(value: str, delay: timedelta) -> None:
    assert run_failures.parse_retry_after(value, date=_DATE, now=_never) == delay


@pytest.mark.parametrize(
    "date",
    [
        "Tuesday, 27-Oct-26 06:00:00 GMT",
        "Tue Oct 27 06:00:00 2026",
        "Tue, 27 Oct 2026 06:00:00 -0000",
    ],
    ids=["rfc850", "asctime", "minus-zero"],
)
def test_a_response_date_in_any_http_date_format_is_gmt(date: str) -> None:
    delay = run_failures.parse_retry_after("Tue, 27 Oct 2026 07:05:00 GMT", date=date, now=_never)

    assert delay == timedelta(minutes=65)


@pytest.mark.parametrize(
    "date",
    [None, "", "soon", "Tue, 32 Oct 2026 06:00:00 GMT"],
    ids=["absent", "empty", "word", "impossible-day"],
)
def test_an_http_date_counts_from_now_without_a_readable_response_date(date: str | None) -> None:
    def now() -> datetime:
        return datetime(2026, 10, 27, 7, 0, tzinfo=UTC)

    delay = run_failures.parse_retry_after("Tue, 27 Oct 2026 07:05:00 GMT", date=date, now=now)

    assert delay == timedelta(minutes=5)


def test_the_default_now_is_the_current_utc_time() -> None:
    assert run_failures.parse_retry_after("Sun, 06 Nov 1994 08:49:37 GMT") == timedelta(0)
    assert run_failures.parse_retry_after("Fri, 31 Dec 9999 23:59:59 GMT") == timedelta(days=1)


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        "later",
        "-1",
        "+120",
        "120.0",
        " 120",
        "\u00b2",
        "\u0661\u0662\u0660",
        "Tue, 32 Oct 2026 07:02:00 GMT",
    ],
    ids=[
        "absent",
        "empty",
        "word",
        "negative",
        "positive-sign",
        "fractional",
        "whitespace",
        "superscript-digit",
        "unicode-decimal",
        "impossible-day",
    ],
)
def test_anything_else_is_no_delay(value: str | None) -> None:
    assert run_failures.parse_retry_after(value, date=_DATE, now=_never) is None
