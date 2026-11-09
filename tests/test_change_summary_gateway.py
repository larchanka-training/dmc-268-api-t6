"""The change summary model on top of the gateway (#316 Part 2).

``GatewayChangeSummaryModel`` sends ``review.summary`` v1 through ``LlmGateway`` on the
review route under its own ``change_summary`` operation, with the strict
``ChangeSummaryOutput`` schema and the bounded repair. No network: provider answers come
from ``httpx.MockTransport``.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import httpx
import pytest

from app.modules.reviews.application.change_summary import (
    ChangeSummary,
    ChangeSummaryRequest,
    change_summary_system_prompt,
)
from app.modules.reviews.application.llm import LlmCallFailed, LlmErrorCode, RunCallContext
from app.modules.reviews.application.prompt_builder import ChangedFile, DiffLine
from app.modules.reviews.infrastructure.llm.gateway import HeuristicTokenCounter
from app.modules.reviews.infrastructure.llm.models import GatewayChangeSummaryModel
from app.modules.reviews.infrastructure.llm.settings import LlmSettings, ModelPrice, ModelProfile
from tests.llm_fakes import FakeClock, ScriptedProvider, completion

NOW = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)
RUN = RunCallContext(
    run_id=UUID("00000000-0000-0000-0000-000000031601"),
    workspace_id=UUID("00000000-0000-0000-0000-000000031602"),
    attempt=1,
    engine="fast",
    deadline=NOW + timedelta(seconds=480),
    # The worker's run carries the review prompt; a change summary call must never record it.
    prompt_version_id=UUID("00000000-0000-0000-0000-000000031603"),
)
REVIEW = ModelProfile(
    provider="router",
    base_url="https://router.test/v1",
    model="review-model",
    context_window=100_000,
    max_output_tokens=1_000,
    price=ModelPrice(Decimal("1"), Decimal("2")),
    api_keys=("sk-test-1",),
)
SETTINGS = LlmSettings(primary=REVIEW, fallback=replace(REVIEW, model="review-fallback-model"))
PROMPT = (Path(__file__).resolve().parents[1] / "review/prompts/review.summary.v1.md").read_text(
    encoding="utf-8"
)
(EXAMPLE_ANSWER,) = re.findall(r"```json\n(.*?)\n```", PROMPT, re.DOTALL)
REQUEST = ChangeSummaryRequest(
    title="Clean up old runs",
    changed_files=(
        ChangedFile(
            path="app/jobs/retention.py",
            status="added",
            lines=(DiffLine(1, "added", "async def purge_stale_runs() -> int:"),),
        ),
    ),
)


class _Provider(ScriptedProvider):
    def summarize(
        self, request: ChangeSummaryRequest = REQUEST, *, settings: LlmSettings = SETTINGS
    ) -> ChangeSummary:
        async def exercise() -> ChangeSummary:
            async with httpx.AsyncClient(transport=httpx.MockTransport(self.handle)) as client:
                gateway = self.gateway(settings, client, FakeClock(NOW))
                return await GatewayChangeSummaryModel(gateway, RUN).summarize_change(
                    request=request
                )

        return asyncio.run(exercise())

    def roles(self, index: int) -> list[str]:
        return [message["role"] for message in self.bodies()[index]["messages"]]


def test_change_summary_example_answer_passes_the_gateway_parse_path() -> None:
    provider = _Provider([completion(EXAMPLE_ANSWER)])

    summary = provider.summarize()

    assert summary == ChangeSummary(
        sentences=(
            "Adds a purge_stale_runs job that deletes stored run diffs older than a given time"
            " and returns how many it removed.",
            "Schedules the job in the worker once a day.",
        )
    )
    assert provider.models() == ["review-model"]
    assert provider.kinds() == ["primary"]
    messages = provider.bodies()[0]["messages"]
    assert messages[0] == {"role": "system", "content": change_summary_system_prompt()}
    assert messages[1]["content"].startswith("<pr_title>Clean up old runs</pr_title>\n")
    response_format = provider.bodies()[0]["response_format"]
    assert response_format["json_schema"]["name"] == "ChangeSummaryOutput"
    schema = response_format["json_schema"]["schema"]
    assert "$schema" not in schema and "$id" not in schema
    assert schema["required"] == ["sentences"]
    assert schema["additionalProperties"] is False
    # The schema file's pattern with its final end anchor (?![\s\S]) rewritten to "$" (#130).
    assert schema["properties"]["sentences"]["items"]["pattern"] == (
        r"^[^\S\r\n]*(?:(?:[^\s.!?]+|\S*[.!?][^\s.!?]*[A-Za-z0-9][^\s.!?]*)[^\S\r\n]+)*"
        r"\S+[^\S\r\n]*$"
    )


def test_change_summary_call_writes_its_own_operation_without_the_review_prompt() -> None:
    provider = _Provider([completion(EXAMPLE_ANSWER)])

    provider.summarize()

    assert [usage.operation for _, usage in provider.ledger.events] == ["change_summary"]
    assert [record.operation for _, record in provider.trace.records] == ["change_summary"]
    assert [record.prompt_version_id for _, record in provider.trace.records] == [None]


def test_change_summary_answers_outside_the_schema_fail_with_llm_invalid_output() -> None:
    extra_field = json.dumps({"sentences": ["Adds a job."], "risk": "low"})
    provider = _Provider([completion(extra_field)] * 3)

    with pytest.raises(LlmCallFailed) as caught:
        provider.summarize()

    assert caught.value.error_code is LlmErrorCode.INVALID_OUTPUT
    assert caught.value.error_code.value == "llm_invalid_output"
    assert provider.kinds() == ["primary", "repair", "fallback"]
    assert provider.models() == ["review-model", "review-model", "review-fallback-model"]
    # The bounded repair: the previous answer goes back as data, never as an assistant turn.
    assert provider.roles(1) == ["system", "user", "user"]
    assert [usage.operation for _, usage in provider.ledger.events] == ["change_summary"] * 3


@pytest.mark.parametrize(
    ("context_window", "budget"),
    [(100_000, 16_000), (10_000, 9_000)],
    ids=["own-budget", "route-window"],
)
def test_change_summary_input_over_its_budget_is_cut_and_the_call_succeeds(
    context_window: int, budget: int
) -> None:
    profile = replace(REVIEW, context_window=context_window)
    settings = LlmSettings(primary=profile, fallback=replace(profile, model="fallback"))
    lines = tuple(DiffLine(n, "added", "y" * 80) for n in range(1, 2_001))
    request = ChangeSummaryRequest(
        title="A large change",
        changed_files=tuple(
            ChangedFile(path=f"app/m{index}.py", status="modified", lines=lines)
            for index in range(3)
        ),
    )
    provider = _Provider([completion(EXAMPLE_ANSWER)])

    provider.summarize(request, settings=settings)

    counter = HeuristicTokenCounter(profile.chars_per_token)
    sent = provider.bodies()[0]["messages"]
    assert budget - 200 < sum(counter.count(message["content"]) for message in sent) <= budget
    assert "of 2000 total." in sent[1]["content"]
    assert sent[1]["content"].endswith('<files_not_shown count="2" />')
