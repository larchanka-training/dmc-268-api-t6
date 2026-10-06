"""Record first raw gateway answers for a validated gold corpus."""

from __future__ import annotations

import asyncio
import json
import shutil
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

import review.scripts.eval_live as eval_live_module
from app.modules.reviews.infrastructure.llm.settings import (
    GatewayPolicy,
    LlmSettings,
    ModelPrice,
    ModelProfile,
)
from app.modules.reviews.infrastructure.llm.transport import (
    ChatRequest,
    ChatResponse,
    TransportError,
    TransportPaidAnswerError,
    TransportPaymentRequired,
    TransportRateLimited,
    TransportTimeout,
    TransportUnavailable,
    extract_text_content,
)
from review.scripts.eval_live import RecorderError, main, record_live
from review.scripts.eval_replay import format_console, replay
from review.scripts.eval_replay import main as replay_main

REPO_ROOT = Path(__file__).resolve().parents[1]
DATASET_ROOT = REPO_ROOT / "test-prs-dataset"
SYSTEM = REPO_ROOT / "review/prompts/review.system.v2.md"
BACKEND_RULES = REPO_ROOT / "review/rules/default-backend.v1.json"
FRONTEND_RULES = REPO_ROOT / "review/rules/default-frontend.v1.json"
RECORDED_AT = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)
VALID = json.dumps(
    {
        "findings": [],
        "summary": {
            "problem": "No issue was found.",
            "done_well": "The change is clear.",
            "effort": "none",
        },
    }
)
INVALID = '{"findings":[]}'


def fixture_root(tmp_path: Path, case_ids: tuple[str, ...]) -> Path:
    root = tmp_path / "dataset"
    for case_id in case_ids:
        shutil.copytree(DATASET_ROOT / "cases" / case_id, root / "cases" / case_id)
    return root


def settings() -> LlmSettings:
    price = ModelPrice(Decimal(0), Decimal(0))
    primary = ModelProfile(
        "fake",
        "http://localhost:8888",
        "primary-model",
        100_000,
        1_000,
        price,
        api_keys=("SENSITIVE_TEST_KEY",),
    )
    fallback = ModelProfile(
        "fake",
        "http://localhost:8888",
        "fallback-model",
        100_000,
        1_000,
        price,
        api_keys=("SENSITIVE_TEST_KEY",),
    )
    return LlmSettings(
        primary,
        fallback,
        GatewayPolicy(
            timeout_retry_delays_s=(),
            unavailable_retry_delays_s=(),
            max_jitter_s=0,
        ),
    )


def answer(
    model: str, content: str | list[dict[str, str]] | None, *, provider: str | None = None
) -> ChatResponse:
    raw: dict[str, Any] = {
        "model": model,
        "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 30},
    }
    if provider is not None:
        raw["provider"] = provider
    text = (
        "".join(part.get("text", "") for part in content if part.get("type") == "text")
        if isinstance(content, list)
        else content
    )
    return ChatResponse(raw, text, "stop", model, 100, 30, 0, None, None)


@dataclass
class FakeTransport:
    outcomes: list[ChatResponse | TransportError]
    requests: list[ChatRequest] = field(default_factory=list)

    async def complete(self, request: ChatRequest) -> ChatResponse:
        self.requests.append(request)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, TransportError):
            raise outcome
        return outcome


def record(
    root: Path, transport: FakeTransport, *, model_settings: LlmSettings | None = None
) -> dict[str, Any]:
    return asyncio.run(
        record_live(
            root,
            model_settings or settings(),
            system_prompt=SYSTEM,
            backend_rules=BACKEND_RULES,
            frontend_rules=FRONTEND_RULES,
            transport=transport,
            recorded_at=RECORDED_AT,
        )
    )


def response(root: Path, case_id: str) -> bytes:
    return (root / "responses" / f"{case_id}.json").read_bytes()


def test_effective_settings_track_provider_and_endpoint_without_raw_url(tmp_path: Path) -> None:
    configured = settings()
    primary = replace(
        configured.primary,
        provider="eurouter",
        base_url="https://API.EXAMPLE.TEST:443/v1/",
    )

    def snapshot(name: str, profile: ModelProfile) -> tuple[dict[str, Any], bytes]:
        root = fixture_root(tmp_path / name, ("SEC-01",))
        manifest = record(
            root,
            FakeTransport([answer("primary-model", VALID)]),
            model_settings=LlmSettings(profile, None),
        )
        return manifest["run_metadata"]["effective_settings"]["primary"], (
            root / "responses/manifest.json"
        ).read_bytes()

    original, raw_original = snapshot("original", primary)
    changed_provider, raw_provider = snapshot(
        "provider", replace(primary, provider="other-provider")
    )
    changed_endpoint, raw_endpoint = snapshot(
        "endpoint", replace(primary, base_url="https://API.EXAMPLE.TEST/v2")
    )
    same_path, raw_same_path = snapshot(
        "same-path", replace(primary, base_url="https://api.example.test/v1/")
    )
    without_trailing_slash, raw_without_slash = snapshot(
        "without-slash", replace(primary, base_url="https://api.example.test/v1")
    )

    assert original["provider_label"] == "eurouter"
    assert original["endpoint_digest"] == same_path["endpoint_digest"]
    assert original["endpoint_digest"] != without_trailing_slash["endpoint_digest"]
    assert changed_provider["provider_label"] == "custom"
    assert changed_provider["provider_digest"] != original["provider_digest"]
    assert changed_provider["endpoint_digest"] == original["endpoint_digest"]
    assert changed_endpoint["provider_digest"] == original["provider_digest"]
    assert changed_endpoint["endpoint_digest"] != original["endpoint_digest"]
    assert changed_provider != original
    assert changed_endpoint != original
    for raw in (
        raw_original,
        raw_provider,
        raw_endpoint,
        raw_same_path,
        raw_without_slash,
    ):
        assert b"api.example.test" not in raw.lower()


def test_effective_settings_track_price_and_policy_changes(tmp_path: Path) -> None:
    configured = settings()

    def snapshot(name: str, selected: LlmSettings) -> dict[str, Any]:
        root = fixture_root(tmp_path / name, ("SEC-01",))
        manifest = record(
            root,
            FakeTransport([answer("primary-model", VALID)]),
            model_settings=selected,
        )
        effective: dict[str, Any] = manifest["run_metadata"]["effective_settings"]
        return effective

    baseline = snapshot("baseline", configured)
    changed_price = snapshot(
        "price",
        replace(
            configured,
            primary=replace(
                configured.primary,
                price=ModelPrice(Decimal("0.25"), Decimal("0.50"), Decimal("0.10")),
            ),
        ),
    )
    changed_cost_limit = snapshot(
        "cost",
        replace(
            configured,
            policy=replace(
                configured.policy,
                run_cost_limit_usd={"fast": Decimal("1.25"), "deep": Decimal("3")},
            ),
        ),
    )
    changed_timeout = snapshot(
        "timeout",
        replace(
            configured,
            policy=replace(
                configured.policy,
                call_timeout_s={"fast": 45.0, "deep": 300.0},
            ),
        ),
    )

    assert baseline["primary"]["price_usd_per_mtok"] == {
        "input": "0",
        "output": "0",
        "cache_read": "0",
    }
    assert changed_price["primary"]["price_usd_per_mtok"] == {
        "input": "0.25",
        "output": "0.5",
        "cache_read": "0.1",
    }
    assert changed_price["fallback"] == baseline["fallback"]
    assert changed_price != baseline
    assert baseline["policy"]["run_cost_limit_usd"] == "0.5"
    assert changed_cost_limit["policy"]["run_cost_limit_usd"] == "1.25"
    assert changed_cost_limit["primary"] == baseline["primary"]
    assert changed_cost_limit != baseline
    assert baseline["policy"]["call_timeout_s"] == 90.0
    assert changed_timeout["policy"]["call_timeout_s"] == 45.0
    assert changed_timeout != baseline


def test_records_sanitized_effective_settings_and_replay_preserves_them(tmp_path: Path) -> None:
    root = fixture_root(tmp_path, ("SEC-01",))
    configured = settings()
    primary = replace(
        configured.primary,
        context_window=70_000,
        max_output_tokens=1_500,
        chars_per_token=3.5,
        extra_body={"reasoning": {"effort": "low"}, "api_key": "SENSITIVE_TEST_KEY"},
    )
    assert configured.fallback is not None
    fallback = replace(
        configured.fallback,
        context_window=45_000,
        max_output_tokens=2_000,
        chars_per_token=4.0,
        structured_output="prompt_json",
    )
    transport = FakeTransport([answer("primary-model", VALID)])

    manifest = record(root, transport, model_settings=LlmSettings(primary, fallback))

    provider_digest = "sha256-v1:32e17e82435ed113d7e4b0459eee9c1b6e8b417271766c908059381eba14ef97"
    endpoint_digest = "sha256-v1:a5e53989d3ad9f238451f0ded1af4452f9b0d2d0520ec908e98541638e0bf870"
    zero_price = {"input": "0", "output": "0", "cache_read": "0"}
    expected = {
        "primary": {
            "model_id": "primary-model",
            "provider_label": "fake",
            "provider_digest": provider_digest,
            "endpoint_digest": endpoint_digest,
            "context_window": 70_000,
            "max_output_tokens": 1_500,
            "input_budget_tokens": 58_500,
            "structured_output": "json_schema",
            "chars_per_token": 3.5,
            "price_usd_per_mtok": zero_price,
            "extra_body_digest": (
                "sha256-v1:81ac586604a9fbd5b586502b28b65b2342c288489b8d42bdf9ca8b74c2825ac6"
            ),
        },
        "fallback": {
            "model_id": "fallback-model",
            "provider_label": "fake",
            "provider_digest": provider_digest,
            "endpoint_digest": endpoint_digest,
            "context_window": 45_000,
            "max_output_tokens": 2_000,
            "input_budget_tokens": 43_000,
            "structured_output": "prompt_json",
            "chars_per_token": 4.0,
            "price_usd_per_mtok": zero_price,
            "extra_body_digest": (
                "sha256-v1:d751c4e009ff68eaf68d721b6caf8ab262b35962f15e93840b0cc5e8933856e8"
            ),
        },
        "policy": {
            "input_token_limit": 60_000,
            "call_timeout_s": 90.0,
            "run_cost_limit_usd": "0.5",
            "max_calls_per_attempt": 4,
            "timeout_retry_delays_s": [2.0],
            "unavailable_retry_delays_s": [2.0, 8.0],
            "max_jitter_s": 1.0,
            "max_retry_after_s": 30.0,
            "rate_limit_default_delay_s": 2.0,
        },
    }
    assert manifest["run_metadata"]["effective_settings"] == expected
    assert replay(root)["provenance"]["run_metadata"]["effective_settings"] == expected
    assert b"SENSITIVE_TEST_KEY" not in (root / "responses/manifest.json").read_bytes()
    assert len(transport.requests) == 1


def test_manifest_records_actual_first_serving_provider(tmp_path: Path) -> None:
    root = fixture_root(tmp_path, ("SEC-01",))

    manifest = record(root, FakeTransport([answer("primary-model", VALID, provider="OVHcloud")]))
    status = manifest["run_metadata"]["cases"]["SEC-01"]

    assert status["first_model"] == "primary-model"
    assert status["first_provider_label"] == "ovhcloud"
    assert status["first_provider_digest"].startswith("sha256-v1:")
    assert len(status["first_provider_digest"]) == len("sha256-v1:") + 64
    assert replay(root)["provenance"]["run_metadata"]["cases"]["SEC-01"] == status


def test_manifest_provider_follows_first_invalid_answer_not_later_repair(tmp_path: Path) -> None:
    root = fixture_root(tmp_path, ("SEC-01",))
    transport = FakeTransport(
        [
            answer("primary-model", INVALID, provider="Mistral AI"),
            answer("primary-model", VALID, provider="Scaleway"),
        ]
    )

    manifest = record(root, transport)
    status = manifest["run_metadata"]["cases"]["SEC-01"]

    assert status["gateway_status"] == "accepted"
    assert status["first_provider_label"] == "mistral ai"
    assert len(transport.requests) == 2


def test_manifest_omits_provider_when_first_call_has_no_response(tmp_path: Path) -> None:
    root = fixture_root(tmp_path, ("SEC-01",))
    transport = FakeTransport(
        [
            TransportUnavailable("primary down", retryable=False),
            answer("fallback-model", VALID, provider="Scaleway"),
        ]
    )

    manifest = record(root, transport)
    status = manifest["run_metadata"]["cases"]["SEC-01"]

    assert status["first_call"] == "no_content"
    assert status["first_provider_label"] is None
    assert status["first_provider_digest"] is None
    assert len(transport.requests) == 2


def test_manifest_keeps_provider_from_failed_paid_first_response(tmp_path: Path) -> None:
    root = fixture_root(tmp_path, ("SEC-01",))
    configured = settings()
    without_fallback = LlmSettings(configured.primary, None, configured.policy)
    transport = FakeTransport(
        [
            TransportPaidAnswerError(
                "invalid cost metadata", answer("primary-model", INVALID, provider="Scaleway")
            )
        ]
    )

    manifest = record(root, transport, model_settings=without_fallback)
    status = manifest["run_metadata"]["cases"]["SEC-01"]

    assert response(root, "SEC-01") == INVALID.encode()
    assert status["gateway_status"] == "llm_invalid_output"
    assert status["paid_metadata_error"] is True
    assert manifest["run_metadata"]["baseline_publishable"] is False
    assert status["first_provider_label"] == "scaleway"
    assert len(transport.requests) == 1


def test_paid_metadata_failure_with_valid_raw_answer_is_nonpublishable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = fixture_root(tmp_path, ("SEC-01",))
    configured = settings()
    without_fallback = LlmSettings(configured.primary, None, configured.policy)
    transport = FakeTransport(
        [TransportPaidAnswerError("SENSITIVE_ACCOUNTING_DETAIL", answer("primary-model", VALID))]
    )

    exit_code = main(["--root", str(root)], transport=transport, model_settings=without_fallback)

    assert exit_code == 1
    assert len(transport.requests) == 1
    assert response(root, "SEC-01") == VALID.encode()
    manifest = json.loads((root / "responses/manifest.json").read_text())
    status = manifest["run_metadata"]["cases"]["SEC-01"]
    assert status["first_call"] == "answer"
    assert status["gateway_status"] == "llm_invalid_output"
    assert status["paid_metadata_error"] is True
    assert manifest["run_metadata"]["baseline_publishable"] is False
    assert manifest["run_metadata"]["nonpublishable_case_ids"] == ["SEC-01"]
    assert "SENSITIVE_ACCOUNTING_DETAIL" not in (root / "responses/manifest.json").read_text()
    report = replay(root)
    assert (report["case_count"], report["valid_response_count"]) == (1, 1)
    assert "not publishable" in report["warnings"][0]
    assert replay_main(["--root", str(root)]) == 1
    assert "not publishable" in capsys.readouterr().err


def test_terminal_model_invalid_output_remains_measurable(tmp_path: Path) -> None:
    root = fixture_root(tmp_path, ("SEC-01",))
    configured = settings()
    one_call = replace(configured.policy, max_calls_per_attempt=1)
    without_fallback = LlmSettings(configured.primary, None, one_call)

    manifest = record(
        root, FakeTransport([answer("primary-model", INVALID)]), model_settings=without_fallback
    )

    status = manifest["run_metadata"]["cases"]["SEC-01"]
    assert status["gateway_status"] == "llm_invalid_output"
    assert status["paid_metadata_error"] is False
    assert manifest["run_metadata"]["baseline_publishable"] is True
    assert replay(root)["validity"] == 0.0


def test_manifest_redacts_untrusted_serving_provider(tmp_path: Path) -> None:
    root = fixture_root(tmp_path, ("SEC-01",))
    malicious = "https://api.example.test/?key=SENSITIVE_TEST_KEY"

    manifest = record(root, FakeTransport([answer("primary-model", VALID, provider=malicious)]))
    status = manifest["run_metadata"]["cases"]["SEC-01"]
    raw_manifest = (root / "responses/manifest.json").read_text(encoding="utf-8")

    assert status["first_provider_label"] == "custom"
    assert status["first_provider_digest"].startswith("sha256-v1:")
    assert malicious not in raw_manifest
    assert "SENSITIVE_TEST_KEY" not in raw_manifest
    assert "api.example.test" not in raw_manifest


def test_records_only_first_valid_answer_with_explicit_inputs(tmp_path: Path) -> None:
    root = fixture_root(tmp_path, ("SEC-01",))
    parts = [
        {"type": "text", "text": VALID[:20]},
        {"type": "image", "text": "UNTRUSTED_IMAGE_TEXT"},
        {"type": "text", "text": VALID[20:]},
    ]
    transport = FakeTransport([answer("primary-model", parts)])

    manifest = record(root, transport)

    assert response(root, "SEC-01") == VALID.encode()
    assert manifest["responses"] == {"SEC-01": "responses/SEC-01.json"}
    assert manifest["model_id"] == "primary-model"
    assert manifest["prompt_path"] == "review/prompts/review.system.v2.md"
    assert manifest["prompt_version"] == "v2"
    assert manifest["static_inputs"] == sorted(manifest["static_inputs"])
    assert "review/prompts/review.system.v2.md" in manifest["static_inputs"]
    assert "review/rules/default-backend.v1.json" in manifest["static_inputs"]
    assert "review/rules/default-frontend.v1.json" in manifest["static_inputs"]
    assert "review/rules/schema.json" in manifest["static_inputs"]
    assert "review/schemas/review-output.schema.json" in manifest["static_inputs"]
    assert "app/common/application/languages.py" in manifest["static_inputs"]
    assert "app/modules/reviews/application/prompt_builder.py" in manifest["static_inputs"]
    assert "app/modules/reviews/application/prompt_budget.py" in manifest["static_inputs"]
    assert "app/modules/reviews/infrastructure/llm/gateway.py" in manifest["static_inputs"]
    assert "app/modules/reviews/infrastructure/llm/settings.py" in manifest["static_inputs"]
    assert "app/modules/reviews/infrastructure/llm/transport.py" in manifest["static_inputs"]
    assert "review/scripts/eval_live.py" in manifest["static_inputs"]
    assert manifest["static_digest"].startswith("sha256-v1:")
    assert manifest["corpus_digest"].startswith("sha256-v1:")
    assert manifest["run_metadata"]["recorded_at"] == "2026-10-04T12:00:00Z"
    assert manifest["run_metadata"]["cases"]["SEC-01"] == {
        "first_call": "answer",
        "first_model": "primary-model",
        "first_kind": "primary",
        "first_provider_label": None,
        "first_provider_digest": None,
        "gateway_status": "accepted",
        "paid_metadata_error": False,
    }
    assert len(transport.requests) == 1
    request = transport.requests[0]
    assert request.profile.model == "primary-model"
    assert request.response_schema is not None
    assert request.response_schema.name == "ReviewOutput"
    assert request.messages[0].content == SYSTEM.read_text()
    assert 'name="Naming Consistency"' in request.messages[1].content
    assert "<files_changed>1</files_changed>" in request.messages[1].content
    assert "<lines_added>" in request.messages[1].content
    assert "<head_sha>" not in request.messages[1].content
    assert "<commit_messages>" not in request.messages[1].content
    assert "SENSITIVE_TEST_KEY" not in (root / "responses/manifest.json").read_text()
    assert "UNTRUSTED_IMAGE_TEXT" not in (root / "responses/manifest.json").read_text()
    assert replay(root)["validity"] == 1.0
    assert replay(root)["warnings"] == []


def test_invalid_first_answer_is_saved_even_when_repair_succeeds(tmp_path: Path) -> None:
    root = fixture_root(tmp_path, ("SEC-01",))
    transport = FakeTransport([answer("primary-model", INVALID), answer("primary-model", VALID)])

    manifest = record(root, transport)

    assert response(root, "SEC-01") == INVALID.encode()
    assert len(transport.requests) == 2
    assert [item.profile.model for item in transport.requests] == ["primary-model"] * 2
    assert manifest["run_metadata"]["cases"]["SEC-01"]["gateway_status"] == "accepted"
    assert replay(root)["validity"] == 0.0


def test_primary_failure_then_fallback_keeps_empty_first_response(tmp_path: Path) -> None:
    root = fixture_root(tmp_path, ("SEC-01",))
    transport = FakeTransport(
        [
            TransportUnavailable("SENSITIVE_TEST_KEY provider down", retryable=False),
            answer("fallback-model", VALID),
        ]
    )

    manifest = record(root, transport)

    assert response(root, "SEC-01") == b""
    assert [item.profile.model for item in transport.requests] == [
        "primary-model",
        "fallback-model",
    ]
    assert manifest["run_metadata"]["cases"]["SEC-01"]["first_call"] == "no_content"
    assert manifest["run_metadata"]["cases"]["SEC-01"]["gateway_status"] == "accepted"
    assert "SENSITIVE_TEST_KEY" not in (root / "responses/manifest.json").read_text()


@pytest.mark.parametrize(
    "first_failure",
    [
        TransportRateLimited("rate limited", http_status=429, retry_after_s=120),
        TransportTimeout("first call timed out", retryable=False),
    ],
)
def test_first_transport_failure_stays_invalid_after_successful_fallback(
    tmp_path: Path, first_failure: TransportError
) -> None:
    root = fixture_root(tmp_path, ("SEC-01",))
    transport = FakeTransport([first_failure, answer("fallback-model", VALID)])

    manifest = record(root, transport)
    report = replay(root)

    assert response(root, "SEC-01") == b""
    assert len(transport.requests) == 2
    assert manifest["run_metadata"]["cases"]["SEC-01"]["first_call"] == "no_content"
    assert manifest["run_metadata"]["cases"]["SEC-01"]["gateway_status"] == "accepted"
    assert manifest["run_metadata"]["baseline_publishable"] is True
    assert manifest["run_metadata"]["nonpublishable_case_ids"] == []
    assert (report["case_count"], report["valid_response_count"], report["validity"]) == (
        1,
        0,
        0.0,
    )
    assert report["warnings"] == []


def test_total_gateway_failure_keeps_case_in_denominator(tmp_path: Path) -> None:
    root = fixture_root(tmp_path, ("SEC-01",))
    transport = FakeTransport(
        [
            TransportUnavailable("SENSITIVE_TEST_KEY primary down", retryable=False),
            TransportUnavailable("SENSITIVE_TEST_KEY fallback down", retryable=False),
        ]
    )

    manifest = record(root, transport)

    assert response(root, "SEC-01") == b""
    assert len(transport.requests) == 2
    assert manifest["run_metadata"]["cases"]["SEC-01"]["gateway_status"] == ("llm_unavailable")
    assert manifest["run_metadata"]["baseline_publishable"] is False
    assert manifest["run_metadata"]["nonpublishable_case_ids"] == ["SEC-01"]
    assert set(manifest["responses"]) == {"SEC-01"}
    report = replay(root)
    assert (report["case_count"], report["valid_response_count"], report["validity"]) == (
        1,
        0,
        0.0,
    )
    assert report["responses"][0]["valid"] is False
    assert "SENSITIVE_TEST_KEY" not in (root / "responses/manifest.json").read_text()


@pytest.mark.parametrize(
    ("raw_content", "expected_text"),
    [
        ("{}", "{}"),
        (
            [
                {"type": "text", "text": "{"},
                {"type": "image_url", "text": "ignored"},
                "ignored",
                {"type": "text", "text": "}"},
                {"type": "text", "text": 7},
            ],
            "{}7",
        ),
        (None, None),
    ],
)
def test_first_recorded_content_uses_transport_text_semantics(
    tmp_path: Path, raw_content: object, expected_text: str | None
) -> None:
    root = fixture_root(tmp_path, ("SEC-01",))
    raw = {
        "model": "primary-model",
        "choices": [{"message": {"content": raw_content}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 30},
    }
    first = ChatResponse(raw, expected_text, "stop", "primary-model", 100, 30, 0, None, None)
    transport = FakeTransport([first, answer("primary-model", VALID)])

    assert extract_text_content(raw_content) == expected_text
    manifest = record(root, transport)

    assert response(root, "SEC-01") == (expected_text or "").encode()
    assert manifest["run_metadata"]["cases"]["SEC-01"]["gateway_status"] == "accepted"
    assert len(transport.requests) == 2


def test_no_content_first_answer_is_empty_even_after_valid_repair(tmp_path: Path) -> None:
    root = fixture_root(tmp_path, ("SEC-01",))
    transport = FakeTransport([answer("primary-model", None), answer("primary-model", VALID)])

    manifest = record(root, transport)

    assert response(root, "SEC-01") == b""
    assert len(transport.requests) == 2
    assert manifest["run_metadata"]["cases"]["SEC-01"]["first_call"] == "empty_answer"
    assert manifest["run_metadata"]["cases"]["SEC-01"]["gateway_status"] == "accepted"
    assert manifest["run_metadata"]["baseline_publishable"] is True
    assert replay(root)["validity"] == 0.0


def test_unexpected_second_case_failure_leaves_no_partial_capture_and_can_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = fixture_root(tmp_path, ("SEC-01", "CLEAN-01"))
    (root / "responses").mkdir()
    original_case_input = eval_live_module._case_input
    calls = 0

    def fail_once_on_second_case(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("unexpected second-case failure")
        return original_case_input(*args, **kwargs)

    monkeypatch.setattr(eval_live_module, "_case_input", fail_once_on_second_case)
    with pytest.raises(RuntimeError, match="unexpected second-case failure"):
        record(root, FakeTransport([answer("primary-model", VALID)]))

    assert list((root / "responses").iterdir()) == []
    assert list(root.glob(".responses-*")) == []

    manifest = record(
        root,
        FakeTransport([answer("primary-model", VALID), answer("primary-model", VALID)]),
    )
    assert set(manifest["responses"]) == {"SEC-01", "CLEAN-01"}
    assert len(list((root / "responses").glob("*.json"))) == 3
    assert list(root.glob(".responses-*")) == []


def test_newly_populated_baseline_is_not_overwritten_during_capture(tmp_path: Path) -> None:
    root = fixture_root(tmp_path, ("SEC-01",))

    class BaselineAppearsTransport(FakeTransport):
        async def complete(self, request: ChatRequest) -> ChatResponse:
            responses = root / "responses"
            responses.mkdir()
            (responses / "existing.json").write_bytes(b"preserve existing baseline")
            return await super().complete(request)

    with pytest.raises(RecorderError, match="responses directory is not empty"):
        record(root, BaselineAppearsTransport([answer("primary-model", VALID)]))

    assert (root / "responses/existing.json").read_bytes() == b"preserve existing baseline"
    assert list(root.glob(".responses-*")) == []


def test_records_exactly_the_twenty_four_active_case_ids(tmp_path: Path) -> None:
    case_ids = tuple(
        sorted(path.name for path in (DATASET_ROOT / "cases").iterdir() if path.is_dir())
    )
    assert len(case_ids) == 24
    root = fixture_root(tmp_path, case_ids)
    transport = FakeTransport([answer("primary-model", VALID) for _ in case_ids])

    manifest = record(root, transport)

    assert len(transport.requests) == 24
    assert set(manifest["responses"]) == set(case_ids)
    assert {path.name for path in (root / "responses").glob("*.json")} == {
        "manifest.json",
        *(f"{case_id}.json" for case_id in case_ids),
    }
    assert len(manifest["run_metadata"]["cases"]) == 24
    assert list(root.glob(".responses-*")) == []
    assert "review/rules/default-backend.v1.json" in manifest["static_inputs"]
    assert "review/rules/default-frontend.v1.json" in manifest["static_inputs"]
    assert 'name="Naming Consistency"' in transport.requests[0].messages[1].content
    assert any(
        'name="FSD Layer Boundaries"' in request.messages[1].content
        for request in transport.requests
    )


def test_python_typescript_and_tsx_cases_use_their_selected_rules(tmp_path: Path) -> None:
    case_ids = ("LOG-02", "RES-02", "SEC-01")
    root = fixture_root(tmp_path, case_ids)
    transport = FakeTransport([answer("primary-model", VALID) for _ in case_ids])

    manifest = record(root, transport)

    assert list(manifest["responses"]) == list(case_ids)
    assert len(transport.requests) == len(case_ids)
    for case_id, request in zip(case_ids, transport.requests, strict=True):
        user_prompt = request.messages[1].content
        if case_id == "SEC-01":
            assert 'name="Clean Architecture Boundaries"' in user_prompt
            assert 'name="FSD Layer Boundaries"' not in user_prompt
        else:
            assert 'name="FSD Layer Boundaries"' in user_prompt
            assert 'name="Clean Architecture Boundaries"' not in user_prompt
    assert "review/rules/default-backend.v1.json" in manifest["static_inputs"]
    assert "review/rules/default-frontend.v1.json" in manifest["static_inputs"]


def test_invalid_case_is_rejected_before_any_response_is_written(tmp_path: Path) -> None:
    root = fixture_root(tmp_path, ("SEC-01",))
    (root / "cases/SEC-01/diff.patch").write_text("not a patch\n")
    transport = FakeTransport([answer("primary-model", VALID)])

    with pytest.raises(RecorderError, match="invalid case SEC-01"):
        record(root, transport)

    assert not (root / "responses").exists()
    assert transport.requests == []


def test_existing_response_bytes_are_preserved_without_provider_calls(tmp_path: Path) -> None:
    root = fixture_root(tmp_path, ("SEC-01",))
    existing = root / "responses" / "SEC-01.json"
    existing.parent.mkdir()
    original = b"existing raw response\n\x00"
    existing.write_bytes(original)
    transport = FakeTransport([answer("primary-model", VALID)])

    with pytest.raises(RecorderError, match="responses directory is not empty"):
        record(root, transport)

    assert existing.read_bytes() == original
    assert sorted(path.name for path in existing.parent.iterdir()) == ["SEC-01.json"]
    assert transport.requests == []


def test_symlinked_responses_directory_never_writes_outside_dataset(tmp_path: Path) -> None:
    root = fixture_root(tmp_path, ("SEC-01",))
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "responses").symlink_to(outside, target_is_directory=True)
    transport = FakeTransport([answer("primary-model", VALID)])

    with pytest.raises(RecorderError, match="responses directory cannot be a symlink"):
        record(root, transport)

    assert transport.requests == []
    assert list(outside.iterdir()) == []


def test_mid_corpus_payment_failure_makes_live_cli_nonpublishable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    case_ids = tuple(
        sorted(path.name for path in (DATASET_ROOT / "cases").iterdir() if path.is_dir())
    )
    assert len(case_ids) == 24
    root = fixture_root(tmp_path, case_ids)
    failed_case_id = case_ids[12]
    outcomes: list[ChatResponse | TransportError] = []
    for case_id in case_ids:
        if case_id == failed_case_id:
            outcomes.extend(
                [
                    TransportPaymentRequired("payment required", http_status=402, retryable=False),
                    TransportPaymentRequired("payment required", http_status=402, retryable=False),
                ]
            )
        else:
            outcomes.append(answer("primary-model", VALID))
    transport = FakeTransport(outcomes)

    exit_code = main(["--root", str(root)], transport=transport, model_settings=settings())

    assert exit_code == 1
    assert len(transport.requests) == 25
    manifest = json.loads((root / "responses/manifest.json").read_text())
    assert set(manifest["responses"]) == set(case_ids)
    assert manifest["run_metadata"]["cases"][failed_case_id]["gateway_status"] == (
        "llm_payment_required"
    )
    assert manifest["run_metadata"]["baseline_publishable"] is False
    assert manifest["run_metadata"]["nonpublishable_case_ids"] == [failed_case_id]
    assert response(root, failed_case_id) == b""
    assert response(root, case_ids[13]) == VALID.encode()
    report = replay(root)
    assert (report["case_count"], report["valid_response_count"]) == (24, 23)
    assert "not publishable" in report["warnings"][0]
    assert replay_main(["--root", str(root)]) == 1
    captured = capsys.readouterr()
    assert "not publishable" in captured.err
    assert "not publishable" in captured.out


def test_pre_send_failure_still_writes_an_empty_response(tmp_path: Path) -> None:
    root = fixture_root(tmp_path, ("SEC-01",))
    configured = settings()
    tiny = LlmSettings(
        replace(configured.primary, context_window=1_001, max_output_tokens=1_000),
        None,
    )
    transport = FakeTransport([])

    manifest = record(root, transport, model_settings=tiny)

    assert response(root, "SEC-01") == b""
    assert transport.requests == []
    assert manifest["run_metadata"]["cases"]["SEC-01"] == {
        "first_call": "no_call",
        "first_model": None,
        "first_kind": None,
        "first_provider_label": None,
        "first_provider_digest": None,
        "gateway_status": "llm_context_overflow",
        "paid_metadata_error": False,
    }


def test_live_cli_prints_same_complete_report_as_offline_replay(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = fixture_root(tmp_path, ("SEC-01",))
    transport = FakeTransport([answer("primary-model", INVALID), answer("primary-model", VALID)])

    exit_code = main(
        ["--root", str(root)],
        transport=transport,
        model_settings=settings(),
    )

    assert exit_code == 0
    assert len(transport.requests) == 2
    live_output = capsys.readouterr().out
    assert live_output == format_console(replay(root)) + "\n"
    assert replay_main(["--root", str(root)]) == 0
    assert capsys.readouterr().out == live_output
    assert replay(root)["validity"] == 0.0


def test_live_cli_writes_redacted_json_report_with_fake_transport(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = fixture_root(tmp_path, ("SEC-01",))
    report_path = tmp_path / "report.json"
    redacted_dir = tmp_path / "redacted-responses"
    invalid = json.loads(VALID)
    invalid["summary"]["effort"] = "RAW_PROVIDER_MARKER_DO_NOT_UPLOAD"
    raw_first = json.dumps(invalid)
    transport = FakeTransport([answer("primary-model", raw_first), answer("primary-model", VALID)])

    exit_code = main(
        [
            "--root",
            str(root),
            "--report-json",
            str(report_path),
            "--redacted-responses",
            str(redacted_dir),
        ],
        transport=transport,
        model_settings=settings(),
    )

    assert exit_code == 0
    assert len(transport.requests) == 2
    assert response(root, "SEC-01") == raw_first.encode()
    uploaded_report = report_path.read_text()
    assert "RAW_PROVIDER_MARKER_DO_NOT_UPLOAD" not in uploaded_report
    assert "validator_output" not in uploaded_report
    assert json.loads(uploaded_report)["validity"] == 0.0
    exported = sorted(redacted_dir.glob("*.json"))
    assert [path.name for path in exported] == ["SEC-01.json"]
    redacted_bytes = exported[0].read_bytes()
    assert b"RAW_PROVIDER_MARKER_DO_NOT_UPLOAD" not in redacted_bytes
    metadata = json.loads(redacted_bytes)
    assert metadata["case_id"] == "SEC-01"
    assert metadata["raw_bytes"] == len(raw_first.encode())
    assert metadata["first_call"] == "answer"
    assert metadata["valid"] is False
    assert len(metadata["raw_sha256"]) == 64
    assert capsys.readouterr().out == format_console(replay(root)) + "\n"


@pytest.mark.parametrize("flag", ["--report-json", "--redacted-responses"])
@pytest.mark.parametrize(
    ("target", "error"),
    [
        ("missing_parent", "parent directory must already exist"),
        ("parent_traversal", "path must not contain parent traversal"),
        ("symlink_parent", "path must not contain a symlink"),
    ],
)
def test_export_parent_is_checked_before_provider_call(
    tmp_path: Path,
    flag: str,
    target: str,
    error: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = fixture_root(tmp_path, ("SEC-01",))
    name = "report.json" if flag == "--report-json" else "redacted"
    if target == "missing_parent":
        destination = tmp_path / "absent" / name
    elif target == "parent_traversal":
        nested = tmp_path / "nested"
        nested.mkdir()
        destination = nested / ".." / name
    else:
        outside = tmp_path / "outside"
        outside.mkdir()
        linked = tmp_path / "linked"
        linked.symlink_to(outside, target_is_directory=True)
        destination = linked / name
    transport = FakeTransport([answer("primary-model", VALID)])

    assert (
        main(
            ["--root", str(root), flag, str(destination)],
            transport=transport,
            model_settings=settings(),
        )
        == 1
    )

    assert error in capsys.readouterr().err
    assert transport.requests == []
    assert not (root / "responses").exists()
    assert not destination.exists()


@pytest.mark.parametrize("target", ["case", "existing", "symlink"])
def test_report_destination_is_rejected_before_provider_call(
    tmp_path: Path, target: str, capsys: pytest.CaptureFixture[str]
) -> None:
    root = fixture_root(tmp_path, ("SEC-01",))
    case_file = root / "cases/SEC-01/case.json"
    if target == "case":
        report_path = case_file
    elif target == "existing":
        report_path = tmp_path / "existing.json"
        report_path.write_bytes(b"existing report sentinel")
    else:
        sentinel = tmp_path / "sentinel.json"
        sentinel.write_bytes(b"symlink target sentinel")
        report_path = tmp_path / "report-link.json"
        report_path.symlink_to(sentinel)
    original = report_path.read_bytes()
    transport = FakeTransport([answer("primary-model", VALID)])

    assert (
        main(
            ["--root", str(root), "--report-json", str(report_path)],
            transport=transport,
            model_settings=settings(),
        )
        == 1
    )

    assert transport.requests == []
    assert report_path.read_bytes() == original
    assert not (root / "responses").exists()
    assert "eval live:" in capsys.readouterr().err


@pytest.mark.parametrize("target", ["dangling_symlink", "parent_traversal"])
def test_report_destination_guard_rejects_unresolved_paths_before_provider_call(
    tmp_path: Path, target: str
) -> None:
    root = fixture_root(tmp_path, ("SEC-01",))
    if target == "dangling_symlink":
        report_path = tmp_path / "dangling-report.json"
        report_path.symlink_to(tmp_path / "missing-target.json")
    else:
        nested = tmp_path / "nested"
        nested.mkdir()
        report_path = nested / ".." / "report.json"
    transport = FakeTransport([answer("primary-model", VALID)])

    assert (
        main(
            ["--root", str(root), "--report-json", str(report_path)],
            transport=transport,
            model_settings=settings(),
        )
        == 1
    )
    assert transport.requests == []
    assert not (root / "responses").exists()
    if target == "dangling_symlink":
        assert report_path.is_symlink()
        assert not (tmp_path / "missing-target.json").exists()
    else:
        assert not (tmp_path / "report.json").exists()


def test_report_and_export_same_path_are_rejected_before_provider_call(tmp_path: Path) -> None:
    root = fixture_root(tmp_path, ("SEC-01",))
    shared_path = tmp_path / "same-destination"
    transport = FakeTransport([answer("primary-model", VALID)])

    assert (
        main(
            [
                "--root",
                str(root),
                "--report-json",
                str(shared_path),
                "--redacted-responses",
                str(shared_path),
            ],
            transport=transport,
            model_settings=settings(),
        )
        == 1
    )
    assert transport.requests == []
    assert not shared_path.exists()
    assert not (root / "responses").exists()


@pytest.mark.parametrize("target", ["cases", "symlink"])
def test_redacted_export_destination_is_rejected_before_provider_call(
    tmp_path: Path, target: str
) -> None:
    root = fixture_root(tmp_path, ("SEC-01",))
    if target == "cases":
        export_dir = root / "cases/redacted-export"
    else:
        destination = tmp_path / "symlink-target"
        destination.mkdir()
        export_dir = tmp_path / "redacted-link"
        export_dir.symlink_to(destination, target_is_directory=True)
    transport = FakeTransport([answer("primary-model", VALID)])

    assert (
        main(
            ["--root", str(root), "--redacted-responses", str(export_dir)],
            transport=transport,
            model_settings=settings(),
        )
        == 1
    )

    assert transport.requests == []
    assert not (root / "responses").exists()
    if target == "cases":
        assert not export_dir.exists()
    else:
        assert list(destination.iterdir()) == []


def test_report_cannot_overlap_redacted_export_file_before_provider_call(tmp_path: Path) -> None:
    root = fixture_root(tmp_path, ("SEC-01",))
    export_dir = tmp_path / "redacted-export"
    report_path = export_dir / "SEC-01.json"
    transport = FakeTransport([answer("primary-model", VALID)])

    assert (
        main(
            [
                "--root",
                str(root),
                "--report-json",
                str(report_path),
                "--redacted-responses",
                str(export_dir),
            ],
            transport=transport,
            model_settings=settings(),
        )
        == 1
    )

    assert transport.requests == []
    assert not report_path.exists()
    assert not (root / "responses").exists()


@pytest.mark.parametrize(
    "protected",
    [SYSTEM, BACKEND_RULES, FRONTEND_RULES, REPO_ROOT / "app/common/application/languages.py"],
)
def test_report_cannot_overwrite_static_input_before_recording(
    tmp_path: Path, protected: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = fixture_root(tmp_path, ("SEC-01",))
    original = protected.read_bytes()

    async def unexpected_record(*args: object, **kwargs: object) -> dict[str, Any]:
        raise AssertionError("record_live must not run for a protected export path")

    monkeypatch.setattr(eval_live_module, "record_live", unexpected_record)
    assert (
        main(
            ["--root", str(root), "--report-json", str(protected)],
            model_settings=settings(),
        )
        == 1
    )
    assert protected.read_bytes() == original
    assert not (root / "responses").exists()
