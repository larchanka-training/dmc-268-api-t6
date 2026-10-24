"""Pre-send token estimate: the pinned counter and the gateway heuristic stay equal (#130).

Decision р.4 №2 of #130 (tech lead, 2026-10-24): the gateway estimates with the task's
pinned counter, so the fitted prompt is the prompt sent; a parity test, not a max, keeps
counter/v2 equal to the model profile's ``chars_per_token`` heuristic.
No network: provider answers come from ``httpx.MockTransport``.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import httpx
import pytest

from app.modules.reviews.application.llm import RunCallContext
from app.modules.reviews.application.prompt_budget import EstimatedTokenCounter, TokenCounter
from app.modules.reviews.infrastructure.llm.gateway import (
    HeuristicTokenCounter,
    LlmGateway,
    StructuredTask,
)
from app.modules.reviews.infrastructure.llm.memory import InMemoryLlmCallTrace, InMemoryUsageLedger
from app.modules.reviews.infrastructure.llm.settings import (
    KNOWN_MODELS,
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

NOW = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)
RUN = RunCallContext(
    run_id=UUID("00000000-0000-0000-0000-000000013101"),
    workspace_id=UUID("00000000-0000-0000-0000-000000013102"),
    attempt=1,
    engine="fast",
    deadline=NOW + timedelta(seconds=480),
)
PROFILE = ModelProfile(
    provider="router",
    base_url="https://router.test/v1",
    model="parity-model",
    context_window=100_000,
    max_output_tokens=1_000,
    price=ModelPrice(Decimal("1"), Decimal("2")),
    api_keys=("sk-test-1",),
)
# 300 + 3001 characters: 100 + 1001 = 1101 tokens at 3 characters per token.
MESSAGES = (ChatMessage("system", "S" * 300), ChatMessage("user", "U" * 3001))
LENGTHS = [0, 1, 3, 4, 3000, 3001]
TOKENS_AT_THREE = [0, 1, 1, 2, 1000, 1001]


class _Clock:
    def now(self) -> datetime:
        return NOW


@dataclass
class _Provider:
    requests: list[httpx.Request] = field(default_factory=list)
    trace: InMemoryLlmCallTrace = field(default_factory=InMemoryLlmCallTrace)
    payment_required_first: bool = False

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.payment_required_first and len(self.requests) == 1:
            return httpx.Response(402, json={"error": {"message": "no credits"}})
        return httpx.Response(
            200,
            json={
                "model": "parity-model",
                "choices": [{"message": {"role": "assistant", "content": '{"ok": true}'}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            },
        )

    def generate(
        self,
        profile: ModelProfile,
        counter: TokenCounter | None,
        fallback: ModelProfile | None = None,
    ) -> None:
        async def exercise() -> None:
            async with httpx.AsyncClient(transport=httpx.MockTransport(self._handle)) as client:
                gateway = LlmGateway(
                    LlmSettings(primary=profile, fallback=fallback),
                    OpenAICompatibleTransport(client),
                    InMemoryUsageLedger(),
                    self.trace,
                    checkpoint=context_checkpoint,
                    clock=_Clock(),
                )
                task = StructuredTask(
                    operation="review",
                    messages=MESSAGES,
                    schema=ResponseSchema("Answer", {"type": "object"}),
                    validate=lambda answer: dict(json.loads(answer)),
                    counter=counter,
                )
                await gateway.generate(task, RUN)

        asyncio.run(exercise())

    def estimates(self) -> list[int]:
        return [record.input_tokens_estimate for _, record in self.trace.records]


@pytest.mark.parametrize(
    "counter",
    [EstimatedTokenCounter(), HeuristicTokenCounter(3.0)],
    ids=["counter-v2", "heuristic-3.0"],
)
def test_counter_v2_and_the_three_character_heuristic_agree(counter: TokenCounter) -> None:
    assert [counter.count("x" * length) for length in LENGTHS] == TOKENS_AT_THREE


def test_every_known_model_counts_three_characters_per_token() -> None:
    assert {name: profile.chars_per_token for name, profile in KNOWN_MODELS.items()} == {
        "mistral-small-4": 3.0,
        "mistral-small-3.2-24b": 3.0,
    }


@pytest.mark.parametrize(
    "env",
    [
        {"LLM_MODEL": "mistral-small-4", "LLM_API_KEYS": "sk-eu-1"},
        {"LLM_MODEL": "mistral-small-3.2-24b", "LLM_API_KEYS": "sk-eu-1"},
        {
            "LLM_MODEL": "local-model",
            "LLM_BASE_URL": "http://localhost:1234/v1",
            "LLM_CONTEXT_WINDOW": "32768",
        },
    ],
    ids=["mistral-small-4", "mistral-small-3.2-24b", "unknown-self-hosted"],
)
def test_every_configured_profile_counts_like_counter_v2(env: dict[str, str]) -> None:
    profile = LlmSettings.from_env(env).primary
    heuristic = HeuristicTokenCounter(profile.chars_per_token)
    counter_v2 = EstimatedTokenCounter()

    assert profile.chars_per_token == 3.0
    assert [heuristic.count("x" * length) for length in LENGTHS] == TOKENS_AT_THREE
    assert [counter_v2.count("x" * length) for length in LENGTHS] == TOKENS_AT_THREE


@pytest.mark.parametrize("name", sorted(KNOWN_MODELS), ids=sorted(KNOWN_MODELS))
@pytest.mark.parametrize(
    "counter", [None, EstimatedTokenCounter()], ids=["no-counter", "counter-v2"]
)
def test_the_live_estimate_is_the_same_with_and_without_counter_v2(
    name: str, counter: TokenCounter | None
) -> None:
    # The catalog profile on a test endpoint: no ECB quote is needed to price the call.
    profile = replace(KNOWN_MODELS[name], base_url="https://router.test/v1", api_keys=("sk-1",))
    provider = _Provider()

    provider.generate(profile, counter)

    assert provider.estimates() == [1101]


@pytest.mark.parametrize(
    "chars_per_token",
    [
        # The 4.0 heuristic would give 75 + 751 = 826: the heuristic alone dies here.
        4.0,
        # The 2.0 heuristic would give 150 + 1501 = 1651: a max of both dies here.
        2.0,
    ],
)
def test_with_a_task_counter_the_estimate_is_the_counters_count(chars_per_token: float) -> None:
    provider = _Provider()

    provider.generate(replace(PROFILE, chars_per_token=chars_per_token), EstimatedTokenCounter())

    assert provider.estimates() == [1101]


def test_without_a_task_counter_each_call_uses_its_own_model_heuristic() -> None:
    provider = _Provider(payment_required_first=True)
    fallback = replace(PROFILE, model="dense-fallback", chars_per_token=2.0)

    provider.generate(PROFILE, None, fallback=fallback)

    assert [record.kind.value for _, record in provider.trace.records] == ["primary", "fallback"]
    # 300 / 2 = 150, 3001 / 2 = 1500.5 -> 1501.
    assert provider.estimates() == [1101, 1651]
