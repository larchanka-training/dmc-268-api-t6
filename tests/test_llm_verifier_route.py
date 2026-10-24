"""The verifier model pair: ``LLM_VERIFIER_*`` settings and the gateway's verifier route (#130).

No network and no keys: settings come from literal mappings, provider answers from
``httpx.MockTransport``.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Literal
from uuid import UUID

import httpx
import pytest

from app.modules.reviews.application.llm import LlmCallFailed, LlmErrorCode, RunCallContext
from app.modules.reviews.infrastructure.llm.answers import InvalidAnswer
from app.modules.reviews.infrastructure.llm.gateway import (
    GatewayResult,
    LlmGateway,
    StructuredTask,
)
from app.modules.reviews.infrastructure.llm.memory import InMemoryLlmCallTrace, InMemoryUsageLedger
from app.modules.reviews.infrastructure.llm.settings import (
    LlmConfigError,
    LlmSettings,
    ModelPrice,
    ModelProfile,
)
from app.modules.reviews.infrastructure.llm.transport import (
    ChatMessage,
    OpenAICompatibleTransport,
    ResponseSchema,
)
from tests.llm_fakes import context_checkpoint

EUROUTER = "https://api.eurouter.ai/api/v1"
REVIEW_ENV = {
    "LLM_MODEL": "mistral-small-4",
    "LLM_API_KEYS": "sk-eu-1",
    "LLM_FALLBACK_MODEL": "mistral-small-3.2-24b",
}
SELF_HOSTED_ENV = {
    "LLM_MODEL": "local-review",
    "LLM_BASE_URL": "http://localhost:1234/v1",
    "LLM_CONTEXT_WINDOW": "32768",
}


# ---------- settings ----------


@pytest.mark.parametrize("value", [None, ""], ids=["absent", "empty"])
def test_without_verifier_variables_the_verifier_route_is_the_review_pair(
    value: str | None,
) -> None:
    env = dict(REVIEW_ENV)
    if value is not None:
        env |= {"LLM_VERIFIER_MODEL": value, "LLM_VERIFIER_FALLBACK_MODEL": value}

    settings = LlmSettings.from_env(env)

    assert settings.verifier is None
    assert settings.verifier_fallback is None
    assert settings.fallback is not None
    assert settings.verifier_route() == (settings.primary, settings.fallback)
    assert [profile.model for profile in settings.verifier_route() if profile] == [
        "mistral-small-4",
        "mistral-small-3.2-24b",
    ]


def test_a_known_verifier_model_on_the_primary_endpoint_inherits_its_keys() -> None:
    settings = LlmSettings.from_env({**REVIEW_ENV, "LLM_VERIFIER_MODEL": "mistral-small-3.2-24b"})

    verifier, verifier_fallback = settings.verifier_route()
    assert settings.verifier is verifier
    assert verifier.model == "mistral-small-3.2-24b"
    assert verifier.base_url == EUROUTER
    assert verifier.api_keys == ("sk-eu-1",)
    assert verifier.context_window == 128_000
    assert settings.primary.model == "mistral-small-4"
    # Without LLM_VERIFIER_FALLBACK_MODEL the already-parsed review fallback is reused.
    assert verifier_fallback is settings.fallback


def test_only_a_verifier_fallback_keeps_the_review_primary() -> None:
    env = {"LLM_MODEL": "mistral-small-3.2-24b", "LLM_API_KEYS": "sk-eu-1"}

    settings = LlmSettings.from_env({**env, "LLM_VERIFIER_FALLBACK_MODEL": "mistral-small-4"})

    verifier, verifier_fallback = settings.verifier_route()
    assert settings.verifier is None
    assert settings.fallback is None
    assert verifier is settings.primary
    assert verifier_fallback is settings.verifier_fallback
    assert verifier_fallback is not None
    assert verifier_fallback.model == "mistral-small-4"
    assert verifier_fallback.api_keys == ("sk-eu-1",)
    assert verifier_fallback.context_window == 262_144


def test_the_verifier_fallback_inherits_from_the_verifier_not_the_review_primary() -> None:
    settings = LlmSettings.from_env(
        {
            **REVIEW_ENV,
            "LLM_VERIFIER_MODEL": "verifier-model",
            "LLM_VERIFIER_BASE_URL": "https://verifier.test/v1",
            "LLM_VERIFIER_API_KEYS": "sk-verifier-1",
            "LLM_VERIFIER_CONTEXT_WINDOW": "64000",
            "LLM_VERIFIER_FALLBACK_MODEL": "verifier-fallback-model",
            "LLM_VERIFIER_FALLBACK_CONTEXT_WINDOW": "32000",
        }
    )

    _, verifier_fallback = settings.verifier_route()
    assert verifier_fallback is not None
    assert verifier_fallback.base_url == "https://verifier.test/v1"
    assert verifier_fallback.api_keys == ("sk-verifier-1",)


def test_a_known_verifier_model_behind_a_self_hosted_primary_needs_its_own_keys() -> None:
    with pytest.raises(LlmConfigError) as caught:
        LlmSettings.from_env({**SELF_HOSTED_ENV, "LLM_VERIFIER_MODEL": "mistral-small-4"})

    assert str(caught.value) == "LLM_VERIFIER_API_KEYS must be set for mistral-small-4"


def test_an_unknown_verifier_model_without_a_base_url_uses_the_primary_endpoint() -> None:
    settings = LlmSettings.from_env(
        {
            **SELF_HOSTED_ENV,
            "LLM_VERIFIER_MODEL": "local-verifier",
            "LLM_VERIFIER_CONTEXT_WINDOW": "16384",
        }
    )

    assert settings.verifier is not None
    assert settings.verifier.base_url == "http://localhost:1234/v1"
    assert settings.verifier.context_window == 16_384


def test_verifier_prompt_json_requires_the_dev_flag() -> None:
    env = {
        **REVIEW_ENV,
        "LLM_VERIFIER_MODEL": "mistral-small-3.2-24b",
        "LLM_VERIFIER_STRUCTURED_OUTPUT": "prompt_json",
    }

    with pytest.raises(LlmConfigError) as caught:
        LlmSettings.from_env(env)

    assert str(caught.value) == (
        "LLM_VERIFIER_STRUCTURED_OUTPUT=prompt_json needs LLM_ALLOW_PROMPT_JSON=1 (dev/eval only)"
    )


def test_primary_keys_are_not_sent_to_another_verifier_host() -> None:
    settings = LlmSettings.from_env(
        {
            **REVIEW_ENV,
            "LLM_VERIFIER_MODEL": "verifier-model",
            "LLM_VERIFIER_BASE_URL": "https://verifier.test/v1",
            "LLM_VERIFIER_CONTEXT_WINDOW": "64000",
        }
    )

    assert settings.verifier is not None
    assert settings.verifier.api_keys == ()
    assert settings.verifier.provider == "self-hosted"


# ---------- gateway route ----------

RUN_ID = UUID("00000000-0000-0000-0000-000000013001")
NOW = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)
RUN = RunCallContext(
    run_id=RUN_ID,
    workspace_id=UUID("00000000-0000-0000-0000-000000013002"),
    attempt=1,
    engine="fast",
    deadline=NOW + timedelta(seconds=480),
)
_BASE = ModelProfile(
    provider="router",
    base_url="https://router.test/v1",
    model="",
    context_window=100_000,
    max_output_tokens=1_000,
    price=ModelPrice(Decimal("1"), Decimal("2")),
    api_keys=("sk-test-1",),
)
REVIEW = replace(_BASE, model="review-model")
REVIEW_FALLBACK = replace(_BASE, model="review-fallback-model")
VERIFIER = replace(_BASE, model="verifier-model")
VERIFIER_FALLBACK = replace(_BASE, model="verifier-fallback-model")
BOTH_PAIRS = LlmSettings(
    primary=REVIEW,
    fallback=REVIEW_FALLBACK,
    verifier=VERIFIER,
    verifier_fallback=VERIFIER_FALLBACK,
)
ANSWER = {"ok": True}

type Reply = httpx.Response | Exception


def _validate(answer: str) -> dict[str, object]:
    try:
        parsed = json.loads(answer)
    except json.JSONDecodeError:
        raise InvalidAnswer(["the answer is not valid JSON"]) from None
    if parsed != ANSWER:
        raise InvalidAnswer(['the answer must be {"ok": true}'])
    return {"ok": True}


def _task(route: Literal["review", "verifier"] = "verifier", user: str = "USER") -> StructuredTask:
    return StructuredTask(
        operation="verification",
        messages=(ChatMessage("system", "SYS"), ChatMessage("user", user)),
        schema=ResponseSchema("Answer", {"type": "object"}),
        validate=_validate,
        route=route,
    )


def _completion(content: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "model": "answering-model",
            "choices": [{"message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        },
    )


def valid() -> httpx.Response:
    return _completion(json.dumps(ANSWER))


def invalid() -> httpx.Response:
    return _completion("not json")


def unavailable() -> httpx.Response:
    return httpx.Response(503, json={"error": {"message": "overloaded"}})


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

    def run(
        self, settings: LlmSettings, *tasks: StructuredTask
    ) -> list[GatewayResult | LlmCallFailed]:
        """Run the tasks one after another in the same (run, attempt), as stages do."""

        async def exercise() -> list[GatewayResult | LlmCallFailed]:
            clock = _Clock()
            async with httpx.AsyncClient(transport=httpx.MockTransport(self._handle)) as client:
                gateway = LlmGateway(
                    settings,
                    OpenAICompatibleTransport(client),
                    InMemoryUsageLedger(),
                    self.trace,
                    checkpoint=context_checkpoint,
                    clock=clock,
                    sleep=clock.sleep,
                    jitter=lambda: 0.0,
                    monotonic=clock.monotonic,
                )
                outcomes: list[GatewayResult | LlmCallFailed] = []
                for task in tasks:
                    try:
                        outcomes.append(await gateway.generate(task, RUN))
                    except LlmCallFailed as failure:
                        outcomes.append(failure)
                return outcomes

        return asyncio.run(exercise())

    def models(self) -> list[str]:
        return [json.loads(request.content)["model"] for request in self.requests]

    def kinds(self) -> list[str]:
        return [record.kind.value for _, record in self.trace.records]


def test_a_verifier_task_calls_the_verifier_then_the_verifier_fallback() -> None:
    provider = _Provider([invalid(), invalid(), valid()])

    [result] = provider.run(BOTH_PAIRS, _task("verifier"))

    assert isinstance(result, GatewayResult)
    assert result.output == {"ok": True}
    assert provider.kinds() == ["primary", "repair", "fallback"]
    assert provider.models() == ["verifier-model", "verifier-model", "verifier-fallback-model"]


def test_a_review_task_keeps_the_review_pair_when_a_verifier_is_set() -> None:
    provider = _Provider([invalid(), invalid(), valid()])

    [result] = provider.run(BOTH_PAIRS, _task("review"))

    assert isinstance(result, GatewayResult)
    assert provider.models() == ["review-model", "review-model", "review-fallback-model"]


def test_without_a_verifier_pair_a_verifier_task_uses_the_review_pair() -> None:
    provider = _Provider([httpx.Response(402, json={"error": {"message": "no credits"}}), valid()])

    [result] = provider.run(LlmSettings(primary=REVIEW, fallback=REVIEW_FALLBACK), _task())

    assert isinstance(result, GatewayResult)
    assert provider.kinds() == ["primary", "fallback"]
    assert provider.models() == ["review-model", "review-fallback-model"]


def test_the_verifier_fallback_keeps_the_last_call_slot_without_a_review_fallback() -> None:
    settings = replace(BOTH_PAIRS, fallback=None)
    provider = _Provider([unavailable(), unavailable(), httpx.ReadTimeout("slow"), valid()])

    [result] = provider.run(settings, _task())

    assert isinstance(result, GatewayResult)
    assert provider.kinds() == ["primary", "retry", "retry", "fallback"]
    assert provider.models() == [
        "verifier-model",
        "verifier-model",
        "verifier-model",
        "verifier-fallback-model",
    ]


def test_the_verifier_window_bounds_a_verifier_task() -> None:
    small = replace(VERIFIER, context_window=1_200)
    settings = replace(BOTH_PAIRS, verifier=small)
    # "SYS" + 1000 characters = 1 + 334 tokens; + 1000 reserved > 1200.
    provider = _Provider([valid()])

    [overflow, review] = provider.run(
        settings, _task("verifier", user="U" * 1_000), _task("review", user="U" * 1_000)
    )

    assert isinstance(overflow, LlmCallFailed)
    assert overflow.error_code is LlmErrorCode.CONTEXT_OVERFLOW
    assert "prompt ~335 tokens + 1000 reserved exceeds 1200" in str(overflow)
    assert isinstance(review, GatewayResult)
    assert provider.models() == ["review-model"]


def test_after_three_review_calls_the_verifier_gets_one_call_and_keeps_its_error() -> None:
    # Shared cap of 4 calls per (run, attempt), no reserve (plan, "Call budget").
    provider = _Provider([invalid(), invalid(), valid(), invalid()])

    review, verifier = provider.run(BOTH_PAIRS, _task("review"), _task("verifier"))

    assert isinstance(review, GatewayResult)
    assert review.calls == 3
    assert isinstance(verifier, LlmCallFailed)
    assert verifier.error_code is LlmErrorCode.INVALID_OUTPUT
    assert verifier.calls == 1
    assert provider.models()[3:] == ["verifier-model"]
    assert len(provider.requests) == 4


def test_after_four_review_calls_the_verifier_makes_no_request() -> None:
    provider = _Provider([unavailable(), unavailable(), unavailable(), valid()])

    review, verifier = provider.run(BOTH_PAIRS, _task("review"), _task("verifier"))

    assert isinstance(review, GatewayResult)
    assert review.calls == 4
    assert isinstance(verifier, LlmCallFailed)
    assert verifier.error_code is LlmErrorCode.UNAVAILABLE
    assert "already made 4 provider calls" in str(verifier)
    assert verifier.calls == 0
    assert len(provider.requests) == 4


# ---------- the verifier pair a policy snapshot pins (seam with D) ----------


def _pinned_models(env: dict[str, str]) -> tuple[str, str | None, str, str | None]:
    from app.worker import execution_profile_of

    profile = execution_profile_of(LlmSettings.from_env(env))
    return (
        profile.review_model,
        profile.fallback_review_model,
        profile.verifier_model,
        profile.fallback_verifier_model,
    )


def test_the_snapshot_pins_the_verifier_pair_from_the_verifier_route() -> None:
    env = {
        **REVIEW_ENV,
        "LLM_VERIFIER_MODEL": "mistral-small-3.2-24b",
        "LLM_VERIFIER_FALLBACK_MODEL": "mistral-small-4",
    }

    assert _pinned_models(env) == (
        "mistral-small-4",
        "mistral-small-3.2-24b",
        "mistral-small-3.2-24b",
        "mistral-small-4",
    )


def test_without_verifier_variables_the_snapshot_pins_the_review_pair_twice() -> None:
    # Live pin: the default environment pins what it pinned before #130 PR E.
    assert _pinned_models(REVIEW_ENV) == (
        "mistral-small-4",
        "mistral-small-3.2-24b",
        "mistral-small-4",
        "mistral-small-3.2-24b",
    )


def test_a_verifier_model_without_any_fallback_pins_no_verifier_fallback() -> None:
    env = {
        "LLM_MODEL": "mistral-small-4",
        "LLM_API_KEYS": "sk-eu-1",
        "LLM_VERIFIER_MODEL": "mistral-small-3.2-24b",
    }

    assert _pinned_models(env) == ("mistral-small-4", None, "mistral-small-3.2-24b", None)
