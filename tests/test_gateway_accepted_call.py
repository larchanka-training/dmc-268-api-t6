"""What a gateway call reports back: the accepted call's id and the provider's Retry-After (#130).

``GatewayResult.accepted_call_id`` names the manifest of the call whose answer was accepted
(R13, ``llm.verify``); ``LlmCallFailed.retry_after_s`` carries the last provider hint for
the verifier's retry delay. ``classify_failure`` does not read it, so live review delays
stay as they are. No network: provider answers come from ``httpx.MockTransport``.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
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
    LlmGateway,
    StructuredTask,
)
from app.modules.reviews.infrastructure.llm.memory import InMemoryLlmCallTrace, InMemoryUsageLedger
from app.modules.reviews.infrastructure.llm.settings import LlmSettings, ModelPrice, ModelProfile
from app.modules.reviews.infrastructure.llm.transport import (
    ChatMessage,
    OpenAICompatibleTransport,
    ResponseSchema,
)
from tests.llm_fakes import context_checkpoint

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

type Reply = httpx.Response | Exception


def _validate(answer: str) -> dict[str, object]:
    if answer == '{"ok": true}':
        return {"ok": True}
    raise InvalidAnswer(['the answer must be {"ok": true}'])


def valid() -> httpx.Response:
    return _completion('{"ok": true}')


def invalid() -> httpx.Response:
    return _completion("not json")


def _completion(content: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "model": "answering-model",
            "choices": [{"message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        },
    )


def error(status: int, **headers: str) -> httpx.Response:
    return httpx.Response(status, json={"error": {"message": "provider error"}}, headers=headers)


class _Clock:
    def __init__(self) -> None:
        self.current = NOW

    def now(self) -> datetime:
        return self.current

    def monotonic(self) -> float:
        return (self.current - NOW).total_seconds()

    async def sleep(self, seconds: float) -> None:
        self.current += timedelta(seconds=seconds)


@dataclass
class _Provider:
    replies: list[Reply]
    requests: list[httpx.Request] = field(default_factory=list)
    trace: InMemoryLlmCallTrace = field(default_factory=InMemoryLlmCallTrace)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if not self.replies:
            raise AssertionError("unexpected provider call")
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    def generate(
        self,
        *,
        base_manifest: CallManifest | None = BASE_MANIFEST,
        fallback: ModelProfile = FALLBACK,
    ) -> GatewayResult:
        async def exercise() -> GatewayResult:
            clock = _Clock()
            async with httpx.AsyncClient(transport=httpx.MockTransport(self._handle)) as client:
                gateway = LlmGateway(
                    LlmSettings(primary=PRIMARY, fallback=fallback),
                    OpenAICompatibleTransport(client),
                    InMemoryUsageLedger(),
                    self.trace,
                    checkpoint=context_checkpoint,
                    clock=clock,
                    sleep=clock.sleep,
                    jitter=lambda: 0.0,
                    monotonic=clock.monotonic,
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

    def kinds(self) -> list[str]:
        return [record.kind.value for _, record in self.trace.records]


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
    # Live pin: the run retry delay ignores the hint, as before.
    assert classify_failure(failure).retry_after is None
    assert RetryDelays().for_failure(1, classify_failure(failure)) == (
        "2m",
        timedelta(minutes=2),
    )


def test_the_hint_is_the_last_calls_not_an_earlier_one() -> None:
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


def test_a_long_retry_after_does_not_move_the_live_run_retry_delay() -> None:
    # 300 s would select the 10m delay if classify_failure read the hint.
    failure = classify_failure(
        LlmCallFailed(LlmErrorCode.RATE_LIMITED, "rate limited", retry_after_s=300.0)
    )

    assert failure.retry_after is None
    assert RetryDelays().for_failure(1, failure) == ("2m", timedelta(minutes=2))
