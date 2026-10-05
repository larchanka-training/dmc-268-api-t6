"""Provider cost metadata and the explicit EUR conversion-rate configuration (#66)."""

from __future__ import annotations

import asyncio
import json
import logging
from decimal import Decimal
from typing import Any

import httpx
import pytest

from app.modules.reviews.infrastructure.llm.settings import LlmConfigError, LlmSettings
from app.modules.reviews.infrastructure.llm.transport import (
    ChatMessage,
    ChatRequest,
    ChatResponse,
    OpenAICompatibleTransport,
    TransportPaidAnswerError,
)

_ENV = {"LLM_MODEL": "mistral-small-4", "LLM_API_KEYS": "sk-test"}


def _answer(usage: str) -> str:
    return (
        '{"model":"mistral-small-4","choices":[{"message":{"content":"{}"},'
        '"finish_reason":"stop"}],"usage":' + usage + "}"
    )


def _complete(body: str) -> ChatResponse:
    settings = LlmSettings.from_env(_ENV)
    request = ChatRequest(
        profile=settings.primary,
        messages=(ChatMessage("user", "Review this diff"),),
        response_schema=None,
        timeout_s=1,
    )

    async def call() -> ChatResponse:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, text=body))
        ) as client:
            return await OpenAICompatibleTransport(client).complete(request)

    return asyncio.run(call())


@pytest.mark.parametrize("currency", ["USD", "EUR"])
def test_provider_cost_keeps_exact_decimal_and_declared_currency(currency: str) -> None:
    response = _complete(
        _answer(
            '{"prompt_tokens":10,"completion_tokens":2,'
            f'"cost":0.123456789012345678901234,"cost_currency":"{currency}"' + "}"
        )
    )

    assert response.cost == Decimal("0.123456789012345678901234")
    assert response.cost_currency == currency
    assert response.raw["usage"]["cost_currency"] == currency


def test_zero_cost_is_present_and_not_confused_with_missing_cost() -> None:
    response = _complete(_answer('{"cost":0,"cost_currency":"EUR"}'))

    assert response.cost == Decimal(0)
    assert response.cost_currency == "EUR"


def test_missing_currency_keeps_legacy_usd_interpretation_with_context_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING):
        response = _complete(_answer('{"cost":0.125,"prompt_tokens":1}'))

    assert response.cost == Decimal("0.125")
    assert response.cost_currency is None
    assert any(
        record.__dict__.get("provider") == "eurouter"
        and record.__dict__.get("model") == "mistral-small-4"
        for record in caplog.records
        if "currency" in record.message
    )
    assert "sk-test" not in caplog.text


def test_missing_currency_warning_uses_configured_model_not_provider_text(
    caplog: pytest.LogCaptureFixture,
) -> None:
    body = _answer('{"cost":0.125}').replace(
        '"model":"mistral-small-4"', '"model":"sk-CANARY-DO-NOT-LOG"'
    )

    with caplog.at_level(logging.WARNING):
        _complete(body)

    assert any(
        record.__dict__.get("model") == "mistral-small-4"
        for record in caplog.records
        if "currency" in record.message
    )
    assert "sk-CANARY-DO-NOT-LOG" not in caplog.text


@pytest.mark.parametrize("currency", ['"GBP"', '""', "123"])
def test_unknown_or_invalid_currency_is_a_classified_error(currency: str) -> None:
    with pytest.raises(TransportPaidAnswerError, match="unsupported usage.cost_currency") as caught:
        _complete(
            _answer(
                '{"prompt_tokens":11,"completion_tokens":7,'
                '"prompt_tokens_details":{"cached_tokens":3},'
                '"cost":0.125,"cost_currency":' + currency + "}"
            )
        )

    assert caught.value.http_status == 200
    assert caught.value.retryable is False
    assert caught.value.paid_response.raw["usage"]["cost"] == 0.125
    assert caught.value.paid_response.content == "{}"
    assert caught.value.paid_response.prompt_tokens == 11
    assert caught.value.paid_response.completion_tokens == 7
    assert caught.value.paid_response.cached_tokens == 3


@pytest.mark.parametrize("cost", ['"0.125"', "-0.125", "NaN"])
def test_invalid_provider_cost_is_a_classified_error(cost: str) -> None:
    with pytest.raises(TransportPaidAnswerError, match="invalid usage.cost") as caught:
        _complete(_answer('{"cost":' + cost + ',"cost_currency":"USD"}'))

    assert caught.value.paid_response.raw["usage"]["cost_currency"] == "USD"
    assert caught.value.paid_response.cost is None


@pytest.mark.parametrize("bad_count", ["-1", "true", "1.5", '"1.5"', '"unknown"', "null"])
def test_invalid_paid_prompt_count_preserves_other_counts(bad_count: str) -> None:
    with pytest.raises(TransportPaidAnswerError, match="invalid usage token counts") as caught:
        _complete(
            _answer(
                '{"prompt_tokens":'
                + bad_count
                + ',"completion_tokens":7,"prompt_tokens_details":{"cached_tokens":3},'
                '"cost":0.125,"cost_currency":"USD"}'
            )
        )

    assert caught.value.http_status == 200
    assert not caught.value.retryable
    assert caught.value.paid_response.prompt_tokens is None
    assert caught.value.paid_response.completion_tokens == 7
    assert caught.value.paid_response.cached_tokens == 3
    assert caught.value.paid_response.raw["usage"]["prompt_tokens"] == json.loads(bad_count)


@pytest.mark.parametrize("field", ["completion_tokens", "cached_tokens"])
def test_invalid_paid_other_count_preserves_valid_prompt_count(field: str) -> None:
    usage: dict[str, Any] = {
        "prompt_tokens": 11,
        "completion_tokens": 7,
        "prompt_tokens_details": {"cached_tokens": 3},
        "cost": 0.125,
        "cost_currency": "USD",
    }
    if field == "cached_tokens":
        usage["prompt_tokens_details"]["cached_tokens"] = False
    else:
        usage[field] = False

    with pytest.raises(TransportPaidAnswerError, match="invalid usage token counts") as caught:
        _complete(_answer(json.dumps(usage)))

    assert caught.value.paid_response.prompt_tokens == 11
    assert (
        caught.value.paid_response.cached_tokens is None
        if field == "cached_tokens"
        else caught.value.paid_response.completion_tokens is None
    )


def test_missing_prompt_count_in_partial_usage_is_unknown() -> None:
    response = _complete(_answer('{"completion_tokens":7,"cost":0.125,"cost_currency":"USD"}'))

    assert response.prompt_tokens is None
    assert response.completion_tokens == 7
    assert response.cost == Decimal("0.125")


@pytest.mark.parametrize("details", ["null", "{}"])
def test_optional_null_or_empty_prompt_token_details_keeps_paid_usd_answer(
    details: str,
) -> None:
    response = _complete(
        _answer(
            '{"prompt_tokens":11,"completion_tokens":7,"prompt_tokens_details":'
            + details
            + ',"cost":0.125,"cost_currency":"USD"}'
        )
    )

    assert response.prompt_tokens == 11
    assert response.completion_tokens == 7
    assert response.cached_tokens is None
    assert response.cost == Decimal("0.125")
    assert response.cost_currency == "USD"


def test_explicit_eur_rate_is_a_decimal_without_default() -> None:
    assert LlmSettings.from_env(_ENV).eur_to_usd_rate is None
    assert LlmSettings.from_env({**_ENV, "LLM_EUR_TO_USD_RATE": "1.1204"}).eur_to_usd_rate == (
        Decimal("1.1204")
    )


@pytest.mark.parametrize("rate", ["0", "-1.2", "NaN", "Infinity", "-Infinity", "invalid"])
def test_invalid_eur_rate_is_rejected(rate: str) -> None:
    with pytest.raises(LlmConfigError, match="LLM_EUR_TO_USD_RATE"):
        LlmSettings.from_env({**_ENV, "LLM_EUR_TO_USD_RATE": rate})
