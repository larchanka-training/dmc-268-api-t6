"""What a gateway call reports back: the accepted call's id and the provider's Retry-After (#130).

``GatewayResult.accepted_call_id`` names the manifest of the call whose answer was accepted
(R13, ``llm.verify``); ``LlmCallFailed.retry_after_s`` carries the provider's Retry-After
(the longest of the call's 429s, over every rotated key, when the final error is a rate
limit, else the last call's) for the verifier's retry delay and, through
``classify_failure``, for the run retry of an ``llm_rate_limited`` failure (#247). No
network: provider answers come from ``httpx.MockTransport``.
"""

from __future__ import annotations

import asyncio
import math
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

import httpx
import pytest

import app.modules.reviews.infrastructure.llm.gateway as gateway_module
from app.modules.reviews.application.llm import LlmCallFailed, LlmErrorCode, RunCallContext
from app.modules.reviews.application.policy_snapshot import (
    CallManifest,
    ManifestBlock,
    PolicySnapshot,
    SourceRole,
)
from app.modules.reviews.application.prompt_budget import verify_provenance_pre_send_guard
from app.modules.reviews.application.run_failures import RetryDelays, classify_failure
from app.modules.reviews.infrastructure.llm.answers import InvalidAnswer
from app.modules.reviews.infrastructure.llm.gateway import (
    GatewayResult,
    StructuredTask,
)
from app.modules.reviews.infrastructure.llm.settings import LlmSettings, ModelPrice, ModelProfile
from app.modules.reviews.infrastructure.llm.transport import (
    ChatMessage,
    ResponseSchema,
)
from tests.llm_fakes import FakeClock, ScriptedProvider, completion

NOW = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)
RUN_ID = UUID("00000000-0000-0000-0000-000000013301")
RUN = RunCallContext(
    run_id=RUN_ID,
    workspace_id=UUID("00000000-0000-0000-0000-000000013302"),
    attempt=1,
    engine="fast",
    deadline=NOW + timedelta(seconds=480),
)
PRIMARY = ModelProfile(
    provider="router",
    base_url="https://router.test/v1",
    model="primary-model",
    context_window=100_000,
    max_output_tokens=1_000,
    price=ModelPrice(Decimal("1"), Decimal("2")),
    api_keys=("sk-test-1",),
)
FALLBACK = replace(PRIMARY, model="fallback-model")
BASE_CALL_ID = UUID("00000000-0000-0000-0000-000000013303")
BASE_MANIFEST = CallManifest(
    call_id=BASE_CALL_ID,
    run_id=RUN_ID,
    stage="verification",
    recovery_kind="none",
    blocks=(
        ManifestBlock(
            block_id="file-1",
            source_role=SourceRole.DIFF,
            source_kind="source_code",
            path="app/service.py",
        ),
    ),
)


def _validate(answer: str) -> dict[str, object]:
    if answer == '{"ok": true}':
        return {"ok": True}
    raise InvalidAnswer(['the answer must be {"ok": true}'])


def valid() -> httpx.Response:
    return completion('{"ok": true}')


def invalid() -> httpx.Response:
    return completion("not json")


def error(status: int, **headers: str) -> httpx.Response:
    return httpx.Response(status, json={"error": {"message": "provider error"}}, headers=headers)


class _Provider(ScriptedProvider):
    def generate(
        self,
        *,
        base_manifest: CallManifest | None = BASE_MANIFEST,
        fallback: ModelProfile = FALLBACK,
    ) -> GatewayResult:
        async def exercise() -> GatewayResult:
            async with httpx.AsyncClient(transport=httpx.MockTransport(self.handle)) as client:
                gateway = self.gateway(
                    LlmSettings(primary=PRIMARY, fallback=fallback), client, FakeClock(NOW)
                )
                task = StructuredTask(
                    operation="verification",
                    messages=(ChatMessage("system", "SYS"), ChatMessage("user", "USER")),
                    schema=ResponseSchema("Answer", {"type": "object"}),
                    validate=_validate,
                    base_manifest=base_manifest,
                )
                return await gateway.generate(task, RUN)

        return asyncio.run(exercise())

    def failure(self, *, fallback: ModelProfile = FALLBACK) -> LlmCallFailed:
        with pytest.raises(LlmCallFailed) as caught:
            self.generate(fallback=fallback)
        return caught.value


@pytest.fixture
def manifests(monkeypatch: pytest.MonkeyPatch) -> list[CallManifest]:
    """Every repair and fallback manifest the gateway checks before sending."""
    seen: list[CallManifest] = []

    def spy(
        manifest: CallManifest,
        *,
        context: Any = None,
        snapshot: PolicySnapshot | None = None,
    ) -> None:
        seen.append(manifest)
        verify_provenance_pre_send_guard(manifest, context=context, snapshot=snapshot)

    monkeypatch.setattr(gateway_module, "verify_provenance_pre_send_guard", spy)
    return seen


# ---------- accepted call ----------


def test_an_accepted_primary_answer_names_the_base_manifest(
    manifests: list[CallManifest],
) -> None:
    provider = _Provider([valid()])

    result = provider.generate()

    assert result.accepted_call_id == BASE_CALL_ID
    assert manifests == []


def test_a_same_model_retry_keeps_the_base_call_id(manifests: list[CallManifest]) -> None:
    provider = _Provider([httpx.ReadTimeout("slow"), valid()])

    result = provider.generate()

    assert provider.kinds() == ["primary", "retry"]
    assert result.accepted_call_id == BASE_CALL_ID


def test_an_accepted_repair_names_the_repair_manifest(manifests: list[CallManifest]) -> None:
    provider = _Provider([invalid(), valid()])

    result = provider.generate()

    [repair] = manifests
    assert repair.recovery_kind == "repair"
    assert result.accepted_call_id == repair.call_id
    assert result.accepted_call_id != BASE_CALL_ID


def test_a_retry_after_the_repair_keeps_the_repair_call_id(
    manifests: list[CallManifest],
) -> None:
    provider = _Provider([invalid(), error(503), valid()])

    result = provider.generate()

    assert provider.kinds() == ["primary", "repair", "retry"]
    [repair] = manifests
    assert result.accepted_call_id == repair.call_id


def test_an_accepted_fallback_names_the_fallback_manifest(
    manifests: list[CallManifest],
) -> None:
    provider = _Provider([invalid(), invalid(), valid()])

    result = provider.generate()

    assert provider.kinds() == ["primary", "repair", "fallback"]
    assert [manifest.recovery_kind for manifest in manifests] == ["repair", "fallback"]
    assert result.accepted_call_id == manifests[1].call_id
    assert len({BASE_CALL_ID, manifests[0].call_id, manifests[1].call_id}) == 3


def test_a_fallback_after_payment_required_names_the_fallback_manifest(
    manifests: list[CallManifest],
) -> None:
    provider = _Provider([error(402), valid()])

    result = provider.generate()

    [fallback] = manifests
    assert fallback.recovery_kind == "fallback"
    assert result.accepted_call_id == fallback.call_id


def test_without_a_base_manifest_there_is_no_accepted_call_id() -> None:
    provider = _Provider([invalid(), valid()])

    result = provider.generate(base_manifest=None)

    assert provider.kinds() == ["primary", "repair"]
    assert result.accepted_call_id is None


# ---------- Retry-After ----------


def test_the_final_rate_limit_failure_carries_the_providers_retry_after() -> None:
    # 45 s is above the 30 s the gateway waits for, so the primary is not retried.
    provider = _Provider([error(429, **{"Retry-After": "45"}), error(429, **{"Retry-After": "45"})])

    failure = provider.failure()

    assert provider.kinds() == ["primary", "fallback"]
    assert failure.error_code is LlmErrorCode.RATE_LIMITED
    assert failure.retry_after_s == 45.0
    # The run retry reads the hint; 45 s is under the 2-minute floor, so it waits 2m.
    assert classify_failure(failure).retry_after == timedelta(seconds=45)
    assert RetryDelays().for_failure(1, classify_failure(failure)) == (
        "2m",
        timedelta(minutes=2),
    )


def test_a_final_error_other_than_a_rate_limit_keeps_only_its_own_calls_hint() -> None:
    provider = _Provider([error(429, **{"Retry-After": "45"}), error(503)])

    failure = provider.failure()

    assert failure.error_code is LlmErrorCode.UNAVAILABLE
    assert failure.retry_after_s is None


def test_a_fallback_too_small_for_the_prompt_keeps_the_primary_hint() -> None:
    provider = _Provider([error(429, **{"Retry-After": "45"})])

    failure = provider.failure(fallback=replace(FALLBACK, context_window=1_001))

    assert provider.kinds() == ["primary"]
    assert failure.error_code is LlmErrorCode.RATE_LIMITED
    assert failure.retry_after_s == 45.0


def test_a_failure_without_a_hint_has_no_retry_after() -> None:
    assert LlmCallFailed(LlmErrorCode.RATE_LIMITED, "rate limited").retry_after_s is None


@pytest.mark.parametrize(
    ("retry_after_s", "retry_after", "delay"),
    [
        pytest.param(900.0, timedelta(minutes=15), ("10m", timedelta(minutes=15)), id="15m"),
        pytest.param(300.0, timedelta(minutes=5), ("10m", timedelta(minutes=10)), id="5m"),
        pytest.param(None, None, ("2m", timedelta(minutes=2)), id="no-hint"),
        pytest.param(0.0, None, ("2m", timedelta(minutes=2)), id="zero"),
        pytest.param(-5.0, None, ("2m", timedelta(minutes=2)), id="negative"),
        pytest.param(math.nan, None, ("2m", timedelta(minutes=2)), id="nan"),
        pytest.param(120.0, timedelta(minutes=2), ("2m", timedelta(minutes=2)), id="120s"),
        pytest.param(121.0, timedelta(seconds=121), ("10m", timedelta(minutes=10)), id="121s"),
        # A huge header must not overflow timedelta: it is capped at one day first.
        pytest.param(1e20, timedelta(days=1), ("10m", timedelta(days=1)), id="huge"),
        pytest.param(math.inf, timedelta(days=1), ("10m", timedelta(days=1)), id="inf"),
    ],
)
def test_a_rate_limit_hint_sets_the_run_retry_delay(
    retry_after_s: float | None,
    retry_after: timedelta | None,
    delay: tuple[str, timedelta],
) -> None:
    # D3 (#247): the run never retries a rate-limited call before its Retry-After.
    failure = classify_failure(
        LlmCallFailed(LlmErrorCode.RATE_LIMITED, "rate limited", retry_after_s=retry_after_s)
    )

    assert (failure.error_code, failure.retry_after) == ("llm_rate_limited", retry_after)
    assert RetryDelays().for_failure(1, failure) == delay


@pytest.mark.parametrize(
    ("hints", "kinds"),
    [
        # Above the 30 s the gateway waits for: the primary goes straight to the fallback.
        pytest.param(["900", None], ["primary", "fallback"], id="fallback-without-header"),
        pytest.param(["900", "45"], ["primary", "fallback"], id="fallback-shorter"),
        pytest.param(["45", "900"], ["primary", "fallback"], id="fallback-longer"),
        # 5 s is waited in the gateway; the retry's 429 has the longer window.
        pytest.param(["5", "900", None], ["primary", "retry", "fallback"], id="retry-longer"),
    ],
)
def test_a_final_rate_limit_keeps_the_longest_retry_after_of_the_call(
    hints: list[str | None], kinds: list[str]
) -> None:
    # No 429 of the call may shorten another one's window (#247).
    provider = _Provider(
        [error(429, **({} if hint is None else {"Retry-After": hint})) for hint in hints]
    )

    failure = provider.failure()

    assert provider.kinds() == kinds
    assert failure.error_code is LlmErrorCode.RATE_LIMITED
    assert failure.retry_after_s == 900.0


def test_a_hint_on_another_code_is_not_a_rate_limit() -> None:
    # for_failure takes any retry_after for a rate limit, so only llm_rate_limited gets one.
    failure = classify_failure(
        LlmCallFailed(LlmErrorCode.UNAVAILABLE, "provider down", retry_after_s=900.0)
    )

    assert (failure.error_code, failure.retry_after) == ("llm_unavailable", None)
    assert RetryDelays().for_failure(1, failure) == ("30s", timedelta(seconds=30))
