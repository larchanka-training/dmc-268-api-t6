"""Offline contract tests for the live verifier evaluation path."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.modules.reviews.infrastructure.llm.settings import LlmSettings, ModelPrice, ModelProfile
from app.modules.reviews.infrastructure.llm.transport import OpenAICompatibleTransport
from review.scripts.eval_verifier import run_all, run_case, score_results
from review.scripts.validate_verifier_dataset import load_dataset
from tests.llm_fakes import completion

CORPUS = Path(__file__).resolve().parents[1] / "test-prs-dataset/verifier/v1/cases.json"


def _settings() -> LlmSettings:
    profile = ModelProfile(
        provider="router",
        base_url="https://router.test/v1",
        model="review-model",
        context_window=100_000,
        max_output_tokens=1_000,
        price=ModelPrice(Decimal("1"), Decimal("2")),
        api_keys=("sk-test",),
    )
    return LlmSettings(
        primary=profile,
        fallback=None,
        verifier=replace(profile, model="verifier-model"),
        verifier_fallback=None,
    )


def test_real_gateway_route_records_decision_usage_and_empty_case_skips_call() -> None:
    cases = {case["id"]: case for case in load_dataset(CORPUS)["cases"]}
    calls: list[dict[str, Any]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        body: dict[str, Any] = json.loads(request.content)
        calls.append(body)
        case = cases["VFY-01"]
        return completion(
            json.dumps(
                {
                    "decisions": [
                        {
                            "candidate_id": case["input"]["candidate"]["candidate_id"],
                            "status": "supported",
                            "reason": "The f-string formats user_id into SQL text.",
                            "evidence_refs": case["expected"]["evidence_refs"],
                            "requirement_refs": case["expected"]["requirement_refs"],
                        }
                    ]
                }
            )
        )

    async def exercise() -> tuple[dict[str, Any], dict[str, Any]]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            transport = OpenAICompatibleTransport(client)
            first = await run_case(cases["VFY-01"], _settings(), transport)
            empty = await run_case(cases["VFY-05"], _settings(), transport)
            return first, empty

    first, empty = asyncio.run(exercise())
    assert len(calls) == 1
    assert calls[0]["model"] == "verifier-model"
    assert calls[0]["response_format"]["json_schema"]["name"] == "VerificationOutput"
    assert first["status"] == "supported"
    assert first["decisions"][0]["status"] == "supported"
    assert first["usage"][0]["tokens_in"] == 10
    assert Decimal(first["cost_usd"]) > 0
    assert first["calls"] == 1
    assert first["invalid_calls"] == 0
    assert first["input_sha256"] and first["accepted_call_id"]
    assert len(first["call_manifests"]) == 1
    assert first["call_manifests"][0]["rendered_messages_sha256"]
    assert first["call_manifests"][0]["blocks"]
    assert first["route"]["primary_model"] == "verifier-model"
    assert first["route"]["fallback_model"] is None
    assert len(first["route"]["endpoint_sha256"]) == 64
    assert "sk-test" not in json.dumps(first["route"])
    assert empty["status"] == "no_candidates"
    assert empty["usage"] == []
    assert empty["cost_usd"] == "0"


def test_offline_score_counts_failures_and_cost_without_dropping_denominators() -> None:
    cases = load_dataset(CORPUS)["cases"]
    results = [
        {
            "case_id": case["id"],
            "status": "invalid",
            "cost_usd": "0.01",
            "calls": 1,
            "invalid_calls": 1,
        }
        for case in cases
    ]
    results[0] = {
        "case_id": "VFY-01",
        "status": "supported",
        "cost_usd": "0.02",
        "calls": 1,
        "invalid_calls": 0,
    }
    results[1] = {
        "case_id": "VFY-02",
        "status": "supported",
        "cost_usd": "0.03",
        "calls": 1,
        "invalid_calls": 0,
    }
    results[4] = {
        "case_id": "VFY-05",
        "status": "no_candidates",
        "cost_usd": "0",
        "calls": 0,
        "invalid_calls": 0,
    }

    score = score_results(cases, results)

    assert score["by_class"]["proven"] == {
        "correct": 1,
        "total": 4,
        "rate": 0.25,
        "failed_case_ids": ["VFY-07", "VFY-09", "VFY-11"],
    }
    assert score["false_supported"]["invented"] == {"count": 1, "total": 4, "rate": 0.25}
    assert score["false_supported"]["injected"] == {"count": 0, "total": 3, "rate": 0.0}
    assert score["false_rejected"] == {"count": 0, "total": 4, "rate": 0.0}
    assert score["invalid"] == {"count": 8, "total": 10, "rate": 0.8}
    assert score["cost_usd"] == "0.13"


def test_score_reports_injected_false_rejections_and_insufficient_distribution() -> None:
    cases = load_dataset(CORPUS)["cases"]
    observed = {
        "VFY-03": "failed",
        "VFY-07": "contradicted",
        "VFY-10": "insufficient_context",
    }
    results = [
        {
            "case_id": case["id"],
            "status": observed.get(case["id"], case["expected"]["status"] or "no_candidates"),
            "cost_usd": "0",
            "calls": 0,
            "invalid_calls": 0,
        }
        for case in cases
    ]

    score = score_results(cases, results)

    assert score["injected_false_rejected"] == {"count": 1, "total": 3, "rate": 1 / 3}
    assert score["by_class"]["proven"]["failed_case_ids"] == ["VFY-07"]
    assert score["by_class"]["invented"]["failed_case_ids"] == ["VFY-10"]
    assert score["by_class"]["inapplicable_rule"]["failed_case_ids"] == ["VFY-03"]
    assert score["insufficient_context"] == {
        "count": 2,
        "total": 10,
        "rate": 0.2,
        "by_class": {
            "proven": {"count": 0, "total": 4, "rate": 0.0},
            "invented": {"count": 1, "total": 4, "rate": 0.25},
            "inapplicable_rule": {"count": 0, "total": 1, "rate": 0.0},
            "insufficient_context": {"count": 1, "total": 1, "rate": 1.0},
        },
    }


def test_invalid_provider_answers_keep_paid_usage_and_safe_failure() -> None:
    case = load_dataset(CORPUS)["cases"][0]
    calls = 0

    def respond(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return completion("{}")

    async def exercise() -> dict[str, Any]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            return await run_case(case, _settings(), OpenAICompatibleTransport(client))

    result = asyncio.run(exercise())
    assert result["status"] == "invalid"
    assert result["calls"] == calls
    assert result["invalid_calls"] == calls
    assert len(result["usage"]) == calls
    assert len(result["call_manifests"]) == calls
    assert {item["recovery_kind"] for item in result["call_manifests"]} == {"none", "repair"}
    assert Decimal(result["cost_usd"]) > 0
    assert "sk-test" not in json.dumps(result)
    assert "{}" not in json.dumps(result)


def test_invalid_primary_then_valid_fallback_records_each_paid_call() -> None:
    case = load_dataset(CORPUS)["cases"][0]
    settings = _settings()
    assert settings.verifier is not None
    settings = replace(
        settings, verifier_fallback=replace(settings.verifier, model="fallback-model")
    )
    models: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        body: dict[str, Any] = json.loads(request.content)
        models.append(body["model"])
        if body["model"] != "fallback-model":
            return completion("{}")
        return completion(
            json.dumps(
                {
                    "decisions": [
                        {
                            "candidate_id": case["input"]["candidate"]["candidate_id"],
                            "status": "supported",
                            "reason": "Visible interpolation",
                            "evidence_refs": case["expected"]["evidence_refs"],
                            "requirement_refs": case["expected"]["requirement_refs"],
                        }
                    ]
                }
            )
        )

    async def exercise() -> dict[str, Any]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            return await run_case(case, settings, OpenAICompatibleTransport(client))

    result = asyncio.run(exercise())
    assert models == ["verifier-model", "verifier-model", "fallback-model"]
    assert result["status"] == "supported"
    assert result["invalid_calls"] == 2
    assert result["calls"] == 3
    assert len(result["usage"]) == 3
    assert {item["model"] for item in result["usage"]} == {"answering-model"}
    assert [item["recovery_kind"] for item in result["call_manifests"]] == [
        "none",
        "repair",
        "fallback",
    ]
    assert len({item["manifest_id"] for item in result["call_manifests"]}) == 3
    assert result["accepted_call_id"] == result["call_manifests"][-1]["call_id"]


def test_paid_metadata_error_counts_as_one_invalid_generation() -> None:
    case = load_dataset(CORPUS)["cases"][0]

    def respond(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "model": "answering-model",
                "choices": [{"message": {"role": "assistant", "content": "{}"}}],
                "usage": {"prompt_tokens": "bad", "completion_tokens": 5},
            },
        )

    async def exercise() -> dict[str, Any]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            return await run_case(case, _settings(), OpenAICompatibleTransport(client))

    result = asyncio.run(exercise())
    assert result["calls"] >= 1
    assert result["invalid_calls"] == result["calls"]
    assert all(call["paid_metadata_error"] for call in result["call_manifests"])


def test_route_fingerprint_includes_fallback_price_and_shape() -> None:
    case = load_dataset(CORPUS)["cases"][4]
    settings = _settings()
    assert settings.verifier is not None
    fallback = replace(
        settings.verifier, model="fallback", price=ModelPrice(Decimal("9"), Decimal("8"))
    )

    async def exercise() -> tuple[dict[str, Any], dict[str, Any]]:
        async with httpx.AsyncClient() as client:
            transport = OpenAICompatibleTransport(client)
            first = await run_case(case, settings, transport)
            second = await run_case(case, replace(settings, verifier_fallback=fallback), transport)
            return first, second

    first, second = asyncio.run(exercise())
    assert first["route"]["route_sha256"] != second["route"]["route_sha256"]
    assert second["route"]["fallback_input_per_mtok_usd"] == "9"


def test_run_all_persists_progress_and_preflights_output(tmp_path: Path) -> None:
    cases = load_dataset(CORPUS)["cases"][:2]
    output = tmp_path / "report.json"
    calls = 0

    def respond(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        case = cases[0]
        return completion(
            json.dumps(
                {
                    "decisions": [
                        {
                            "candidate_id": case["input"]["candidate"]["candidate_id"],
                            "status": "supported",
                            "reason": "Visible interpolation",
                            "evidence_refs": case["expected"]["evidence_refs"],
                            "requirement_refs": case["expected"]["requirement_refs"],
                        }
                    ]
                }
            )
        )

    async def exercise() -> dict[str, Any]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            transport = OpenAICompatibleTransport(client)
            report = await run_all(
                cases,
                _settings(),
                transport,
                output,
                stop_threshold_usd=Decimal("0.000001"),
                approved_overrun_usd=Decimal("0.01"),
                window_end=datetime.now(UTC) + timedelta(minutes=3),
                approval_ref="test approval",
            )
            with pytest.raises(FileExistsError):
                await run_all(
                    cases,
                    _settings(),
                    transport,
                    output,
                    stop_threshold_usd=Decimal("1"),
                    approved_overrun_usd=Decimal("0.01"),
                    window_end=datetime.now(UTC) + timedelta(minutes=3),
                    approval_ref="test approval",
                )
            return report

    report = asyncio.run(exercise())
    assert calls == 1
    assert report["stop_reason"] == "threshold_reached"
    assert report["completed_cases"] == 1
    assert report["stop_threshold_usd"] == "0.000001"
    assert report["approved_overrun_usd"] == "0.01"
    assert report["approved_overrun_exceeded"] is False
    assert json.loads(output.read_text())["completed_cases"] == 1


def test_zero_allowance_blocks_first_call_when_estimate_exceeds_remaining(
    tmp_path: Path,
) -> None:
    case = load_dataset(CORPUS)["cases"][0]
    calls = 0

    def respond(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return completion("{}")

    async def exercise() -> dict[str, Any]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            return await run_all(
                [case],
                _settings(),
                OpenAICompatibleTransport(client),
                tmp_path / "report.json",
                stop_threshold_usd=Decimal("0.000001"),
                approved_overrun_usd=Decimal("0"),
                window_end=datetime.now(UTC) + timedelta(minutes=3),
                approval_ref="test approval",
            )

    report = asyncio.run(exercise())
    assert calls == 0
    assert report["cases"][0]["error_code"] == "verification_budget_exceeded"
    assert report["cases"][0]["run_cost_limit_usd"] == "0.000001"
    assert report["stop_reason"] == "budget_preflight"
    assert report["approved_overrun_exceeded"] is False


@pytest.mark.parametrize(
    ("reported_tokens", "overrun", "stop_reason"),
    [
        (9_000, False, "complete"),
        (20_000, True, "approved_overrun_exceeded"),
    ],
)
def test_remaining_budget_blocks_repair_and_fallback_before_request(
    tmp_path: Path, reported_tokens: int, overrun: bool, stop_reason: str
) -> None:
    case = load_dataset(CORPUS)["cases"][0]
    settings = _settings()
    assert settings.verifier is not None
    settings = replace(
        settings, verifier_fallback=replace(settings.verifier, model="fallback-model")
    )
    calls = 0

    def respond(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            json={
                "model": "answering-model",
                "choices": [{"message": {"role": "assistant", "content": "{}"}}],
                "usage": {"prompt_tokens": reported_tokens, "completion_tokens": 5},
            },
        )

    async def exercise() -> dict[str, Any]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            return await run_all(
                [case],
                settings,
                OpenAICompatibleTransport(client),
                tmp_path / "report.json",
                stop_threshold_usd=Decimal("0.01"),
                approved_overrun_usd=Decimal("0"),
                window_end=datetime.now(UTC) + timedelta(minutes=3),
                approval_ref="test approval",
            )

    report = asyncio.run(exercise())
    assert calls == 1
    assert report["cases"][0]["calls"] == 1
    assert report["cases"][0]["status"] == "invalid"
    assert report["stop_reason"] == stop_reason
    assert report["approved_overrun_exceeded"] is overrun


def test_window_checkpoint_prevents_repair_request_after_expiry() -> None:
    case = load_dataset(CORPUS)["cases"][0]
    start = datetime.now(UTC)
    current = start
    calls = 0

    def now() -> datetime:
        return current

    def respond(_request: httpx.Request) -> httpx.Response:
        nonlocal current, calls
        calls += 1
        current = start + timedelta(minutes=3)
        return completion("{}")

    async def exercise() -> dict[str, Any]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            return await run_case(
                case,
                _settings(),
                OpenAICompatibleTransport(client),
                window_end=start + timedelta(minutes=2),
                now=now,
            )

    result = asyncio.run(exercise())
    assert calls == 1
    assert result["status"] == "failed"
    assert result["calls"] == 1
