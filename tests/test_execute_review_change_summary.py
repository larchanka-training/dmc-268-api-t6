"""The run calls the change summary agent after its mandatory stages (#316 Part 3).

``ExecuteReviewRun`` on the snapshot path, its verifier and the real ``StoreReviewOutput``:
the agent's text reaches the stored body signed, a failed call leaves it out and the run
still publishes, and ``review.postprocess`` says which happened.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any, cast
from uuid import UUID

import pytest

from app.modules.reviews.application.change_summary import (
    ChangeSummary,
    ChangeSummaryRequest,
)
from app.modules.reviews.application.execute_review import ExecuteReviewRun, ReviewPromptInput
from app.modules.reviews.application.llm import LlmCallFailed, LlmErrorCode
from app.modules.reviews.application.policy_errors import PolicyError, PolicyErrorCode
from app.modules.reviews.application.reflection_pass import (
    ReflectionPass,
    VerificationUnavailableError,
    VerifierReply,
)
from app.modules.reviews.application.review_context import NoReviewableText, SummaryOnlyReview
from app.modules.reviews.application.run_failures import RunCancelled, RunFailure, RunSkipped
from app.modules.reviews.application.store_review_output import StoreReviewOutput
from tests.llm_fakes import SupportingVerifier
from tests.verifier_pipeline_fakes import (
    MAIN_BLOCK_ID,
    RUN_ID,
    WORKER_ID,
    FakeModel,
    FakeProcessor,
    FakePublishingRepository,
    FakeQueue,
    FakeRepository,
    FakeUnitOfWork,
    NullSuggestionParser,
)

SIGNED = "**What changed** (автоописание, не замечание): "
FINDING = {
    "path": "app/main.py",
    "line": 10,
    "start_line": None,
    "severity": "high",
    "category": "security",
    "title": "Unchecked input",
    "body": "The value reaches the query unchecked.",
    "suggestion": None,
    "confidence": 0.9,
    "rule_name": None,
    "evidence_refs": [{"block_id": MAIN_BLOCK_ID, "start_line": 10, "end_line": 10}],
}


class _FailingVerifier:
    async def verify(self, **_: object) -> VerifierReply:
        raise RunFailure("verification_unavailable", "the verifier failed")


class _ChangeSummaryModel:
    """Answers ``answer``; notes how many prompts the run's verifier had got at each call."""

    def __init__(self, answer: ChangeSummary | Exception) -> None:
        self._answer = answer
        self.verifier = SupportingVerifier(MAIN_BLOCK_ID, 10)
        self.requests: list[ChangeSummaryRequest] = []
        self.verifier_prompts_before: list[int] = []

    async def summarize_change(self, *, request: ChangeSummaryRequest) -> ChangeSummary:
        self.requests.append(request)
        self.verifier_prompts_before.append(len(self.verifier.prompts))
        if isinstance(self._answer, Exception):
            raise self._answer
        return self._answer


class _LegacyRepository(FakeRepository):
    async def get_review_prompt_input(self, run_id: UUID, saved: Any) -> ReviewPromptInput:
        prompt = await super().get_review_prompt_input(run_id, saved)
        return replace(prompt, policy_snapshot=None)


class _NoTextProcessor(FakeProcessor):
    def __init__(self, error: Exception) -> None:
        self._error = error

    async def prepare(self, run_id: UUID) -> Any:
        raise self._error


def _pipeline(
    agent: _ChangeSummaryModel,
    *,
    findings: list[dict[str, Any]] | None = None,
    processor: FakeProcessor | None = None,
    repository: FakeRepository | None = None,
    verifier: Any = None,
) -> tuple[ExecuteReviewRun, FakePublishingRepository]:
    reviews = FakePublishingRepository()
    store = StoreReviewOutput(
        cast(Any, lambda: FakeUnitOfWork(reviews)),
        FakeQueue(),
        worker_id=WORKER_ID,
        run_url=lambda _: None,
        parser=NullSuggestionParser(),
    )
    pipeline = ExecuteReviewRun(
        processor or FakeProcessor(),
        repository or FakeRepository(),
        FakeModel([FINDING] if findings is None else findings),
        store,
        reflection_pass=ReflectionPass(verifier or agent.verifier),
        change_summary=agent,
    )
    return pipeline, reviews


def _run(agent: _ChangeSummaryModel, **kwargs: Any) -> FakePublishingRepository:
    pipeline, reviews = _pipeline(agent, **kwargs)
    assert asyncio.run(pipeline.execute(RUN_ID)) is True
    return reviews


def test_change_summary_follows_the_verifier_and_its_text_is_signed_in_the_stored_body() -> None:
    agent = _ChangeSummaryModel(
        ChangeSummary(("Adds a lookup by id.", "Reads the value from the query."))
    )

    reviews = _run(agent)

    assert agent.verifier_prompts_before == [1]
    [request] = agent.requests
    assert request.title == "Title"
    assert [file.path for file in request.changed_files] == ["app/main.py"]
    [processed] = reviews.stored
    assert f"{SIGNED}Adds a lookup by id. Reads the value from the query." in (
        processed.review_body
    )
    assert [item.finding.title for item in processed.inline + processed.body_only] == [
        "Unchecked input"
    ]
    [entered] = reviews.entered
    assert entered["postprocess_response"]["change_summary"] == {
        "status": "shown",
        "received": 2,
        "shown": 2,
    }
    assert entered["postprocess_response"]["verdict"] == "blocking"


def test_change_summary_follows_the_review_when_it_has_no_candidate() -> None:
    agent = _ChangeSummaryModel(ChangeSummary(("Adds a lookup by id.",)))

    reviews = _run(agent, findings=[])

    assert agent.verifier_prompts_before == [0]
    assert agent.verifier.prompts == []
    [processed] = reviews.stored
    assert f"{SIGNED}Adds a lookup by id." in processed.review_body
    [entered] = reviews.entered
    assert entered["postprocess_response"]["change_summary"] == {
        "status": "shown",
        "received": 1,
        "shown": 1,
    }


@pytest.mark.parametrize(
    ("sentences", "trace", "kept"),
    [
        (
            (f"Adds {'a' * 1500}.",),
            {"status": "no_room", "received": 1, "shown": 0},
            None,
        ),
        (
            (f"Adds {'a' * 700}.", f"Reads {'b' * 700}.", f"Moves {'c' * 700}."),
            {"status": "shown", "received": 3, "shown": 1},
            f"{SIGNED}Adds {'a' * 700}.",
        ),
    ],
    ids=["no-room", "first-sentence-only"],
)
def test_change_summary_text_over_the_block_limit_is_cut_and_traced(
    sentences: tuple[str, ...], trace: dict[str, object], kept: str | None
) -> None:
    # The adapter caps a sentence at 200 characters; longer ones fill the 1 500 characters of
    # the block here without a long checklist (the post-processor tests show that one).
    reviews = _run(_ChangeSummaryModel(ChangeSummary(sentences)))

    [processed] = reviews.stored
    [entered] = reviews.entered
    assert entered["postprocess_response"]["change_summary"] == trace
    if kept is None:
        assert "**What changed**" not in processed.review_body
    else:
        assert f"{kept}\n" in processed.review_body
        assert sentences[1] not in processed.review_body


@pytest.mark.parametrize(
    ("error", "error_code"),
    [
        (LlmCallFailed(LlmErrorCode.UNAVAILABLE, "no call slot left"), "llm_unavailable"),
        (LlmCallFailed(LlmErrorCode.BUDGET_EXCEEDED, "run cost limit"), "budget_exceeded"),
        (LlmCallFailed(LlmErrorCode.TIMEOUT, "timed out"), "llm_timeout"),
        (LlmCallFailed(LlmErrorCode.INVALID_OUTPUT, "two sentences"), "llm_invalid_output"),
        (
            LlmCallFailed(LlmErrorCode.VERIFICATION_DEADLINE_EXCEEDED, "10 s are kept"),
            "verification_deadline_exceeded",
        ),
        (
            PolicyError(PolicyErrorCode.SNAPSHOT_INTEGRITY_ERROR, "prompt misses its pin"),
            "policy_snapshot_integrity_error",
        ),
    ],
    ids=["no-slot", "no-budget", "timeout", "invalid", "no-time", "pin"],
)
def test_change_summary_failure_publishes_the_review_without_the_text(
    error: Exception, error_code: str
) -> None:
    with_text = _run(_ChangeSummaryModel(ChangeSummary(("Adds a lookup by id.",))))
    agent = _ChangeSummaryModel(error)

    reviews = _run(agent)

    assert agent.verifier_prompts_before == [1]
    [processed] = reviews.stored
    assert "**What changed**" not in processed.review_body
    [entered] = reviews.entered
    assert entered["postprocess_response"]["change_summary"] == {
        "status": "failed",
        "error_code": error_code,
    }
    [reference] = with_text.entered
    for key in ("verdict", "findings_hash", "severity_counts"):
        assert entered["postprocess_response"][key] == reference["postprocess_response"][key]


@pytest.mark.parametrize(
    "error",
    [
        RunCancelled("run_inactive"),
        PolicyError(PolicyErrorCode.CONTEXT_VIOLATION, "a block outside its context"),
    ],
    ids=["cancelled", "other-policy-error"],
)
def test_change_summary_cancellation_and_other_errors_still_end_the_attempt(
    error: Exception,
) -> None:
    agent = _ChangeSummaryModel(error)
    pipeline, reviews = _pipeline(agent)

    with pytest.raises(type(error)):
        asyncio.run(pipeline.execute(RUN_ID))

    assert agent.verifier_prompts_before == [1]
    assert reviews.stored == []


@pytest.mark.parametrize(
    ("processor", "repository"),
    [
        (_NoTextProcessor(SummaryOnlyReview()), None),
        (_NoTextProcessor(NoReviewableText()), None),
        (None, _LegacyRepository()),
    ],
    ids=["summary-only", "coverage-none", "legacy"],
)
def test_change_summary_is_not_called_without_a_verified_snapshot_review(
    processor: FakeProcessor | None, repository: FakeRepository | None
) -> None:
    agent = _ChangeSummaryModel(ChangeSummary(("Adds a lookup by id.",)))

    reviews = _run(agent, findings=[], processor=processor, repository=repository)

    assert agent.requests == []
    [processed] = reviews.stored
    assert "**What changed**" not in processed.review_body
    [entered] = reviews.entered
    assert "change_summary" not in entered["postprocess_response"]


@pytest.mark.parametrize(
    ("processor", "verifier", "error"),
    [
        (_NoTextProcessor(RunSkipped("no_changes")), None, RunSkipped),
        (None, _FailingVerifier(), VerificationUnavailableError),
    ],
    ids=["skipped", "verifier-failed"],
)
def test_change_summary_is_not_called_when_the_run_is_skipped_or_fails(
    processor: FakeProcessor | None, verifier: _FailingVerifier | None, error: type[Exception]
) -> None:
    agent = _ChangeSummaryModel(ChangeSummary(("Adds a lookup by id.",)))
    pipeline, _ = _pipeline(agent, processor=processor, verifier=verifier)

    with pytest.raises(error):
        asyncio.run(pipeline.execute(RUN_ID))

    assert agent.requests == []
