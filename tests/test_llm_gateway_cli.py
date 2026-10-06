"""The live-run CLI and composition of the LLM gateway (README "LLM gateway", #33)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bootstrap.llm_gateway import CASE_DEADLINE, build_gateway, main
from app.modules.reviews.infrastructure.llm import answers
from app.modules.reviews.infrastructure.llm.gateway import LlmGateway
from app.modules.reviews.infrastructure.llm.settings import LlmSettings
from app.modules.reviews.infrastructure.llm.transport import OpenAICompatibleTransport

REPO_ROOT = Path(__file__).resolve().parents[1]
SAMPLE_DIFF = str(REPO_ROOT / "review" / "examples" / "sample.diff")
SAMPLE_OUTPUT = (REPO_ROOT / "review" / "examples" / "findings.sample.json").read_text()
SELF_HOSTED = {
    "LLM_BASE_URL": "http://localhost:11434/v1",
    "LLM_MODEL": "qwen3-1.7b-16k",
    "LLM_CONTEXT_WINDOW": "16384",
    "LLM_MAX_OUTPUT_TOKENS": "4000",
}


def _transport(handler: Any, requests: list[httpx.Request]) -> OpenAICompatibleTransport:
    def record(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        response: httpx.Response = handler(request)
        return response

    return OpenAICompatibleTransport(httpx.AsyncClient(transport=httpx.MockTransport(record)))


def test_cli_prints_the_run_summary_for_a_self_hosted_strict_model(
    capsys: pytest.CaptureFixture[str],
) -> None:
    requests: list[httpx.Request] = []
    answer = {
        "model": "qwen3-1.7b-16k",
        "choices": [{"message": {"content": SAMPLE_OUTPUT}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 7511, "completion_tokens": 379},
    }

    code = main(
        [SAMPLE_DIFF],
        env=SELF_HOSTED,
        transport=_transport(lambda _: httpx.Response(200, json=answer), requests),
    )

    out = json.loads(capsys.readouterr().out)
    assert code == 0
    assert out["provider"] == "self-hosted"
    assert out["model"] == "qwen3-1.7b-16k"
    assert out["calls"] == [
        {
            "kind": "primary",
            "model": "qwen3-1.7b-16k",
            "call_no": 1,
            "duration_ms": out["calls"][0]["duration_ms"],
            "response_model": "qwen3-1.7b-16k",
        }
    ]
    assert (out["tokens_in"], out["tokens_out"], out["cost_usd"]) == (7511, 379, "0.000000")
    assert out["findings"] == len(json.loads(SAMPLE_OUTPUT)["findings"])
    body = json.loads(requests[0].content)
    assert str(requests[0].url) == "http://localhost:11434/v1/chat/completions"
    assert body["response_format"]["json_schema"]["strict"] is True


def test_cli_reports_a_gateway_failure_with_every_call(
    capsys: pytest.CaptureFixture[str],
) -> None:
    requests: list[httpx.Request] = []
    rejected = httpx.Response(400, json={"error": {"message": "response_format is not supported"}})

    code = main(
        [SAMPLE_DIFF],
        env={**SELF_HOSTED, "LLM_FALLBACK_MODEL": "other", "LLM_FALLBACK_CONTEXT_WINDOW": "16384"},
        transport=_transport(lambda _: rejected, requests),
    )

    captured = capsys.readouterr()
    out = json.loads(captured.out)
    assert code == 1
    assert out["error_code"] == "llm_unavailable"
    assert [(call["kind"], call["error"]["http_status"]) for call in out["calls"]] == [
        ("primary", 400),
        ("fallback", 400),
    ]
    assert "response_format is not supported" in out["calls"][0]["error"]["message"]
    assert captured.err == "gateway failed: llm_unavailable\n"
    assert len(requests) == 2


def test_cli_uses_ecb_quote_and_ignores_invalid_legacy_rate(
    capsys: pytest.CaptureFixture[str],
) -> None:
    llm_requests: list[httpx.Request] = []
    fx_requests: list[httpx.Request] = []
    answer = {
        "model": "mistral-small-4",
        "choices": [{"message": {"content": SAMPLE_OUTPUT}, "finish_reason": "stop"}],
        "usage": {
            "prompt_tokens": 7511,
            "completion_tokens": 379,
            "cost": 0.125,
            "cost_currency": "EUR",
        },
    }

    def respond_fx(request: httpx.Request) -> httpx.Response:
        fx_requests.append(request)
        return httpx.Response(
            200,
            text=(
                "KEY,FREQ,CURRENCY,CURRENCY_DENOM,EXR_TYPE,EXR_SUFFIX,TIME_PERIOD,OBS_VALUE\n"
                f"EXR.D.USD.EUR.SP00.A,D,USD,EUR,SP00,A,{datetime.now(UTC).date()},1.20\n"
            ),
        )

    code = main(
        [SAMPLE_DIFF],
        env={
            "LLM_MODEL": "mistral-small-4",
            "LLM_API_KEYS": "sk-test",
            "LLM_EUR_TO_USD_RATE": "invalid-legacy-value",
        },
        transport=_transport(lambda _: httpx.Response(200, json=answer), llm_requests),
        fx_transport=httpx.MockTransport(respond_fx),
    )

    out = json.loads(capsys.readouterr().out)
    assert code == 0
    assert out["cost_usd"] == "0.150000"
    assert out["calls"][0]["fx"] == {
        "source": "EXR.D.USD.EUR.SP00.A",
        "observation_date": datetime.now(UTC).date().isoformat(),
        "rate_usd_per_eur": "1.20",
        "stale_cache": False,
    }
    assert len(fx_requests) == len(llm_requests) == 1
    assert fx_requests[0].url.host == "data-api.ecb.europa.eu"
    assert "authorization" not in fx_requests[0].headers


def test_cli_cold_cache_ecb_outage_fails_before_llm_without_config_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    llm_requests: list[httpx.Request] = []
    fx_requests: list[httpx.Request] = []

    def unavailable_fx(request: httpx.Request) -> httpx.Response:
        fx_requests.append(request)
        return httpx.Response(503)

    code = main(
        [SAMPLE_DIFF],
        env={"LLM_MODEL": "mistral-small-4", "LLM_API_KEYS": "sk-test"},
        transport=_transport(lambda _: httpx.Response(200), llm_requests),
        fx_transport=httpx.MockTransport(unavailable_fx),
    )

    out = json.loads(capsys.readouterr().out)
    assert code == 1
    assert out["error_code"] == "llm_unavailable"
    assert out["calls"] == []
    assert out["cost_usd"] == "0"
    assert len(fx_requests) == 1
    assert llm_requests == []


def test_cli_paid_answer_failure_reports_quote_used_for_conservative_charge(
    capsys: pytest.CaptureFixture[str],
) -> None:
    answer = {
        "model": "mistral-small-4",
        "choices": [{"message": {"content": SAMPLE_OUTPUT}, "finish_reason": "stop"}],
        "usage": {
            "prompt_tokens": 7511,
            "completion_tokens": 379,
            "cost": 0.125,
            "cost_currency": "GBP",
        },
    }
    observation_date = datetime.now(UTC).date()

    code = main(
        [SAMPLE_DIFF],
        env={"LLM_MODEL": "mistral-small-4", "LLM_API_KEYS": "sk-test"},
        transport=_transport(lambda _: httpx.Response(200, json=answer), []),
        fx_transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                text=(
                    "KEY,FREQ,CURRENCY,CURRENCY_DENOM,EXR_TYPE,EXR_SUFFIX,TIME_PERIOD,OBS_VALUE\n"
                    f"EXR.D.USD.EUR.SP00.A,D,USD,EUR,SP00,A,{observation_date},1.20\n"
                ),
            )
        ),
    )

    out = json.loads(capsys.readouterr().out)
    assert code == 1
    assert out["error_code"] == "llm_invalid_output"
    assert len(out["calls"]) == 1
    assert out["calls"][0]["fx"] == {
        "source": "EXR.D.USD.EUR.SP00.A",
        "observation_date": observation_date.isoformat(),
        "rate_usd_per_eur": "1.20",
        "stale_cache": False,
    }
    assert out["cost_usd"] != "0"


def test_cli_reports_a_configuration_error_without_a_traceback(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = main([SAMPLE_DIFF], env={})

    captured = capsys.readouterr()
    assert code == 2
    assert json.loads(captured.out) == {
        "error_code": "config_error",
        "message": "LLM_MODEL must be set",
    }
    assert captured.err == "configuration error: LLM_MODEL must be set\n"


def test_build_gateway_composes_the_production_ports() -> None:
    settings = LlmSettings.from_env(SELF_HOSTED)
    factory = cast(async_sessionmaker[AsyncSession], object())

    gateway = build_gateway(settings, httpx.AsyncClient(), factory)

    assert isinstance(gateway, LlmGateway)
    assert gateway.settings is settings


def test_case_deadlines_are_the_attempt_deadlines_of_the_spec() -> None:
    # docs/PIPELINE_SPEC.md §3: fast 8 min from claim, deep (SandboxEngine) 10 min
    assert {"fast": timedelta(minutes=8), "deep": timedelta(minutes=10)} == CASE_DEADLINE


def test_build_gateway_fails_at_start_when_the_schema_file_is_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(answers, "REVIEW_OUTPUT_SCHEMA_PATH", tmp_path / "missing.json")
    answers.review_output_schema.cache_clear()
    try:
        with pytest.raises(FileNotFoundError):
            build_gateway(
                LlmSettings.from_env(SELF_HOSTED),
                httpx.AsyncClient(),
                cast(async_sessionmaker[AsyncSession], object()),
            )
    finally:
        answers.review_output_schema.cache_clear()
