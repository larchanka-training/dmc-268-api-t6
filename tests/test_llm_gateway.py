"""LLM gateway policy on a fake HTTP transport (docs/PIPELINE_SPEC.md §3, §4.5, §5.1, §6).

No network and no keys: every provider answer comes from ``httpx.MockTransport``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import pytest

from app.bootstrap.llm_gateway import ReviewCase, review_case
from app.modules.reviews.application.conventions import ConventionsRequest, RepositoryFile
from app.modules.reviews.application.llm import (
    LlmCallFailed,
    LlmCallKind,
    LlmErrorCode,
    LlmUsage,
    RunCallContext,
)
from app.modules.reviews.application.prompt_budget import prompt_tokens
from app.modules.reviews.application.prompt_builder import (
    ChangedFile,
    DiffLine,
    PullRequestMeta,
    RepoConventions,
    ReviewContext,
    parse_unified_diff,
)
from app.modules.reviews.application.review_output import ReviewOutput, parse_review_output
from app.modules.reviews.infrastructure.llm.gateway import (
    GatewayResult,
    HeuristicTokenCounter,
    LlmGateway,
    StructuredTask,
)
from app.modules.reviews.infrastructure.llm.memory import (
    InMemoryLlmCallTrace,
    InMemoryUsageLedger,
)
from app.modules.reviews.infrastructure.llm.models import (
    GatewayConventionsModel,
    GatewayReviewModel,
)
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

REPO_ROOT = Path(__file__).resolve().parents[1]
RUN_ID = UUID("00000000-0000-0000-0000-000000003301")
WORKSPACE_ID = UUID("00000000-0000-0000-0000-000000003302")
PROMPT_VERSION_ID = UUID("00000000-0000-0000-0000-000000003303")
RULE_VERSION_ID = UUID("00000000-0000-0000-0000-000000003304")
NOW = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)

PRIMARY = ModelProfile(
    provider="eurouter",
    base_url="https://router.test/api/v1",
    model="primary-model",
    context_window=100_000,
    max_output_tokens=1_000,
    price=ModelPrice(Decimal("1"), Decimal("2")),
    api_keys=("sk-primary-1",),
)
FALLBACK = ModelProfile(
    provider="eurouter",
    base_url="https://router.test/api/v1",
    model="fallback-model",
    context_window=100_000,
    max_output_tokens=1_000,
    price=ModelPrice(Decimal("1"), Decimal("2")),
    api_keys=("sk-fallback-1",),
)

VALID_OUTPUT: dict[str, Any] = {
    "findings": [
        {
            "path": "app/service.py",
            "line": 4,
            "start_line": None,
            "severity": "high",
            "category": "correctness",
            "title": "Error is swallowed",
            "body": "The broad except hides the failure from the caller.",
            "suggestion": None,
            "confidence": 0.9,
            "rule_name": None,
        }
    ],
    "summary": {
        "problem": "The change hides a failure from its caller.",
        "done_well": "The new service keeps its dependencies injected.",
        "effort": "small",
    },
}

CONTEXT = ReviewContext(
    system="SYSTEM PROMPT",
    rules=(),
    agents_md=None,
    conventions=RepoConventions((), ()),
    pr_meta=PullRequestMeta("Title", None, "octo", "feature", "main", (), 1, 1, 0, False, False),
    changed_files=(
        ChangedFile(
            "app/service.py",
            "modified",
            (DiffLine(3, "context", "try:"), DiffLine(4, "added", "except Exception: pass")),
        ),
    ),
    omitted_files=(),
)

type Reply = httpx.Response | Callable[[httpx.Request], httpx.Response] | Exception


def completion(
    content: str | None,
    *,
    model: str = "primary-model-2026-09-01",
    prompt_tokens: int = 1200,
    completion_tokens: int = 300,
    cached_tokens: int = 0,
    cost: float | None = None,
    cost_currency: str | None = None,
    finish_reason: str = "stop",
) -> httpx.Response:
    usage: dict[str, Any] = {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
        "prompt_tokens_details": {"cached_tokens": cached_tokens},
    }
    if cost is not None:
        usage["cost"] = cost
    if cost_currency is not None:
        usage["cost_currency"] = cost_currency
    return httpx.Response(
        200,
        json={
            "id": "chatcmpl-1",
            "object": "chat.completion",
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": finish_reason,
                }
            ],
            "usage": usage,
        },
    )


def valid(**kwargs: Any) -> httpx.Response:
    return completion(json.dumps(VALID_OUTPUT), **kwargs)


def error(status: int, message: str = "provider error", **headers: str) -> httpx.Response:
    return httpx.Response(status, json={"error": {"message": message}}, headers=headers)


def timeout() -> Exception:
    return httpx.ReadTimeout("read timed out")


class FakeClock:
    def __init__(self) -> None:
        self.current = NOW
        self.ticks = 0.0
        self.sleeps: list[float] = []

    def now(self) -> datetime:
        return self.current

    def monotonic(self) -> float:
        return self.ticks

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.current += timedelta(seconds=seconds)
        self.ticks += seconds


@dataclass
class Harness:
    replies: list[Reply]
    primary: ModelProfile = PRIMARY
    fallback: ModelProfile | None = FALLBACK
    deadline_in_s: float = 480
    engine: str = "fast"
    attempt: int = 2
    clock: FakeClock = field(default_factory=FakeClock)
    ledger: InMemoryUsageLedger = field(default_factory=InMemoryUsageLedger)
    trace: InMemoryLlmCallTrace = field(default_factory=InMemoryLlmCallTrace)
    requests: list[httpx.Request] = field(default_factory=list)
    settings: LlmSettings | None = None

    @property
    def run(self) -> RunCallContext:
        return RunCallContext(
            run_id=RUN_ID,
            workspace_id=WORKSPACE_ID,
            attempt=self.attempt,
            engine="deep" if self.engine == "deep" else "fast",
            deadline=NOW + timedelta(seconds=self.deadline_in_s),
            prompt_version_id=PROMPT_VERSION_ID,
            rule_version_id=RULE_VERSION_ID,
        )

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if not self.replies:
            raise AssertionError("unexpected provider call")
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        if isinstance(reply, httpx.Response):
            return reply
        return reply(request)

    def gateway(self, client: httpx.AsyncClient) -> LlmGateway:
        settings = self.settings or LlmSettings(primary=self.primary, fallback=self.fallback)
        return LlmGateway(
            settings,
            OpenAICompatibleTransport(client),
            self.ledger,
            self.trace,
            clock=self.clock,
            sleep=self.clock.sleep,
            jitter=lambda: 0.25,
            monotonic=self.clock.monotonic,
        )

    async def _review(self, context: ReviewContext) -> GatewayResult:
        async with httpx.AsyncClient(transport=httpx.MockTransport(self._handle)) as client:
            model = GatewayReviewModel(self.gateway(client), self.run, _NoMeta())
            await model.draft_review(context=context)
            assert model.last_result is not None
            return model.last_result

    def review(self, context: ReviewContext = CONTEXT) -> GatewayResult:
        return asyncio.run(self._review(context))

    def failure(self, context: ReviewContext = CONTEXT) -> LlmCallFailed:
        with pytest.raises(LlmCallFailed) as caught:
            self.review(context)
        return caught.value

    def kinds(self) -> list[str]:
        return [record.kind.value for _, record in self.trace.records]

    def models(self) -> list[str]:
        return [record.model for _, record in self.trace.records]

    def bodies(self) -> list[dict[str, Any]]:
        return [json.loads(request.content) for request in self.requests]


class _NoMeta:
    async def get_pull_request_meta(self, run_id: UUID) -> PullRequestMeta | None:
        return None


# ---------- providers are configuration only ----------


def test_eurouter_is_selected_by_configuration_only() -> None:
    settings = LlmSettings.from_env(
        {
            "LLM_MODEL": "mistral-small-4",
            "LLM_API_KEYS": "sk-eu-1, sk-eu-2",
            "LLM_FALLBACK_MODEL": "mistral-small-3.2-24b",
        }
    )
    harness = Harness([valid(model="mistral/mistral-small-4")], settings=settings)

    result = harness.review()

    request = harness.requests[0]
    assert str(request.url) == "https://api.eurouter.ai/api/v1/chat/completions"
    assert request.headers["Authorization"] == "Bearer sk-eu-1"
    body = harness.bodies()[0]
    assert body["model"] == "mistral-small-4"
    assert body["temperature"] == 0
    assert [message["role"] for message in body["messages"]] == ["system", "user"]
    assert body["messages"][0]["content"] == "SYSTEM PROMPT"
    assert body["messages"][1]["content"].startswith("<custom_instructions>")
    response_format = body["response_format"]
    assert response_format["type"] == "json_schema"
    assert response_format["json_schema"]["name"] == "ReviewOutput"
    assert response_format["json_schema"]["strict"] is True
    schema = response_format["json_schema"]["schema"]
    assert "$schema" not in schema and "$comment" not in schema
    assert schema["additionalProperties"] is False
    assert result.output == VALID_OUTPUT
    assert result.provider == "eurouter"
    assert result.model == "mistral/mistral-small-4"
    assert settings.fallback is not None
    assert settings.fallback.api_keys == ("sk-eu-1", "sk-eu-2")
    assert settings.fallback.base_url == "https://api.eurouter.ai/api/v1"
    assert settings.fallback.model == "mistral-small-3.2-24b"
    assert settings.fallback.context_window == 128_000
    assert settings.fallback.structured_output == "json_schema"


def test_oq2_pair_keeps_the_sd15_catalog_prices_and_windows() -> None:
    # docs/SYSTEM_DESIGN.md §15: these prices feed the pre-call run cost check.
    settings = LlmSettings.from_env(
        {
            "LLM_MODEL": "mistral-small-4",
            "LLM_API_KEYS": "sk-eu-1",
            "LLM_FALLBACK_MODEL": "mistral-small-3.2-24b",
        }
    )

    primary, fallback = settings.primary, settings.fallback
    assert fallback is not None
    assert primary.price == ModelPrice(Decimal("0.56125"), Decimal("2.35725"), Decimal("0.56125"))
    assert primary.context_window == 262_144
    assert fallback.price == ModelPrice(Decimal("0.2245"), Decimal("0.449"), Decimal("0.2245"))
    assert fallback.context_window == 128_000
    # one maximal fast call: SD §13 60 000 - 8 000 reserved = 52 000 in + 8 000 out
    assert primary.price.cost_usd(tokens_in=52_000, tokens_out=8_000) == Decimal("0.048043")
    assert fallback.price.cost_usd(tokens_in=52_000, tokens_out=8_000) == Decimal("0.015266")


@pytest.mark.parametrize(
    ("rate", "primary_price", "fallback_price", "primary_call_usd", "fallback_call_usd"),
    [
        (
            "1.1204",
            ModelPrice(Decimal("0.56125"), Decimal("2.35725"), Decimal("0.56125")),
            ModelPrice(Decimal("0.2245"), Decimal("0.449"), Decimal("0.2245")),
            Decimal("0.048043"),
            Decimal("0.015266"),
        ),
        (
            "1.1225",
            ModelPrice(Decimal("0.56125"), Decimal("2.35725"), Decimal("0.56125")),
            ModelPrice(Decimal("0.2245"), Decimal("0.449"), Decimal("0.2245")),
            Decimal("0.048043"),
            Decimal("0.015266"),
        ),
        (
            "1.20",
            ModelPrice(Decimal("0.60"), Decimal("2.520"), Decimal("0.60")),
            ModelPrice(Decimal("0.240"), Decimal("0.480"), Decimal("0.240")),
            Decimal("0.051360"),
            Decimal("0.016320"),
        ),
    ],
)
def test_known_eurouter_prices_cover_eur_routes_at_the_configured_rate(
    rate: str,
    primary_price: ModelPrice,
    fallback_price: ModelPrice,
    primary_call_usd: Decimal,
    fallback_call_usd: Decimal,
) -> None:
    settings = LlmSettings.from_env(
        {
            "LLM_MODEL": "mistral-small-4",
            "LLM_API_KEYS": "sk-eu-1",
            "LLM_FALLBACK_MODEL": "mistral-small-3.2-24b",
            "LLM_EUR_TO_USD_RATE": rate,
        }
    )

    assert settings.primary.price == primary_price
    assert settings.primary.price.cost_usd(tokens_in=52_000, tokens_out=8_000) == primary_call_usd
    assert settings.fallback is not None
    assert settings.fallback.price == fallback_price
    assert settings.fallback.price.cost_usd(tokens_in=52_000, tokens_out=8_000) == fallback_call_usd


def test_equivalent_eurouter_url_keeps_the_rate_aware_floor_and_provider() -> None:
    settings = LlmSettings.from_env(
        {
            "LLM_MODEL": "mistral-small-4",
            "LLM_BASE_URL": "HTTPS://API.EUROUTER.AI:443/api/v1/",
            "LLM_API_KEYS": "sk-eu-1",
            "LLM_EUR_TO_USD_RATE": "1.20",
        }
    )

    assert settings.primary.provider == "eurouter"
    assert settings.primary.price == ModelPrice(Decimal("0.60"), Decimal("2.520"), Decimal("0.60"))


def test_fallback_inherits_keys_across_equivalent_eurouter_urls() -> None:
    settings = LlmSettings.from_env(
        {
            "LLM_MODEL": "mistral-small-4",
            "LLM_BASE_URL": "https://API.EUROUTER.AI:443/api/v1",
            "LLM_API_KEYS": "sk-eu-1",
            "LLM_FALLBACK_MODEL": "mistral-small-3.2-24b",
            "LLM_EUR_TO_USD_RATE": "1.20",
        }
    )

    assert settings.fallback is not None
    assert settings.fallback.api_keys == ("sk-eu-1",)
    assert settings.fallback.price == ModelPrice(
        Decimal("0.240"), Decimal("0.480"), Decimal("0.240")
    )


def test_eurouter_host_with_unrecognized_path_cannot_bypass_the_price_floor() -> None:
    with pytest.raises(LlmConfigError, match="LLM_BASE_URL"):
        LlmSettings.from_env(
            {
                "LLM_MODEL": "mistral-small-4",
                "LLM_BASE_URL": "https://api.eurouter.ai/api/%76%31",
                "LLM_API_KEYS": "sk-eu-1",
                "LLM_EUR_TO_USD_RATE": "1.20",
            }
        )


@pytest.mark.parametrize("prefix", ["LLM_", "LLM_FALLBACK_"])
def test_unicode_dot_eurouter_host_cannot_bypass_the_price_floor(prefix: str) -> None:
    env = {
        "LLM_MODEL": "mistral-small-4",
        "LLM_API_KEYS": "sk-eu-1",
        "LLM_EUR_TO_USD_RATE": "1.20",
        f"{prefix}BASE_URL": "https://api.eurouter.ai。/api/v1",
        f"{prefix}PRICE_INPUT_PER_MTOK": "0.01",
        f"{prefix}PRICE_OUTPUT_PER_MTOK": "0.01",
    }
    if prefix == "LLM_FALLBACK_":
        env["LLM_FALLBACK_MODEL"] = "mistral-small-3.2-24b"
        env["LLM_FALLBACK_API_KEYS"] = "sk-eu-2"

    with pytest.raises(LlmConfigError, match=f"{prefix}BASE_URL"):
        LlmSettings.from_env(env)


def test_large_representable_rate_can_price_a_known_route() -> None:
    settings = LlmSettings.from_env(
        {
            "LLM_MODEL": "mistral-small-4",
            "LLM_API_KEYS": "sk-eu-1",
            "LLM_EUR_TO_USD_RATE": "1e20",
        }
    )

    assert settings.primary.price.cost_usd(tokens_in=52_000, tokens_out=8_000) == Decimal(
        "4280000000000000000.000000"
    )


def test_unquantizable_rate_is_rejected_before_gateway_start() -> None:
    with pytest.raises(LlmConfigError, match="LLM_EUR_TO_USD_RATE"):
        LlmSettings.from_env(
            {
                "LLM_MODEL": "mistral-small-4",
                "LLM_API_KEYS": "sk-eu-1",
                "LLM_EUR_TO_USD_RATE": "1e30",
            }
        )


def test_known_eurouter_price_floor_survives_lower_env_overrides() -> None:
    settings = LlmSettings.from_env(
        {
            "LLM_MODEL": "mistral-small-4",
            "LLM_API_KEYS": "sk-eu-1",
            "LLM_FALLBACK_MODEL": "mistral-small-3.2-24b",
            "LLM_EUR_TO_USD_RATE": "1.20",
            "LLM_PRICE_INPUT_PER_MTOK": "0.01",
            "LLM_PRICE_OUTPUT_PER_MTOK": "0.01",
            "LLM_PRICE_CACHE_READ_PER_MTOK": "0.01",
            "LLM_FALLBACK_PRICE_INPUT_PER_MTOK": "0.01",
            "LLM_FALLBACK_PRICE_OUTPUT_PER_MTOK": "0.01",
            "LLM_FALLBACK_PRICE_CACHE_READ_PER_MTOK": "0.01",
        }
    )

    assert settings.primary.price == ModelPrice(Decimal("0.60"), Decimal("2.520"), Decimal("0.60"))
    assert settings.fallback is not None
    assert settings.fallback.price == ModelPrice(
        Decimal("0.240"), Decimal("0.480"), Decimal("0.240")
    )


def test_known_route_cache_reads_use_the_effective_input_price() -> None:
    settings = LlmSettings.from_env(
        {
            "LLM_MODEL": "mistral-small-4",
            "LLM_API_KEYS": "sk-eu-1",
            "LLM_FALLBACK_MODEL": "mistral-small-3.2-24b",
            "LLM_EUR_TO_USD_RATE": "1.20",
            "LLM_PRICE_INPUT_PER_MTOK": "0.70",
            "LLM_PRICE_CACHE_READ_PER_MTOK": "0.01",
            "LLM_FALLBACK_PRICE_INPUT_PER_MTOK": "0.30",
            "LLM_FALLBACK_PRICE_CACHE_READ_PER_MTOK": "0.01",
        }
    )

    assert settings.primary.price == ModelPrice(Decimal("0.70"), Decimal("2.520"), Decimal("0.70"))
    assert settings.fallback is not None
    assert settings.fallback.price == ModelPrice(Decimal("0.30"), Decimal("0.480"), Decimal("0.30"))


def test_custom_endpoint_keeps_its_explicit_price_for_a_known_model() -> None:
    settings = LlmSettings.from_env(
        {
            "LLM_MODEL": "mistral-small-4",
            "LLM_BASE_URL": "https://custom.test/v1",
            "LLM_API_KEYS": "sk-custom",
            "LLM_EUR_TO_USD_RATE": "1.20",
            "LLM_PRICE_INPUT_PER_MTOK": "0.1",
            "LLM_PRICE_OUTPUT_PER_MTOK": "0.2",
            "LLM_PRICE_CACHE_READ_PER_MTOK": "0.05",
        }
    )

    assert settings.primary.price == ModelPrice(Decimal("0.1"), Decimal("0.2"), Decimal("0.05"))


def test_self_hosted_is_selected_by_configuration_only_with_prompt_json() -> None:
    settings = LlmSettings.from_env(
        {
            "LLM_BASE_URL": "http://localhost:1234/v1/",
            "LLM_MODEL": "qwen3-coder-30b",
            "LLM_CONTEXT_WINDOW": "65536",
            "LLM_STRUCTURED_OUTPUT": "prompt_json",
            "LLM_ALLOW_PROMPT_JSON": "1",
        }
    )
    harness = Harness([valid(model="qwen3-coder-30b")], settings=settings)

    result = harness.review()

    request = harness.requests[0]
    assert str(request.url) == "http://localhost:1234/v1/chat/completions"
    assert "Authorization" not in request.headers
    body = harness.bodies()[0]
    assert body["model"] == "qwen3-coder-30b"
    assert "response_format" not in body
    assert result.provider == "self-hosted"
    assert parse_review_output(result.output) == ReviewOutput.model_validate(VALID_OUTPUT)
    assert harness.ledger.events[0][1].cost_usd == Decimal("0")


def test_prompt_json_path_rejects_a_fenced_answer_and_repairs_it() -> None:
    settings = LlmSettings(
        primary=replace(PRIMARY, structured_output="prompt_json", api_keys=()), fallback=None
    )
    fenced = "```json\n" + json.dumps(VALID_OUTPUT) + "\n```"
    harness = Harness([completion(fenced), valid()], settings=settings)

    result = harness.review()

    assert result.output == VALID_OUTPUT
    assert harness.kinds() == ["primary", "repair"]


def test_prompt_json_requires_the_dev_flag() -> None:
    with pytest.raises(LlmConfigError, match="LLM_ALLOW_PROMPT_JSON"):
        LlmSettings.from_env(
            {
                "LLM_BASE_URL": "http://localhost:1234/v1",
                "LLM_MODEL": "local",
                "LLM_CONTEXT_WINDOW": "32768",
                "LLM_STRUCTURED_OUTPUT": "prompt_json",
            }
        )


_KEY = {"LLM_API_KEYS": "sk-eu-1"}


@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({}, "LLM_MODEL must be set"),
        ({"LLM_MODEL": "unknown"}, "LLM_BASE_URL must be set"),
        (
            {"LLM_MODEL": "unknown", "LLM_BASE_URL": "http://llm.test/v1"},
            "LLM_CONTEXT_WINDOW must be set",
        ),
        ({"LLM_MODEL": "mistral-small-4"}, "LLM_API_KEYS must be set for mistral-small-4"),
        (
            {**_KEY, "LLM_MODEL": "mistral-small-4", "LLM_CONTEXT_WINDOW": "big"},
            "must be an integer",
        ),
        ({**_KEY, "LLM_MODEL": "mistral-small-4", "LLM_CONTEXT_WINDOW": "0"}, "must be positive"),
        (
            {**_KEY, "LLM_MODEL": "mistral-small-4", "LLM_MAX_OUTPUT_TOKENS": "2000000"},
            "LLM_MAX_OUTPUT_TOKENS must be below LLM_CONTEXT_WINDOW",
        ),
        ({**_KEY, "LLM_MODEL": "mistral-small-4", "LLM_PRICE_INPUT_PER_MTOK": "x"}, "a decimal"),
        (
            {**_KEY, "LLM_MODEL": "mistral-small-4", "LLM_PRICE_INPUT_PER_MTOK": "NaN"},
            "non-negative decimal",
        ),
        (
            {**_KEY, "LLM_MODEL": "mistral-small-4", "LLM_PRICE_OUTPUT_PER_MTOK": "-1"},
            "non-negative decimal",
        ),
        (
            {**_KEY, "LLM_MODEL": "mistral-small-4", "LLM_STRUCTURED_OUTPUT": "tools"},
            "json_schema or",
        ),
        (
            {**_KEY, "LLM_MODEL": "mistral-small-4", "LLM_BASE_URL": "HTTP://router.example/v1"},
            "LLM_BASE_URL must use https when keys are sent",
        ),
        ({**_KEY, "LLM_MODEL": "mistral-small-4", "LLM_EXTRA_BODY": "[1]"}, "JSON object"),
        ({**_KEY, "LLM_MODEL": "mistral-small-4", "LLM_EXTRA_BODY": "{"}, "JSON object"),
        (
            {
                **_KEY,
                "LLM_MODEL": "mistral-small-4",
                "LLM_BASE_URL": "http://router.example/v1",
            },
            "LLM_BASE_URL must use https when keys are sent",
        ),
        (
            {
                "LLM_BASE_URL": "http://localhost:1234/v1",
                "LLM_MODEL": "local",
                "LLM_CONTEXT_WINDOW": "32768",
                "LLM_FALLBACK_MODEL": "mistral-small-4",
            },
            "LLM_FALLBACK_API_KEYS must be set for mistral-small-4",
        ),
    ],
)
def test_incomplete_configuration_names_the_variable(env: dict[str, str], message: str) -> None:
    with pytest.raises(LlmConfigError, match=message):
        LlmSettings.from_env(env)


def test_primary_keys_are_not_sent_to_another_fallback_endpoint() -> None:
    settings = LlmSettings.from_env(
        {
            "LLM_MODEL": "mistral-small-4",
            "LLM_API_KEYS": "eurouter-secret",
            "LLM_FALLBACK_MODEL": "qwen",
            "LLM_FALLBACK_BASE_URL": "https://third-party.example/v1",
            "LLM_FALLBACK_CONTEXT_WINDOW": "32768",
        }
    )

    assert settings.fallback is not None
    assert settings.fallback.base_url == "https://third-party.example/v1"
    assert settings.fallback.api_keys == ()
    assert settings.fallback.provider == "self-hosted"


def test_a_known_fallback_keeps_its_own_endpoint_behind_a_self_hosted_primary() -> None:
    settings = LlmSettings.from_env(
        {
            "LLM_BASE_URL": "http://localhost:1234/v1",
            "LLM_MODEL": "local",
            "LLM_CONTEXT_WINDOW": "65536",
            "LLM_STRUCTURED_OUTPUT": "prompt_json",
            "LLM_ALLOW_PROMPT_JSON": "1",
            "LLM_FALLBACK_MODEL": "mistral-small-4",
            "LLM_FALLBACK_API_KEYS": "sk-eu-1",
        }
    )

    assert settings.primary.provider == "self-hosted"
    assert settings.fallback is not None
    assert settings.fallback.base_url == "https://api.eurouter.ai/api/v1"
    assert settings.fallback.provider == "eurouter"
    assert settings.fallback.structured_output == "json_schema"
    assert settings.fallback.api_keys == ("sk-eu-1",)


def test_an_unknown_fallback_on_the_same_endpoint_inherits_keys_and_output_mode() -> None:
    settings = LlmSettings.from_env(
        {
            "LLM_BASE_URL": "http://localhost:1234/v1",
            "LLM_MODEL": "local",
            "LLM_CONTEXT_WINDOW": "65536",
            "LLM_API_KEYS": "local-key",
            "LLM_STRUCTURED_OUTPUT": "prompt_json",
            "LLM_ALLOW_PROMPT_JSON": "1",
            "LLM_FALLBACK_MODEL": "local-small",
            "LLM_FALLBACK_CONTEXT_WINDOW": "32768",
        }
    )

    assert settings.fallback is not None
    assert settings.fallback.base_url == "http://localhost:1234/v1"
    assert settings.fallback.api_keys == ("local-key",)
    assert settings.fallback.structured_output == "prompt_json"


def test_a_remote_model_without_a_price_is_reported(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        LlmSettings.from_env(
            {
                "LLM_BASE_URL": "https://llm.example/v1",
                "LLM_MODEL": "remote",
                "LLM_CONTEXT_WINDOW": "65536",
            }
        )

    assert "llm model has no price" in caplog.text


def test_extra_body_and_overrides_reach_the_request() -> None:
    settings = LlmSettings.from_env(
        {
            "LLM_MODEL": "mistral-small-4",
            "LLM_API_KEYS": "sk-eu-1",
            "LLM_MAX_OUTPUT_TOKENS": "4000",
            "LLM_PRICE_INPUT_PER_MTOK": "0.7",
            "LLM_EXTRA_BODY": '{"provider": {"allow_fallbacks": false}}',
        }
    )
    harness = Harness([valid()], settings=settings)

    harness.review()

    body = harness.bodies()[0]
    assert body["provider"] == {"allow_fallbacks": False}
    assert body["max_tokens"] == 4000
    assert body["temperature"] == 0
    assert settings.primary.price.input_per_mtok == Decimal("0.7")


def test_extra_body_overrides_sampling_defaults_but_not_model_messages_or_schema() -> None:
    extra: dict[str, object] = {
        "temperature": None,
        "max_tokens": None,
        "max_completion_tokens": 8000,
        "reasoning_effort": "low",
        "model": "other-model",
        "messages": [],
        "response_format": {"type": "text"},
    }
    harness = Harness([valid()], primary=replace(PRIMARY, extra_body=extra))

    harness.review()

    body = harness.bodies()[0]
    assert "temperature" not in body
    assert "max_tokens" not in body
    assert body["max_completion_tokens"] == 8000
    assert body["reasoning_effort"] == "low"
    assert body["model"] == "primary-model"
    assert [message["role"] for message in body["messages"]] == ["system", "user"]
    assert body["response_format"]["json_schema"]["strict"] is True


# ---------- key rotation ----------


@pytest.mark.parametrize("status", [401, 403, 429])
def test_rejected_key_rotates_to_the_next_key_without_counting_a_call(status: int) -> None:
    primary = replace(PRIMARY, api_keys=("sk-a", "sk-b"))
    harness = Harness([error(status), valid(), valid()], primary=primary)

    async def two_attempts() -> list[int]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(harness._handle)) as client:
            gateway = harness.gateway(client)
            calls = []
            for attempt in (2, 3):
                run = replace(harness.run, attempt=attempt)
                model = GatewayReviewModel(gateway, run, _NoMeta())
                await model.draft_review(context=CONTEXT)
                assert model.last_result is not None
                calls.append(model.last_result.calls)
            return calls

    calls = asyncio.run(two_attempts())

    # the next attempt starts with the key that answered last
    assert [request.headers["Authorization"] for request in harness.requests] == [
        "Bearer sk-a",
        "Bearer sk-b",
        "Bearer sk-b",
    ]
    assert calls == [1, 1]
    assert harness.kinds() == ["primary", "primary"]
    assert [record.call_no for _, record in harness.trace.records] == [1, 1]


def test_rotations_inside_every_call_do_not_shrink_the_call_limit() -> None:
    primary = replace(PRIMARY, api_keys=("sk-a", "sk-b"))
    harness = Harness(
        [error(401), error(503), error(401), error(503), error(401), error(503), valid()],
        primary=primary,
    )

    result = harness.review()

    assert len(harness.requests) == 7
    assert result.calls == 4
    assert harness.kinds() == ["primary", "retry", "retry", "fallback"]


# ---------- failure classes, §5.1 ----------


def test_timeout_retries_once_then_uses_the_fallback_then_fails() -> None:
    harness = Harness([timeout(), timeout(), timeout()])

    failure = harness.failure()

    assert failure.error_code is LlmErrorCode.TIMEOUT
    assert failure.run_retryable is True
    assert harness.kinds() == ["primary", "retry", "fallback"]
    assert harness.models() == ["primary-model", "primary-model", "fallback-model"]
    assert harness.clock.sleeps == [2.25]
    assert failure.calls == 3


def test_rate_limit_without_free_keys_waits_retry_after_once_then_falls_back() -> None:
    harness = Harness(
        [
            error(429, **{"Retry-After": "5"}),
            error(429, **{"Retry-After": "5"}),
            error(429, **{"Retry-After": "5"}),
        ]
    )

    failure = harness.failure()

    assert failure.error_code is LlmErrorCode.RATE_LIMITED
    assert failure.run_retryable is True
    assert harness.kinds() == ["primary", "retry", "fallback"]
    assert harness.clock.sleeps == [5.0]


def test_rate_limit_with_a_long_retry_after_falls_back_at_once() -> None:
    harness = Harness([error(429, **{"Retry-After": "60"}), valid(model="fallback-model-v2")])

    result = harness.review()

    assert harness.kinds() == ["primary", "fallback"]
    assert harness.clock.sleeps == []
    assert result.model == "fallback-model-v2"


def test_rate_limit_without_retry_after_retries_once_after_the_default_backoff() -> None:
    harness = Harness([error(429), error(429), valid(model="fallback-model-v2")])

    result = harness.review()

    assert harness.kinds() == ["primary", "retry", "fallback"]
    assert harness.clock.sleeps == [2.25]
    assert result.model == "fallback-model-v2"


def test_rate_limit_without_retry_after_may_succeed_on_the_retry() -> None:
    harness = Harness([error(429), valid()])

    result = harness.review()

    assert harness.kinds() == ["primary", "retry"]
    assert result.output == VALID_OUTPUT


def test_server_errors_retry_twice_with_backoff_then_fall_back_within_four_calls() -> None:
    harness = Harness([error(503), error(502), httpx.ConnectError("refused"), error(500)])

    failure = harness.failure()

    assert failure.error_code is LlmErrorCode.UNAVAILABLE
    assert harness.kinds() == ["primary", "retry", "retry", "fallback"]
    assert harness.clock.sleeps == [2.25, 8.25]
    assert len(harness.requests) == 4


def test_fallback_answer_is_accepted_with_its_actual_model() -> None:
    harness = Harness([error(500), error(500), error(500), valid(model="fallback-model-v7")])

    result = harness.review()

    assert result.output == VALID_OUTPUT
    assert result.model == "fallback-model-v7"
    assert result.calls == 4
    assert [usage.model for usage in result.usage] == ["fallback-model-v7"]


def test_unauthorized_on_every_key_skips_same_model_retries() -> None:
    harness = Harness([error(401), error(403)])

    failure = harness.failure()

    assert failure.error_code is LlmErrorCode.UNAVAILABLE
    assert harness.kinds() == ["primary", "fallback"]


def test_http_200_without_a_completion_is_unavailable() -> None:
    harness = Harness([httpx.Response(200, text="<html>gateway</html>")], fallback=None)
    harness.replies += [httpx.Response(200, json={"choices": []})] * 2

    failure = harness.failure()

    assert failure.error_code is LlmErrorCode.UNAVAILABLE
    assert harness.kinds() == ["primary", "retry", "retry"]


@pytest.mark.parametrize(
    "replies",
    [
        [error(503), completion("{}"), completion("{}"), completion("{}"), valid()],
        [timeout(), error(500), error(500), error(500), valid()],
        [error(500), error(500), error(500), error(500), valid()],
        [completion("x"), timeout(), timeout(), timeout(), valid()],
    ],
)
def test_one_attempt_never_makes_more_than_four_calls(replies: list[Reply]) -> None:
    harness = Harness(list(replies))

    with pytest.raises(LlmCallFailed):
        harness.review()

    assert len(harness.requests) <= 4
    assert len(harness.trace.records) == len(harness.requests)
    assert harness.kinds()[-1] == "fallback"


def test_without_a_fallback_the_primary_gets_the_whole_call_limit() -> None:
    harness = Harness([error(500), error(500), error(500)], fallback=None)

    failure = harness.failure()

    assert failure.error_code is LlmErrorCode.UNAVAILABLE
    assert harness.kinds() == ["primary", "retry", "retry"]


def _conventions_request() -> ConventionsRequest:
    return ConventionsRequest("P", (), None, ("app/service.py",), (), {}, ("app/service.py",))


def _conventions_answer() -> dict[str, object]:
    return {
        "files": [{"path": "app/service.py", "relevance": "changed service"}],
        "key_patterns": ["a", "b", "c"],
        "recommendations": [f"Check {index} (from: standard/correctness)" for index in range(5)],
    }


def test_conventions_and_review_share_the_four_calls_of_one_attempt() -> None:
    harness = Harness(
        [
            error(503),
            error(503),
            completion(json.dumps(_conventions_answer())),
            error(503),
            valid(),
        ]
    )

    async def scenario() -> LlmCallFailed:
        async with httpx.AsyncClient(transport=httpx.MockTransport(harness._handle)) as client:
            gateway = harness.gateway(client)
            await GatewayConventionsModel(gateway, harness.run).draft_conventions(
                request=_conventions_request()
            )
            with pytest.raises(LlmCallFailed) as caught:
                await GatewayReviewModel(gateway, harness.run, _NoMeta()).draft_review(
                    context=CONTEXT
                )
            # a third generation in the same attempt has no call left at all
            with pytest.raises(LlmCallFailed) as exhausted:
                await GatewayReviewModel(gateway, harness.run, _NoMeta()).draft_review(
                    context=CONTEXT
                )
            assert exhausted.value.calls == 0
            assert exhausted.value.error_code is LlmErrorCode.UNAVAILABLE
            return caught.value

    failure = asyncio.run(scenario())

    assert failure.error_code is LlmErrorCode.UNAVAILABLE
    assert len(harness.requests) == 4
    assert [record.call_no for _, record in harness.trace.records] == [1, 2, 3, 4]
    assert harness.kinds() == ["primary", "retry", "retry", "primary"]


def test_a_new_attempt_of_the_run_gets_four_calls_again() -> None:
    harness = Harness([error(500), error(500), error(500), error(500), valid()])

    async def scenario() -> GatewayResult:
        async with httpx.AsyncClient(transport=httpx.MockTransport(harness._handle)) as client:
            gateway = harness.gateway(client)
            with pytest.raises(LlmCallFailed):
                await GatewayReviewModel(gateway, harness.run, _NoMeta()).draft_review(
                    context=CONTEXT
                )
            model = GatewayReviewModel(gateway, replace(harness.run, attempt=3), _NoMeta())
            await model.draft_review(context=CONTEXT)
            assert model.last_result is not None
            return model.last_result

    result = asyncio.run(scenario())

    assert result.calls == 1
    assert [record.call_no for _, record in harness.trace.records] == [1, 2, 3, 4, 1]
    assert [record.attempt for _, record in harness.trace.records] == [2, 2, 2, 2, 3]


def test_a_fallback_window_too_small_for_the_prompt_keeps_the_primary_error() -> None:
    harness = Harness(
        [error(503), error(503), error(503)], fallback=replace(FALLBACK, context_window=500)
    )

    failure = harness.failure()

    assert failure.error_code is LlmErrorCode.UNAVAILABLE
    assert failure.run_retryable is True
    assert harness.kinds() == ["primary", "retry", "retry"]


def test_two_runs_with_the_same_attempt_number_have_separate_limits() -> None:
    harness = Harness([error(500), error(500), error(500), error(500), valid()])
    other_run = UUID("00000000-0000-0000-0000-000000003399")

    async def scenario() -> GatewayResult:
        async with httpx.AsyncClient(transport=httpx.MockTransport(harness._handle)) as client:
            gateway = harness.gateway(client)
            with pytest.raises(LlmCallFailed):
                await GatewayReviewModel(gateway, harness.run, _NoMeta()).draft_review(
                    context=CONTEXT
                )
            model = GatewayReviewModel(gateway, replace(harness.run, run_id=other_run), _NoMeta())
            await model.draft_review(context=CONTEXT)
            assert model.last_result is not None
            return model.last_result

    assert asyncio.run(scenario()).calls == 1
    assert [record.call_no for _, record in harness.trace.records] == [1, 2, 3, 4, 1]


def test_concurrent_generations_of_one_attempt_stay_within_four_calls() -> None:
    class YieldingLedger(InMemoryUsageLedger):
        """Like the database ledger, the cost read lets other tasks run."""

        async def run_cost_usd(self, run_id: UUID) -> Decimal:
            await asyncio.sleep(0)
            return await super().run_cost_usd(run_id)

    replies: list[Reply] = [error(500) for _ in range(8)]
    harness = Harness(replies, fallback=None, ledger=YieldingLedger())

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(harness._handle)) as client:
            gateway = harness.gateway(client)
            results = await asyncio.gather(
                *(
                    GatewayReviewModel(gateway, harness.run, _NoMeta()).draft_review(
                        context=CONTEXT
                    )
                    for _ in range(2)
                ),
                return_exceptions=True,
            )
            assert all(isinstance(item, LlmCallFailed) for item in results)

    asyncio.run(scenario())

    assert len(harness.requests) == 4
    assert sorted(record.call_no for _, record in harness.trace.records) == [1, 2, 3, 4]


# ---------- invalid answers ----------


def test_invalid_answer_gets_one_repair_call_with_the_validator_errors() -> None:
    broken = dict(VALID_OUTPUT, verdict="clean")
    harness = Harness([completion(json.dumps(broken)), valid()])

    result = harness.review()

    assert result.output == VALID_OUTPUT
    assert harness.kinds() == ["primary", "repair"]
    repair_messages = harness.bodies()[1]["messages"]
    assert [message["role"] for message in repair_messages] == [
        "system",
        "user",
        "assistant",
        "user",
    ]
    assert repair_messages[2]["content"] == json.dumps(broken)
    assert (
        "Additional properties are not allowed ('verdict' was unexpected)"
        in (repair_messages[3]["content"])
    )


def test_invalid_answers_end_in_llm_invalid_output_after_repair_and_fallback() -> None:
    semantic = json.loads(json.dumps(VALID_OUTPUT))
    semantic["findings"][0]["title"] = "Ends with a period."
    harness = Harness(
        [
            completion("not json"),
            completion(json.dumps(semantic)),
            completion("[]"),
        ]
    )

    failure = harness.failure()

    assert failure.error_code is LlmErrorCode.INVALID_OUTPUT
    assert failure.run_retryable is False
    assert harness.kinds() == ["primary", "repair", "fallback"]
    assert "not valid JSON" in harness.bodies()[1]["messages"][-1]["content"]
    # the fallback gets the original request, not the repair conversation
    assert len(harness.bodies()[2]["messages"]) == 2
    assert "the answer must be one JSON object" in str(failure)
    # the invalid answers are kept in the trace as the provider sent them
    answers = [
        record.response_json()["choices"][0]["message"]["content"]
        for _, record in harness.trace.records
    ]
    assert answers == ["not json", json.dumps(semantic), "[]"]


@pytest.mark.parametrize(
    "reply",
    [
        # valid JSON that was nevertheless cut: the length flag alone makes it invalid
        completion(json.dumps({"findings": [], "summary": {}}), finish_reason="length"),
        completion(None),
    ],
)
def test_cut_or_empty_answer_is_invalid(reply: httpx.Response) -> None:
    harness = Harness([reply, valid()])

    harness.review()

    assert harness.kinds() == ["primary", "repair"]


def test_repair_that_would_overflow_the_context_is_skipped_for_the_fallback() -> None:
    estimate = prompt_tokens(CONTEXT, HeuristicTokenCounter(PRIMARY.chars_per_token))
    primary = replace(PRIMARY, context_window=estimate + PRIMARY.max_output_tokens + 50)
    harness = Harness([completion("x" * 3_000), valid(model="fallback-model-v3")], primary=primary)

    result = harness.review()

    assert harness.kinds() == ["primary", "fallback"]
    assert result.model == "fallback-model-v3"
    fallback_messages = harness.bodies()[1]["messages"]
    assert fallback_messages == harness.bodies()[0]["messages"]


def test_a_refusal_skips_the_repair_and_goes_to_the_fallback() -> None:
    refusal = httpx.Response(
        200,
        json={
            "model": "primary-model",
            "choices": [
                {
                    "message": {"role": "assistant", "content": None, "refusal": "I can't."},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 3},
        },
    )
    harness = Harness([refusal, valid(model="fallback-model-v8")])

    result = harness.review()

    assert harness.kinds() == ["primary", "fallback"]
    assert result.model == "fallback-model-v8"


def test_content_parts_are_joined_into_the_answer() -> None:
    text = json.dumps(VALID_OUTPUT)
    parts = [{"type": "text", "text": text[:50]}, {"type": "text", "text": text[50:]}]
    body = {
        "model": "primary-model",
        "choices": [{"message": {"content": parts}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 3},
    }
    harness = Harness([httpx.Response(200, json=body)])

    assert harness.review().output == VALID_OUTPUT
    assert harness.kinds() == ["primary"]


# ---------- context overflow, budget, deadline ----------


def test_prompt_over_the_limit_by_pre_count_makes_no_call() -> None:
    huge_system = "x" * 400_000
    harness = Harness([], primary=replace(PRIMARY, context_window=50_000))

    failure = harness.failure(replace(CONTEXT, system=huge_system))

    assert failure.error_code is LlmErrorCode.CONTEXT_OVERFLOW
    assert harness.requests == []
    assert failure.calls == 0


def test_provider_context_length_error_is_not_retried_nor_sent_to_the_fallback() -> None:
    harness = Harness(
        [
            httpx.Response(
                400,
                json={
                    "error": {
                        "code": "context_length_exceeded",
                        "message": "This model's maximum context length is 8192 tokens",
                    }
                },
            )
        ]
    )

    failure = harness.failure()

    assert failure.error_code is LlmErrorCode.CONTEXT_OVERFLOW
    assert failure.run_retryable is False
    assert harness.kinds() == ["primary"]
    error_json = harness.trace.records[0][1].response_json()
    assert error_json["error"]["class"] == "llm_context_overflow"
    assert error_json["error"]["http_status"] == 400


def test_run_cost_limit_stops_the_call_before_it_is_made() -> None:
    harness = Harness([valid()])
    asyncio.run(
        harness.ledger.record(
            harness.run,
            LlmUsage("eurouter", "primary-model", "review", 1, 1, 0, Decimal("0.499")),
        )
    )

    failure = harness.failure()

    assert failure.error_code is LlmErrorCode.BUDGET_EXCEEDED
    assert harness.requests == []


def test_run_cost_limit_counts_calls_of_this_attempt_and_stops_the_fallback() -> None:
    expensive = replace(FALLBACK, price=ModelPrice(Decimal("400"), Decimal("400")))
    harness = Harness(
        [completion("bad", cost=0.2), completion("bad", cost=0.2)], fallback=expensive
    )

    failure = harness.failure()

    assert failure.error_code is LlmErrorCode.BUDGET_EXCEEDED
    assert harness.kinds() == ["primary", "repair"]
    assert [usage.cost_usd for usage in failure.usage] == [Decimal("0.2"), Decimal("0.2")]


def test_deep_engine_uses_its_own_limits() -> None:
    harness = Harness([valid()], engine="deep", deadline_in_s=299)

    failure = harness.failure()

    assert failure.error_code is LlmErrorCode.DEADLINE_EXCEEDED


def test_no_call_when_less_than_the_call_timeout_is_left_before_the_deadline() -> None:
    harness = Harness([valid()], deadline_in_s=89)

    failure = harness.failure()

    assert failure.error_code is LlmErrorCode.DEADLINE_EXCEEDED
    assert failure.run_retryable is False
    assert harness.requests == []


def test_no_same_model_retry_when_the_backoff_leaves_less_than_the_call_timeout() -> None:
    harness = Harness([timeout(), valid(model="fallback-model-v4")], deadline_in_s=92)

    result = harness.review()

    # 92 s - 2.25 s of backoff < 90 s: the retry is skipped, the fallback still fits
    assert harness.kinds() == ["primary", "fallback"]
    assert harness.clock.sleeps == []
    assert result.model == "fallback-model-v4"


def test_a_backoff_that_leaves_exactly_one_call_timeout_still_retries() -> None:
    # 92.25 s - 2.25 s of backoff == the 90 s call timeout
    harness = Harness([timeout(), valid()], deadline_in_s=92.25)

    harness.review()

    assert harness.kinds() == ["primary", "retry"]
    assert harness.clock.sleeps == [2.25]


def test_long_retry_after_is_not_slept_when_it_would_pass_the_deadline() -> None:
    harness = Harness([error(429, **{"Retry-After": "30"})], deadline_in_s=110, fallback=None)

    failure = harness.failure()

    assert failure.error_code is LlmErrorCode.RATE_LIMITED
    assert harness.clock.sleeps == []
    assert harness.kinds() == ["primary"]


def test_no_backoff_after_the_last_same_model_call() -> None:
    harness = Harness([timeout(), error(503), error(503), valid(model="fallback-model-v5")])

    result = harness.review()

    assert harness.kinds() == ["primary", "retry", "retry", "fallback"]
    # timeout retry, then the first 5xx retry; no sleep after the third primary call
    assert harness.clock.sleeps == [2.25, 2.25]
    assert result.model == "fallback-model-v5"


def _advance(harness: Harness, seconds: float, reply: httpx.Response | Exception) -> Reply:
    """A provider answer that takes ``seconds`` of the fake clock."""

    def respond(request: httpx.Request) -> httpx.Response:
        harness.clock.current += timedelta(seconds=seconds)
        if isinstance(reply, Exception):
            raise reply
        return reply

    return respond


def test_no_repair_call_when_the_deadline_is_too_close() -> None:
    harness = Harness([], deadline_in_s=100)
    harness.replies = [_advance(harness, 20, completion("not json")), valid()]

    failure = harness.failure()

    assert failure.error_code is LlmErrorCode.DEADLINE_EXCEEDED
    assert harness.kinds() == ["primary"]
    assert len(harness.requests) == 1


def test_no_fallback_call_when_the_deadline_is_too_close() -> None:
    harness = Harness([], deadline_in_s=200)
    harness.replies = [
        _advance(harness, 90, timeout()),
        _advance(harness, 90, timeout()),
        valid(),
    ]

    failure = harness.failure()

    assert failure.error_code is LlmErrorCode.DEADLINE_EXCEEDED
    assert harness.kinds() == ["primary", "retry"]
    assert len(harness.requests) == 2


def test_exactly_one_call_timeout_left_still_allows_the_call() -> None:
    harness = Harness([valid()], deadline_in_s=90)

    harness.review()

    assert harness.kinds() == ["primary"]


async def _generate(harness: Harness, characters: int) -> GatewayResult:
    async with httpx.AsyncClient(transport=httpx.MockTransport(harness._handle)) as client:
        return await harness.gateway(client).generate(
            StructuredTask(
                operation="review",
                messages=(ChatMessage("user", "x" * characters),),
                schema=ResponseSchema("ReviewOutput", {"type": "object"}),
                validate=lambda content: json.loads(content),
            ),
            harness.run,
        )


@pytest.mark.parametrize(
    ("engine", "characters", "calls"),
    [
        # fast: min(window 200 000, SD §13 60 000) - 1 000 reserved = 59 000 tokens
        ("fast", 59_000 * 3, 1),
        ("fast", 59_000 * 3 + 1, 0),
        # deep: min(200 000, 150 000) - 1 000 = 149 000 tokens
        ("deep", 149_000 * 3, 1),
        ("deep", 149_000 * 3 + 1, 0),
    ],
)
def test_sd13_input_limit_applies_below_a_larger_model_window(
    engine: str, characters: int, calls: int
) -> None:
    primary = replace(PRIMARY, context_window=200_000, price=ModelPrice(Decimal(0), Decimal(0)))
    harness = Harness([completion("{}")], primary=primary, engine=engine, deadline_in_s=900)

    if calls:
        asyncio.run(_generate(harness, characters))
    else:
        with pytest.raises(LlmCallFailed) as caught:
            asyncio.run(_generate(harness, characters))
        assert caught.value.error_code is LlmErrorCode.CONTEXT_OVERFLOW

    assert len(harness.requests) == calls


@pytest.mark.parametrize(("spent", "calls"), [("0.497", 1), ("0.497001", 0)])
def test_run_cost_limit_boundary(spent: str, calls: int) -> None:
    # one call may cost 1 000 output tokens x $3/Mtok = $0.003 (the input is priced 0)
    primary = replace(PRIMARY, price=ModelPrice(Decimal(0), Decimal(3)))
    harness = Harness([valid()], primary=primary)
    asyncio.run(
        harness.ledger.record(
            harness.run, LlmUsage("eurouter", "m", "review", 1, 1, 0, Decimal(spent))
        )
    )

    if calls:
        harness.review()
    else:
        assert harness.failure().error_code is LlmErrorCode.BUDGET_EXCEEDED
    assert len(harness.requests) == calls


@pytest.mark.parametrize(("over", "calls"), [(Decimal(0), 1), (Decimal("0.000001"), 0)])
def test_run_cost_limit_boundary_at_the_oq2_primary_price(over: Decimal, calls: int) -> None:
    settings = LlmSettings.from_env(
        {
            "LLM_MODEL": "mistral-small-4",
            "LLM_API_KEYS": "sk-eu-1",
            "LLM_EUR_TO_USD_RATE": "1.20",
        }
    )
    primary = settings.primary
    # the gateway's own pre-call estimate: gateway.token_counter(primary) over both messages
    estimate = prompt_tokens(CONTEXT, HeuristicTokenCounter(primary.chars_per_token))
    next_cost = primary.price.cost_usd(tokens_in=estimate, tokens_out=primary.max_output_tokens)
    spent = settings.policy.run_cost_limit_usd["fast"] - next_cost + over
    harness = Harness([valid(model="mistral/mistral-small-4")], settings=settings)
    asyncio.run(
        harness.ledger.record(
            harness.run, LlmUsage("eurouter", "mistral-small-4", "review", 1, 1, 0, spent)
        )
    )

    if calls:
        harness.review()
    else:
        assert harness.failure().error_code is LlmErrorCode.BUDGET_EXCEEDED
    assert len(harness.requests) == calls


@pytest.mark.parametrize("base_url", [None, "HTTPS://API.EUROUTER.AI:443/api/v1/"])
def test_regolo_price_at_higher_fx_rejects_a_maximal_fast_call_before_request(
    base_url: str | None,
) -> None:
    settings = LlmSettings.from_env(
        {
            "LLM_MODEL": "mistral-small-4",
            "LLM_API_KEYS": "sk-eu-1",
            "LLM_EUR_TO_USD_RATE": "1.20",
            **({"LLM_BASE_URL": base_url} if base_url is not None else {}),
        }
    )
    harness = Harness([completion("{}")], settings=settings)
    asyncio.run(
        harness.ledger.record(
            harness.run,
            LlmUsage("eurouter", "mistral-small-4", "review", 1, 1, 0, Decimal("0.450000")),
        )
    )

    # Regolo: 52k × €0.50/M + 8k × €2.10/M = €0.0428; at 1.20 this is $0.05136.
    # The historical 1.1225 floor would admit the call ($0.450000 + $0.048043).
    with pytest.raises(LlmCallFailed) as caught:
        asyncio.run(_generate(harness, 52_000 * 3))

    assert caught.value.error_code is LlmErrorCode.BUDGET_EXCEEDED
    assert caught.value.calls == 0
    assert harness.requests == []


def test_review_adapter_fits_a_diff_larger_than_the_budget() -> None:
    primary = replace(PRIMARY, context_window=3_000, max_output_tokens=1_000)
    files = tuple(
        ChangedFile(
            path,
            "modified",
            tuple(DiffLine(number, "added", "x" * 40) for number in range(1, 61)),
        )
        for path in ("app/a.py", "app/b.py", "app/c.py")
    )
    harness = Harness([valid()], primary=primary)

    harness.review(replace(CONTEXT, changed_files=files))

    assert len(harness.requests) == 1
    user = harness.bodies()[0]["messages"][1]["content"]
    assert "<omitted_files>\napp/" in user
    assert "Use offset=" in user
    estimate = harness.trace.records[0][1].input_tokens_estimate
    assert estimate + primary.max_output_tokens <= 3_000


@pytest.mark.parametrize(
    "message",
    [
        "context_length_exceeded",
        "This model's context length is 8192 tokens",
        "prompt exceeds the context window",
        # only this marker matches: "context length" must not cover it
        "request exceeds the maximum context of 8192 tokens",
        "too many tokens in the request",
        "prompt is too long",
        "input is too long for the model",
    ],
)
def test_every_context_length_marker_is_an_overflow(message: str) -> None:
    harness = Harness([error(400, message)])

    assert harness.failure().error_code is LlmErrorCode.CONTEXT_OVERFLOW
    assert harness.kinds() == ["primary"]


def test_payload_too_large_is_an_overflow() -> None:
    harness = Harness([httpx.Response(413, text="Request Entity Too Large")])

    failure = harness.failure()

    assert failure.error_code is LlmErrorCode.CONTEXT_OVERFLOW
    assert harness.trace.records[0][1].response_json()["error"]["http_status"] == 413


def test_other_bad_request_goes_straight_to_the_fallback() -> None:
    harness = Harness(
        [error(400, "unsupported parameter: temperature"), valid(model="fallback-model-v6")]
    )

    result = harness.review()

    assert harness.kinds() == ["primary", "fallback"]
    assert harness.clock.sleeps == []
    assert result.model == "fallback-model-v6"


@pytest.mark.parametrize("value", ["-5", "inf", "nan", "soon"])
def test_unusable_retry_after_counts_as_missing(value: str) -> None:
    harness = Harness([error(429, **{"Retry-After": value}), valid()])

    harness.review()

    assert harness.kinds() == ["primary", "retry"]
    assert harness.clock.sleeps == [2.25]


def test_a_rate_limited_key_outranks_a_rejected_one() -> None:
    primary = replace(PRIMARY, api_keys=("sk-a", "sk-b"))
    harness = Harness([error(429, **{"Retry-After": "5"}), error(401), valid()], primary=primary)

    harness.review()

    assert harness.kinds() == ["primary", "retry"]
    assert harness.clock.sleeps == [5.0]
    first = harness.trace.records[0][1].response_json()["error"]
    assert (first["class"], first["http_status"]) == ("llm_rate_limited", 429)


def test_an_answer_without_usage_is_charged_by_the_estimate() -> None:
    body = {
        "model": "local",
        "choices": [{"message": {"content": json.dumps(VALID_OUTPUT)}, "finish_reason": "stop"}],
    }
    primary = replace(PRIMARY, price=ModelPrice(Decimal(1), Decimal(1)))
    harness = Harness([httpx.Response(200, json=body)], primary=primary)

    result = harness.review()

    (usage,) = result.usage
    assert usage.tokens_in == harness.trace.records[0][1].input_tokens_estimate > 0
    assert usage.tokens_out == -(-len(json.dumps(VALID_OUTPUT)) // 3)
    assert usage.cost_usd > 0


def test_a_failed_trace_write_does_not_discard_the_answer(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class BrokenTrace:
        async def record_call(self, run_id: UUID, record: object) -> None:
            raise RuntimeError("database is down")

    harness = Harness([valid()])
    harness.trace = BrokenTrace()  # type: ignore[assignment]

    with caplog.at_level(logging.ERROR):
        result = harness.review()

    assert result.output == VALID_OUTPUT
    assert "llm.call trace write failed" in caplog.text
    failed = next(r for r in caplog.records if r.getMessage() == "llm.call trace write failed")
    assert vars(failed)["run_id"] == str(RUN_ID)
    assert len(harness.ledger.events) == 1


# ---------- usage and trace ----------


@pytest.mark.parametrize(
    ("currency", "raw_cost", "rate", "expected_usd"),
    [
        ("USD", 0.1234567, "1.1204", Decimal("0.123457")),
        (None, 0.125, "1.1204", Decimal("0.125000")),
        ("EUR", 0.125, "1.1204", Decimal("0.140050")),
        ("EUR", 0.00000049, "2", Decimal("0.000001")),
    ],
)
def test_provider_cost_is_recorded_in_usd_once(
    currency: str | None, raw_cost: float, rate: str, expected_usd: Decimal
) -> None:
    settings = LlmSettings(primary=PRIMARY, fallback=None, eur_to_usd_rate=Decimal(rate))
    harness = Harness([valid(cost=raw_cost, cost_currency=currency)], settings=settings)

    result = harness.review()

    assert result.usage[0].cost_usd == expected_usd
    assert harness.ledger.events[0][1].cost_usd == expected_usd


def test_eur_fallback_cost_blocks_a_later_attempt_at_the_usd_budget() -> None:
    settings = LlmSettings(primary=PRIMARY, fallback=FALLBACK, eur_to_usd_rate=Decimal("1.1204"))
    harness = Harness([error(401), valid(cost=0.445, cost_currency="EUR")], settings=settings)

    first = harness.review()
    harness.attempt = 3
    failure = harness.failure()

    assert first.usage[0].cost_usd == Decimal("0.498578")
    assert harness.kinds() == ["primary", "fallback"]
    assert failure.error_code is LlmErrorCode.BUDGET_EXCEEDED
    assert failure.calls == 0
    assert len(harness.requests) == 2


def test_eur_cost_without_rate_records_paid_usage_and_raw_trace_then_fails() -> None:
    harness = Harness(
        [
            valid(cost=0.1, cost_currency="EUR", prompt_tokens=10_000, completion_tokens=200),
            valid(),
        ],
        settings=LlmSettings(primary=PRIMARY, fallback=FALLBACK),
    )

    failure = harness.failure()

    assert failure.error_code is LlmErrorCode.INVALID_OUTPUT
    assert not failure.run_retryable
    assert "LLM_EUR_TO_USD_RATE" in str(failure)
    assert failure.calls == 1
    assert len(harness.requests) == 1
    assert [(usage.tokens_in, usage.tokens_out, usage.cost_usd) for usage in failure.usage] == [
        (10_000, 200, Decimal("0.012000"))
    ]
    assert harness.ledger.events[0][1] == failure.usage[0]
    assert harness.trace.records[0][1].response_json()["usage"]["cost_currency"] == "EUR"


@pytest.mark.parametrize("metadata", ["unknown-currency", "invalid-cost"])
def test_bad_paid_cost_metadata_is_traced_and_charged_without_another_call(
    metadata: str,
) -> None:
    if metadata == "unknown-currency":
        reply = valid(
            cost=0.1,
            cost_currency="GBP",
            prompt_tokens=10_000,
            completion_tokens=200,
        )
    else:
        payload = valid(
            cost=0.1,
            cost_currency="USD",
            prompt_tokens=10_000,
            completion_tokens=200,
        ).json()
        payload["usage"]["cost"] = "invalid"
        reply = httpx.Response(200, json=payload)
    harness = Harness([reply, valid()])

    failure = harness.failure()

    assert failure.error_code is LlmErrorCode.INVALID_OUTPUT
    assert not failure.run_retryable
    assert failure.calls == 1
    assert len(harness.requests) == 1
    assert [(usage.tokens_in, usage.tokens_out, usage.cost_usd) for usage in failure.usage] == [
        (10_000, 200, Decimal("0.012000"))
    ]
    assert harness.ledger.events[0][1] == failure.usage[0]
    assert harness.trace.records[0][1].response_json()["usage"]["cost"] == (
        0.1 if metadata == "unknown-currency" else "invalid"
    )


def test_known_route_ceiling_prices_a_paid_answer_with_bad_currency() -> None:
    settings = LlmSettings.from_env(
        {
            "LLM_MODEL": "mistral-small-4",
            "LLM_API_KEYS": "sk-eu-1",
            "LLM_EUR_TO_USD_RATE": "1.20",
        }
    )
    harness = Harness(
        [
            valid(cost=0.1, cost_currency="GBP", prompt_tokens=52_000, completion_tokens=200),
            valid(),
        ],
        settings=settings,
    )

    failure = harness.failure()

    assert failure.error_code is LlmErrorCode.INVALID_OUTPUT
    assert failure.calls == 1
    assert len(harness.requests) == 1
    assert failure.usage[0].cost_usd == Decimal("0.051360")
    assert harness.ledger.events[0][1] == failure.usage[0]
    assert harness.trace.records[0][1].response_json()["usage"]["cost_currency"] == "GBP"


def test_fallback_route_ceiling_prices_a_paid_answer_with_bad_currency() -> None:
    settings = LlmSettings.from_env(
        {
            "LLM_MODEL": "mistral-small-4",
            "LLM_API_KEYS": "sk-eu-1",
            "LLM_FALLBACK_MODEL": "mistral-small-3.2-24b",
            "LLM_EUR_TO_USD_RATE": "1.20",
        }
    )
    harness = Harness(
        [
            error(401),
            valid(cost=0.1, cost_currency="GBP", prompt_tokens=52_000, completion_tokens=200),
            valid(),
        ],
        settings=settings,
    )

    failure = harness.failure()

    assert failure.error_code is LlmErrorCode.INVALID_OUTPUT
    assert failure.calls == 2
    assert len(harness.requests) == 2
    assert failure.usage[0].cost_usd == Decimal("0.016320")
    assert harness.ledger.events[0][1] == failure.usage[0]
    assert harness.trace.records[1][1].response_json()["usage"]["cost_currency"] == "GBP"


@pytest.mark.parametrize(
    ("currency", "cost", "rate"),
    [("USD", 1e30, None), ("EUR", 1e20, Decimal("1e20"))],
)
def test_unquantizable_paid_cost_records_estimate_and_raw_trace_then_fails(
    currency: str, cost: float, rate: Decimal | None
) -> None:
    harness = Harness(
        [
            valid(cost=cost, cost_currency=currency, prompt_tokens=10_000, completion_tokens=200),
            valid(),
        ],
        settings=LlmSettings(primary=PRIMARY, fallback=FALLBACK, eur_to_usd_rate=rate),
    )

    failure = harness.failure()

    assert failure.error_code is LlmErrorCode.INVALID_OUTPUT
    assert not failure.run_retryable
    assert failure.calls == 1
    assert len(harness.requests) == 1
    assert [(usage.tokens_in, usage.tokens_out, usage.cost_usd) for usage in failure.usage] == [
        (10_000, 200, Decimal("0.012000"))
    ]
    assert harness.ledger.events[0][1] == failure.usage[0]
    assert harness.trace.records[0][1].response_json()["usage"]["cost"] == cost


def test_usage_of_every_call_is_returned_and_recorded_with_the_actual_model() -> None:
    harness = Harness(
        [
            completion("oops", model="primary-2026", prompt_tokens=10_000, completion_tokens=500),
            valid(
                model="primary-2026",
                prompt_tokens=12_000,
                completion_tokens=800,
                cached_tokens=8_000,
                cost=0.0123,
            ),
        ],
        primary=replace(PRIMARY, price=ModelPrice(Decimal("2"), Decimal("8"), Decimal("0.5"))),
    )

    result = harness.review()

    expected = (
        LlmUsage("eurouter", "primary-2026", "review", 10_000, 500, 0, Decimal("0.024000")),
        LlmUsage("eurouter", "primary-2026", "review", 12_000, 800, 8_000, Decimal("0.012300")),
    )
    assert result.usage == expected
    assert tuple(usage for _, usage in harness.ledger.events) == expected
    assert {context.workspace_id for context, _ in harness.ledger.events} == {WORKSPACE_ID}


def test_every_call_is_traced_with_request_metadata_then_response_or_error() -> None:
    harness = Harness([error(503, "upstream down"), valid()])

    harness.review()

    first, second = (record for _, record in harness.trace.records)
    assert {run_id for run_id, _ in harness.trace.records} == {RUN_ID}
    request = first.request_json()
    assert set(request) == {
        "kind",
        "model",
        "call_no",
        "attempt",
        "timeout_s",
        "prompt_version_id",
        "rule_version_id",
        "input_tokens_estimate",
    }
    assert request["kind"] == "primary"
    assert request["call_no"] == 1
    assert request["attempt"] == 2
    assert request["timeout_s"] == 90.0
    assert request["prompt_version_id"] == str(PROMPT_VERSION_ID)
    assert request["rule_version_id"] == str(RULE_VERSION_ID)
    assert isinstance(request["input_tokens_estimate"], int)
    assert first.response_json() == {
        "error": {
            "class": "llm_unavailable",
            "http_status": 503,
            "message": "HTTP 503: upstream down",
        }
    }
    assert second.kind is LlmCallKind.RETRY
    assert second.request_json()["call_no"] == 2
    assert second.response_json()["model"] == "primary-model-2026-09-01"


def test_a_key_on_the_cut_boundary_of_an_error_text_leaves_no_prefix() -> None:
    secret = "sk-live-" + "k" * 30
    harness = Harness(
        [error(401, "x" * 290 + secret)],
        primary=replace(PRIMARY, api_keys=(secret,)),
        fallback=None,
    )

    failure = harness.failure()

    message = harness.trace.records[0][1].response_json()["error"]["message"]
    assert "sk-live-" not in message
    assert "sk-live-" not in str(failure)


def test_keys_never_reach_logs_exceptions_or_the_trace(caplog: pytest.LogCaptureFixture) -> None:
    secret = "sk-live-very-secret"
    primary = replace(PRIMARY, api_keys=(secret, "sk-live-second"))

    def echo_key(request: httpx.Request) -> httpx.Response:
        key = request.headers.get("Authorization", "").removeprefix("Bearer ")
        status = 401 if key == secret else 500
        return error(status, f"api key {key} rejected")

    def refuse_with_key(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"connect with {request.headers['Authorization']}")

    harness = Harness(
        [echo_key, echo_key, refuse_with_key, echo_key, echo_key, echo_key, echo_key],
        primary=primary,
        fallback=replace(FALLBACK, api_keys=(secret,)),
    )

    with caplog.at_level(logging.DEBUG):
        failure = harness.failure()

    texts = [
        caplog.text,
        # structured fields passed through ``extra=`` are not part of caplog.text
        *(repr(vars(record)) for record in caplog.records),
        str(failure),
        repr(failure),
        repr(primary),
        json.dumps([record.response_json() for _, record in harness.trace.records]),
        json.dumps([record.request_json() for _, record in harness.trace.records]),
    ]
    for text in texts:
        assert secret not in text
        assert "sk-live-second" not in text


# ---------- conventions call through the gateway ----------


def test_conventions_usage_counts_toward_the_run_cost_limit_of_the_review() -> None:
    conventions_answer = {
        "files": [{"path": "app/service.py", "relevance": "changed service"}],
        "key_patterns": ["a", "b", "c"],
        "recommendations": [f"Check {index} (from: standard/correctness)" for index in range(5)],
    }
    harness = Harness([completion(json.dumps(conventions_answer), cost=0.499)])

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(harness._handle)) as client:
            gateway = harness.gateway(client)
            draft = await GatewayConventionsModel(gateway, harness.run).draft_conventions(
                request=ConventionsRequest(
                    system="CONVENTIONS PROMPT",
                    rules=(),
                    agents_md="# Agents",
                    repo_tree=("app/service.py", "tests/test_service.py"),
                    repo_files=(RepositoryFile("app/service.py", 10, "import x\nrun()\n"),),
                    languages={"Python": 100},
                    changed_files=("app/service.py",),
                )
            )
            assert draft == conventions_answer
            with pytest.raises(LlmCallFailed) as caught:
                await GatewayReviewModel(gateway, harness.run, _NoMeta()).draft_review(
                    context=CONTEXT
                )
            assert caught.value.error_code is LlmErrorCode.BUDGET_EXCEEDED

    asyncio.run(scenario())

    assert len(harness.requests) == 1
    body = harness.bodies()[0]
    assert body["response_format"]["json_schema"]["name"] == "RepoConventionsDraft"
    user = body["messages"][1]["content"]
    assert "<repo_tree>\napp/service.py\ntests/test_service.py\n</repo_tree>" in user
    assert '<file path="app/service.py">\n<line n="1">import x</line>' in user
    assert [usage.operation for _, usage in harness.ledger.events] == ["conventions"]


def test_conventions_answer_for_other_paths_goes_through_repair_to_the_fallback() -> None:
    wrong = {
        "files": [{"path": "other.py", "relevance": "x"}],
        "key_patterns": ["a", "b", "c"],
        "recommendations": [f"Check {index} (from: standard/security)" for index in range(5)],
    }
    right = dict(wrong, files=[{"path": "app/service.py", "relevance": "x"}])
    harness = Harness(
        [
            completion(json.dumps(wrong)),
            completion(json.dumps({"files": 1})),
            completion(json.dumps(right)),
        ]
    )

    async def scenario() -> object:
        async with httpx.AsyncClient(transport=httpx.MockTransport(harness._handle)) as client:
            return await GatewayConventionsModel(
                harness.gateway(client), harness.run
            ).draft_conventions(
                request=ConventionsRequest("P", (), None, (), (), {}, ("app/service.py",))
            )

    assert asyncio.run(scenario()) == right
    assert harness.kinds() == ["primary", "repair", "fallback"]
    assert "must list every path" in harness.bodies()[1]["messages"][-1]["content"]


def test_conventions_tree_over_the_budget_is_cut_with_a_marker() -> None:
    tree = tuple(f"src/pkg{index}/module_{index}.py" for index in range(8_000))
    harness = Harness([completion(json.dumps(_conventions_answer()))])

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(harness._handle)) as client:
            await GatewayConventionsModel(harness.gateway(client), harness.run).draft_conventions(
                request=ConventionsRequest("P", (), None, tree, (), {}, ("app/service.py",))
            )

    asyncio.run(scenario())

    assert len(harness.requests) == 1
    user = harness.bodies()[0]["messages"][1]["content"]
    marker = re.search(r"\[(\d+) more paths omitted\]\n</repo_tree>", user)
    assert marker is not None
    kept = user.split("<repo_tree>\n")[1].split("\n[")[0].splitlines()
    assert kept and kept[0] == "src/pkg0/module_0.py"
    assert len(kept) + int(marker.group(1)) == 8_000
    assert "<changed_files>\napp/service.py\n</changed_files>" in user
    estimate = harness.trace.records[0][1].input_tokens_estimate
    assert estimate + PRIMARY.max_output_tokens <= 60_000


def test_conventions_for_more_than_one_hundred_changed_paths_are_valid() -> None:
    changed = tuple(f"src/module_{index:03}.py" for index in range(101))
    answer = dict(
        _conventions_answer(),
        files=[{"path": path, "relevance": "changed module"} for path in changed[:100]],
    )
    harness = Harness([completion(json.dumps(answer))])

    async def scenario() -> object:
        async with httpx.AsyncClient(transport=httpx.MockTransport(harness._handle)) as client:
            return await GatewayConventionsModel(
                harness.gateway(client), harness.run
            ).draft_conventions(request=ConventionsRequest("P", (), None, (), (), {}, changed))

    assert asyncio.run(scenario()) == answer
    assert harness.kinds() == ["primary"]
    user = harness.bodies()[0]["messages"][1]["content"]
    assert "src/module_099.py\n[1 more changed paths omitted]\n</changed_files>" in user
    schema = harness.bodies()[0]["response_format"]["json_schema"]["schema"]
    assert schema["properties"]["files"]["maxItems"] == 100


# ---------- end to end ----------


def test_sample_diff_goes_through_prompt_builder_and_gateway_into_a_review_output() -> None:
    sample = (REPO_ROOT / "review" / "examples" / "findings.sample.json").read_text()
    diff = (REPO_ROOT / "review" / "examples" / "sample.diff").read_text()
    system = (REPO_ROOT / "review" / "prompts" / "review.system.v2.md").read_text()
    harness = Harness([completion(sample)])
    case = ReviewCase(diff=diff, system=system, pr_meta=CONTEXT.pr_meta)

    async def scenario() -> ReviewOutput:
        async with httpx.AsyncClient(transport=httpx.MockTransport(harness._handle)) as client:
            result = await review_case(
                case,
                LlmSettings(primary=PRIMARY, fallback=FALLBACK),
                transport=OpenAICompatibleTransport(client),
            )
        assert result.provider == "eurouter"
        assert result.model == "primary-model-2026-09-01"
        assert [call.kind for call in result.calls] == [LlmCallKind.PRIMARY]
        assert result.usage[0].tokens_in == 1200
        return result.output

    output = asyncio.run(scenario())

    assert isinstance(output, ReviewOutput)
    assert output == ReviewOutput.model_validate_json(sample)
    user = harness.bodies()[0]["messages"][1]["content"]
    for file in parse_unified_diff(diff):
        assert f'<file path="{file.path}"' in user
    assert harness.bodies()[0]["messages"][0]["content"] == system
