"""LLM gateway policy on a fake HTTP transport (docs/PIPELINE_SPEC.md §3, §4.5, §5.1, §6).

No network and no keys: every provider answer comes from ``httpx.MockTransport``.
"""

from __future__ import annotations

import asyncio
import json
import logging
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
from app.modules.reviews.application.prompt_builder import (
    ChangedFile,
    DiffLine,
    PullRequestMeta,
    RepoConventions,
    ReviewContext,
    parse_unified_diff,
)
from app.modules.reviews.application.review_output import ReviewOutput, parse_review_output
from app.modules.reviews.infrastructure.llm.gateway import GatewayResult, LlmGateway
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
from app.modules.reviews.infrastructure.llm.transport import OpenAICompatibleTransport

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
            attempt=2,
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
            "LLM_MODEL": "gpt-4.1-mini",
            "LLM_API_KEYS": "sk-eu-1, sk-eu-2",
            "LLM_FALLBACK_MODEL": "mistral-small-4",
        }
    )
    harness = Harness([valid(model="openai/gpt-4.1-mini-2025-04-14")], settings=settings)

    result = harness.review()

    request = harness.requests[0]
    assert str(request.url) == "https://api.eurouter.ai/api/v1/chat/completions"
    assert request.headers["Authorization"] == "Bearer sk-eu-1"
    body = harness.bodies()[0]
    assert body["model"] == "gpt-4.1-mini"
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
    assert result.model == "openai/gpt-4.1-mini-2025-04-14"
    assert settings.fallback is not None
    assert settings.fallback.api_keys == ("sk-eu-1", "sk-eu-2")
    assert settings.fallback.base_url == "https://api.eurouter.ai/api/v1"


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


@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({}, "LLM_MODEL must be set"),
        ({"LLM_MODEL": "unknown"}, "LLM_BASE_URL must be set"),
        (
            {"LLM_MODEL": "unknown", "LLM_BASE_URL": "http://llm.test/v1"},
            "LLM_CONTEXT_WINDOW must be set",
        ),
        ({"LLM_MODEL": "gpt-4.1-mini", "LLM_CONTEXT_WINDOW": "big"}, "must be an integer"),
        ({"LLM_MODEL": "gpt-4.1-mini", "LLM_PRICE_INPUT_PER_MTOK": "x"}, "must be a decimal"),
        ({"LLM_MODEL": "gpt-4.1-mini", "LLM_STRUCTURED_OUTPUT": "tools"}, "json_schema or"),
        ({"LLM_MODEL": "gpt-4.1-mini", "LLM_EXTRA_BODY": "[1]"}, "JSON object"),
        ({"LLM_MODEL": "gpt-4.1-mini", "LLM_EXTRA_BODY": "{"}, "JSON object"),
    ],
)
def test_incomplete_configuration_names_the_variable(env: dict[str, str], message: str) -> None:
    with pytest.raises(LlmConfigError, match=message):
        LlmSettings.from_env(env)


def test_extra_body_and_overrides_reach_the_request() -> None:
    settings = LlmSettings.from_env(
        {
            "LLM_MODEL": "gpt-4.1-mini",
            "LLM_API_KEYS": "sk-eu-1",
            "LLM_MAX_OUTPUT_TOKENS": "4000",
            "LLM_PRICE_INPUT_PER_MTOK": "0.5",
            "LLM_EXTRA_BODY": '{"provider": {"allow_fallbacks": false}}',
        }
    )
    harness = Harness([valid()], settings=settings)

    harness.review()

    body = harness.bodies()[0]
    assert body["provider"] == {"allow_fallbacks": False}
    assert body["max_tokens"] == 4000
    assert settings.primary.price.input_per_mtok == Decimal("0.5")


# ---------- key rotation ----------


@pytest.mark.parametrize("status", [401, 403, 429])
def test_rejected_key_rotates_to_the_next_key_without_counting_a_call(status: int) -> None:
    primary = replace(PRIMARY, api_keys=("sk-a", "sk-b"))
    harness = Harness([error(status), valid(), valid()], primary=primary)

    async def two_reviews() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(harness._handle)) as client:
            model = GatewayReviewModel(harness.gateway(client), harness.run, _NoMeta())
            await model.draft_review(context=CONTEXT)
            await model.draft_review(context=CONTEXT)

    asyncio.run(two_reviews())

    assert [request.headers["Authorization"] for request in harness.requests] == [
        "Bearer sk-a",
        "Bearer sk-b",
        "Bearer sk-b",
    ]
    assert harness.kinds() == ["primary", "primary"]
    assert [record.call_no for _, record in harness.trace.records] == [1, 1]


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


@pytest.mark.parametrize("retry_after", ["60", None])
def test_rate_limit_with_a_long_or_missing_retry_after_falls_back_at_once(
    retry_after: str | None,
) -> None:
    headers = {} if retry_after is None else {"Retry-After": retry_after}
    harness = Harness([error(429, **headers), valid(model="fallback-model-v2")])

    result = harness.review()

    assert harness.kinds() == ["primary", "fallback"]
    assert harness.clock.sleeps == []
    assert result.model == "fallback-model-v2"


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
        completion(json.dumps(VALID_OUTPUT)[:40], finish_reason="length"),
        completion(None),
    ],
)
def test_cut_or_empty_answer_is_invalid(reply: httpx.Response) -> None:
    harness = Harness([reply, valid()])

    harness.review()

    assert harness.kinds() == ["primary", "repair"]


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


def test_no_retry_when_the_backoff_leaves_less_than_the_call_timeout() -> None:
    harness = Harness([timeout(), valid()], deadline_in_s=92)

    failure = harness.failure()

    assert failure.error_code is LlmErrorCode.DEADLINE_EXCEEDED
    assert harness.kinds() == ["primary"]
    assert harness.clock.sleeps == [2.25]


# ---------- usage and trace ----------


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


def test_conventions_answer_for_other_paths_is_repaired() -> None:
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
