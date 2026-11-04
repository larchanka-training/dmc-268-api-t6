"""Public contract tests for strict model review output parsing."""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from types import TracebackType
from typing import Any, cast
from uuid import UUID

import pytest
from pydantic import ValidationError

from app.modules.reviews.application.findings_post_processor import ProcessedReviewOutput
from app.modules.reviews.application.review_output import (
    FindingPostProcessingInput,
    InvalidReviewOutput,
    PublishReviewOutput,
    ReviewOutput,
    ReviewPublication,
    comment_body,
    parse_review_output,
    parse_review_output_v2,
)
from tests.review_output_fakes import (
    RUN_ID,
    RecordingFactory,
    RecordingProvider,
    RecordingRepository,
)
from tests.test_findings_post_processor import _added_file_diff_map


def valid_output() -> dict[str, object]:
    return {
        "findings": [
            {
                "path": "app/example.py",
                "line": 12,
                "start_line": 10,
                "severity": "high",
                "category": "correctness",
                "title": "Missing transaction boundary",
                "body": "The provider call can leave the transaction open.",
                "suggestion": None,
                "confidence": 0.9,
                "rule_name": None,
            }
        ],
        "summary": {
            "problem": "A transaction can span a provider call.",
            "done_well": "The repository adapter is small.",
            "effort": "small",
        },
    }


def test_review_output_accepts_the_exact_contract() -> None:
    output = ReviewOutput.model_validate(valid_output())

    assert output.findings[0].line == 12
    assert output.summary.effort == "small"


def test_review_output_accepts_a_body_over_the_prompt_word_target() -> None:
    """The schema enforces the character ceiling, not prompt-level brevity."""
    data = valid_output()
    data["findings"][0]["body"] = " ".join(["word"] * 121)  # type: ignore[index]

    output = ReviewOutput.model_validate(data)

    assert len(output.findings[0].body.split()) == 121


def test_review_output_rejects_a_body_over_the_character_ceiling() -> None:
    data = valid_output()
    data["findings"][0]["body"] = "x" * 1201  # type: ignore[index]

    with pytest.raises(ValidationError):
        ReviewOutput.model_validate(data)


@pytest.mark.parametrize("start_line", [12, 13], ids=["equal", "after"])
def test_parser_rejects_non_strict_multiline_anchor_start(start_line: int) -> None:
    data = valid_output()
    data["findings"][0]["start_line"] = start_line  # type: ignore[index]

    with pytest.raises(InvalidReviewOutput):
        parse_review_output(data)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value.update(unexpected=True),
        lambda value: value["findings"][0].update(extra="no"),
        lambda value: value["findings"][0].update(line=0),
        lambda value: value["findings"][0].update(severity="urgent"),
        lambda value: value["findings"].extend([value["findings"][0]] * 10),
        lambda value: value["findings"][0].update(title="x" * 81),
        lambda value: value["findings"][0].update(title="Ends with a period."),
        lambda value: value["findings"][0].update(title="Two\nlines"),
        lambda value: value["findings"][0].update(title="Two\u2028lines"),
        lambda value: value["summary"].update(effort="tiny"),
        lambda value: value["summary"].update(problem="First issue. Second issue."),
        lambda value: value["summary"].update(done_well="One good thing. Another. A third."),
    ],
)
def test_review_output_rejects_invalid_contract(mutate: object) -> None:
    data = valid_output()
    assert callable(mutate)
    mutate(data)

    with pytest.raises(ValidationError):
        ReviewOutput.model_validate(data)


def _publication() -> ReviewPublication:
    return ReviewPublication("a" * 40, (), "review body", "stable-key")


def _context() -> FindingPostProcessingInput:
    return FindingPostProcessingInput(
        diff_map=_added_file_diff_map("app/example.py", 14),
        checked_paths=frozenset({"app/example.py"}),
        rule_names=frozenset(),
    )


@pytest.mark.parametrize("encoded", ("str", "bytes"))
def test_publish_valid_json_text_preserves_the_raw_object(encoded: str) -> None:
    repository = RecordingRepository(_context(), _publication())
    factory = RecordingFactory(repository)
    provider = RecordingProvider(units=factory.units)
    value = valid_output()
    cast(list[dict[str, object]], value["findings"])[0]["confidence"] = 1
    text = json.dumps(value, indent=1)
    raw: str | bytes = text if encoded == "str" else text.encode("utf-8")

    assert asyncio.run(PublishReviewOutput(factory, provider).execute(RUN_ID, raw)) is True

    assert repository.raw_objects == [value]
    stored_finding = cast(list[dict[str, object]], repository.raw_objects[0]["findings"])[0]
    assert type(stored_finding["confidence"]) is int
    assert stored_finding["confidence"] == 1
    assert repository.marked == 1
    assert [unit.commits for unit in factory.units] == [0, 1, 1]
    assert provider.calls == [("a" * 40, "review body", (), "stable-key")]


def test_publish_mapping_preserves_the_raw_object() -> None:
    repository = RecordingRepository(_context(), _publication())
    factory = RecordingFactory(repository)
    provider = RecordingProvider(units=factory.units)
    value = valid_output()

    assert asyncio.run(PublishReviewOutput(factory, provider).execute(RUN_ID, value))

    assert repository.raw_objects == [value]
    assert provider.calls[0][0] == "a" * 40


@pytest.mark.parametrize("raw", ('{"findings":', b'{"findings":'))
def test_parse_rejects_malformed_json_text(raw: str | bytes) -> None:
    with pytest.raises(InvalidReviewOutput):
        parse_review_output(raw)


def test_publish_without_durable_context_returns_false() -> None:
    repository = RecordingRepository(None, _publication())
    factory = RecordingFactory(repository)
    provider = RecordingProvider()

    assert (
        asyncio.run(PublishReviewOutput(factory, provider).execute(RUN_ID, valid_output())) is False
    )

    assert repository.raw_objects == []
    assert provider.calls == []
    assert [unit.commits for unit in factory.units] == [0]


def test_publish_without_a_storable_result_returns_false() -> None:
    repository = RecordingRepository(_context(), None)
    factory = RecordingFactory(repository)
    provider = RecordingProvider()

    assert (
        asyncio.run(PublishReviewOutput(factory, provider).execute(RUN_ID, valid_output())) is False
    )

    assert repository.raw_objects == [valid_output()]
    assert provider.calls == []
    assert [unit.commits for unit in factory.units] == [0, 0]


def test_review_output_rejects_out_of_order_findings() -> None:
    data = valid_output()
    finding = cast(list[dict[str, object]], data["findings"])[0]
    data["findings"] = [finding, {**finding, "severity": "critical"}]

    with pytest.raises(ValidationError, match="severity then confidence ordered"):
        ReviewOutput.model_validate(data)


def test_review_output_rejects_title_with_trailing_newline() -> None:
    data = valid_output()
    cast(list[dict[str, object]], data["findings"])[0]["title"] = "Trailing\n"

    with pytest.raises(ValidationError, match="title must be one line"):
        ReviewOutput.model_validate(data)


def test_comment_body_formatting() -> None:
    assert comment_body("Title", "Body", None) == "**Title**\n\nBody"
    assert (
        comment_body("Title", "Body", "suggestion_code")
        == "**Title**\n\nBody\n\n```suggestion\nsuggestion_code\n```"
    )


def test_publish_snapshot_run_preserves_v2_rule_attribution() -> None:
    context = FindingPostProcessingInput(
        diff_map=_added_file_diff_map("app/example.py", 14),
        checked_paths=frozenset({"app/example.py"}),
        rule_names=frozenset(),
        allowed_v2_rules=frozenset({".review/rules.md"}),
    )
    published_processed: list[ProcessedReviewOutput] = []

    @dataclass
    class CustomRecordingRepository(RecordingRepository):
        async def store_review_output(
            self,
            run_id: UUID,
            model_output: dict[str, object],
            parsed: ReviewOutput,
            processed: ProcessedReviewOutput,
        ) -> ReviewPublication | None:
            published_processed.append(processed)
            return await super().store_review_output(run_id, model_output, parsed, processed)

    repository = CustomRecordingRepository(context, _publication())
    factory = RecordingFactory(repository)
    provider = RecordingProvider(units=factory.units)
    value = valid_output()
    cast(list[dict[str, object]], value["findings"])[0]["rule_name"] = ".review/rules.md"

    assert asyncio.run(PublishReviewOutput(factory, provider).execute(RUN_ID, value)) is True
    assert len(published_processed) == 1
    assert published_processed[0].inline[0].finding.rule_name == ".review/rules.md"


def test_store_review_output_preserves_allowed_v2_rules() -> None:
    from app.modules.reviews.application.store_review_output import (
        PublishingGuard,
        StoreReviewOutput,
    )

    stored_processed: list[ProcessedReviewOutput] = []

    class FakeReviewRepo:
        async def get_post_processing_input(self, run_id: UUID) -> FindingPostProcessingInput:
            return FindingPostProcessingInput(
                diff_map=_added_file_diff_map("app/example.py", 14),
                checked_paths=frozenset({"app/example.py"}),
                rule_names=frozenset(),
                allowed_v2_rules=frozenset({".review/rules.md"}),
            )

        async def lock_for_publishing(self, run_id: UUID) -> PublishingGuard:
            return PublishingGuard(
                state="running",
                worker_id="test-worker",
                cancel_requested=False,
                head_sha="head_sha_123",
                head_current=True,
                repository_review_event="APPROVE",
            )

        async def store_review_output(
            self,
            run_id: UUID,
            answer: dict[str, Any],
            parsed: ReviewOutput,
            processed: ProcessedReviewOutput,
        ) -> ReviewPublication:
            stored_processed.append(processed)
            return ReviewPublication(
                head_sha="head_sha_123",
                findings=(),
                review_body="body",
            )

        async def enter_publishing(self, run_id: UUID, **kwargs: Any) -> None:
            pass

    class FakeUoW:
        def __init__(self) -> None:
            self.reviews = FakeReviewRepo()

        async def __aenter__(self) -> FakeUoW:
            return self

        async def __aexit__(
            self,
            exc_type: type[BaseException] | None,
            exc: BaseException | None,
            tb: TracebackType | None,
        ) -> None:
            pass

        async def commit(self) -> None:
            pass

        async def rollback(self) -> None:
            pass

    class FakeQueue:
        def __init__(self) -> None:
            self.published: list[Any] = []

        async def publish_review(self, pointer: Any) -> None:
            self.published.append(pointer)

    queue = FakeQueue()
    store = StoreReviewOutput(
        cast(Any, lambda: FakeUoW()), queue, worker_id="test-worker", run_url=lambda _: None
    )
    value = valid_output()
    cast(list[dict[str, object]], value["findings"])[0]["rule_name"] = ".review/rules.md"

    success = asyncio.run(store.execute(RUN_ID, value))
    assert success is True
    assert len(stored_processed) == 1
    assert stored_processed[0].inline[0].finding.rule_name == ".review/rules.md"
    assert len(queue.published) == 1


@pytest.mark.parametrize(
    "fail_at_publication_start, claimed_attempt",
    [(False, 1), (True, 1), (False, 2)],
    ids=["publish", "fail-before-t8", "stale-claim"],
)
def test_store_review_output_commits_accepted_output_before_t8(
    fail_at_publication_start: bool,
    claimed_attempt: int,
) -> None:
    from app.modules.reviews.application.store_review_output import (
        FinalizedReviewPlan,
        PublishingGuard,
        StoreReviewOutput,
    )

    committed_state = "running"
    committed_outputs = 0
    committed_finalized = 0
    committed_plan: FinalizedReviewPlan | None = None
    commits: list[tuple[str, int]] = []
    accepted_output = valid_output()
    cast(list[dict[str, object]], accepted_output["findings"])[0]["evidence_refs"] = [
        {"block_id": "b1", "start_line": 10, "end_line": 12}
    ]

    class FakeUoW:
        def __init__(self) -> None:
            self.reviews = self
            self.pending_state = committed_state
            self.pending_outputs = 0
            self.pending_finalized = 0
            self.pending_plan: FinalizedReviewPlan | None = None

        async def __aenter__(self) -> FakeUoW:
            return self

        async def __aexit__(
            self,
            exc_type: type[BaseException] | None,
            exc: BaseException | None,
            tb: TracebackType | None,
        ) -> None:
            await self.rollback()

        async def commit(self) -> None:
            nonlocal committed_state, committed_outputs, committed_finalized, committed_plan
            committed_state = self.pending_state
            committed_outputs += self.pending_outputs
            committed_finalized += self.pending_finalized
            if self.pending_plan is not None:
                committed_plan = self.pending_plan
            commits.append((committed_state, committed_outputs))

        async def rollback(self) -> None:
            self.pending_state = committed_state
            self.pending_outputs = 0
            self.pending_finalized = 0
            self.pending_plan = None

        async def get_post_processing_input(self, run_id: UUID) -> FindingPostProcessingInput:
            return FindingPostProcessingInput(
                diff_map=_added_file_diff_map("app/example.py", 14),
                checked_paths=frozenset({"app/example.py"}),
                rule_names=frozenset(),
            )

        async def lock_for_publishing(self, run_id: UUID) -> PublishingGuard:
            return PublishingGuard(
                state=committed_state,
                worker_id="test-worker",
                cancel_requested=False,
                head_sha="head_sha_123",
                head_current=True,
                repository_review_event="COMMENT",
                attempt=claimed_attempt,
            )

        async def store_review_output(
            self,
            run_id: UUID,
            answer: dict[str, Any],
            parsed: ReviewOutput,
            processed: ProcessedReviewOutput,
        ) -> ReviewPublication:
            assert committed_outputs == 1
            assert committed_finalized == 1
            assert parsed.summary == processed.summary
            assert [item.finding.title for item in processed.inline] == [
                "Missing transaction boundary"
            ]
            return ReviewPublication(
                head_sha="head_sha_123",
                findings=(),
                review_body=processed.review_body,
            )

        async def checkpoint_review_candidates(self, run_id: UUID, **kwargs: Any) -> None:
            assert run_id == RUN_ID
            assert kwargs["model_output"] == parse_review_output_v2(accepted_output).model_dump(
                mode="json"
            )
            self.pending_outputs += 1

        async def finalize_review(self, run_id: UUID, **kwargs: Any) -> None:
            assert run_id == RUN_ID
            assert kwargs["result"]["findings"][0]["title"] == "Missing transaction boundary"
            self.pending_finalized += 1
            self.pending_plan = FinalizedReviewPlan(
                "head_sha_123", kwargs["findings_hash"], kwargs["review_event"]
            )

        async def load_finalized_plan(self, run_id: UUID) -> FinalizedReviewPlan | None:
            return committed_plan

        async def enter_publishing(self, run_id: UUID, **kwargs: Any) -> None:
            if fail_at_publication_start:
                raise RuntimeError("publication transition failed")
            self.pending_state = "publishing"

    class FakeQueue:
        async def publish_review(self, pointer: Any) -> None:
            assert (committed_state, committed_outputs) == ("publishing", 1)

    store = StoreReviewOutput(
        cast(Any, FakeUoW),
        FakeQueue(),
        worker_id="test-worker",
        expected_attempt=1,
        run_url=lambda _: None,
    )

    async def scenario() -> bool:
        checkpointed = await store.checkpoint_candidates(
            RUN_ID,
            accepted_output,
            ("candidate-1",),
            candidates=[cast(list[dict[str, object]], accepted_output["findings"])[0]],
            dropped=[],
            accepted_call_id=None,
        )
        if claimed_attempt != 1:
            return checkpointed
        assert checkpointed
        return await store.execute(RUN_ID, valid_output())

    if claimed_attempt != 1:
        assert asyncio.run(scenario()) is False
        assert commits == []
        assert committed_outputs == 0
    elif fail_at_publication_start:
        with pytest.raises(RuntimeError, match="publication transition failed"):
            asyncio.run(scenario())
        assert commits == [("running", 1), ("running", 1)]
        assert (committed_state, committed_outputs) == ("running", 1)
        assert committed_finalized == 1
    else:
        assert asyncio.run(scenario()) is True
        assert commits == [("running", 1), ("running", 1), ("publishing", 1)]


def test_store_review_output_persists_dropped_findings_from_reflection_pass() -> None:
    from app.modules.reviews.application.store_review_output import (
        PublishingGuard,
        StoreReviewOutput,
    )

    stored_processed: list[ProcessedReviewOutput] = []
    entered_publishing: list[dict[str, Any]] = []

    class FakeReviewRepo:
        async def get_post_processing_input(self, run_id: UUID) -> FindingPostProcessingInput:
            return FindingPostProcessingInput(
                diff_map=_added_file_diff_map("app/example.py", 14),
                checked_paths=frozenset({"app/example.py"}),
                rule_names=frozenset(),
                allowed_v2_rules=frozenset({".review/rules.md"}),
            )

        async def lock_for_publishing(self, run_id: UUID) -> PublishingGuard:
            return PublishingGuard(
                state="running",
                worker_id="test-worker",
                cancel_requested=False,
                head_sha="head_sha_123",
                head_current=True,
                repository_review_event="APPROVE",
            )

        async def store_review_output(
            self,
            run_id: UUID,
            answer: dict[str, Any],
            parsed: ReviewOutput,
            processed: ProcessedReviewOutput,
        ) -> ReviewPublication:
            # _dropped_findings must be popped from answer to not pollute the raw answer object
            assert "_dropped_findings" not in answer
            stored_processed.append(processed)
            return ReviewPublication(
                head_sha="head_sha_123",
                findings=(),
                review_body="body",
            )

        async def enter_publishing(self, run_id: UUID, **kwargs: Any) -> None:
            entered_publishing.append(kwargs)

    class FakeUoW:
        def __init__(self) -> None:
            self.reviews = FakeReviewRepo()

        async def __aenter__(self) -> FakeUoW:
            return self

        async def __aexit__(
            self,
            exc_type: type[BaseException] | None,
            exc: BaseException | None,
            tb: TracebackType | None,
        ) -> None:
            pass

        async def commit(self) -> None:
            pass

        async def rollback(self) -> None:
            pass

    class FakeQueue:
        def __init__(self) -> None:
            self.published: list[Any] = []

        async def publish_review(self, pointer: Any) -> None:
            self.published.append(pointer)

    from app.modules.reviews.application.review_output import (
        DroppedCandidateRecord,
        VerifiedReviewOutput,
    )

    queue = FakeQueue()
    store = StoreReviewOutput(
        cast(Any, lambda: FakeUoW()), queue, worker_id="test-worker", run_url=lambda _: None
    )
    value = VerifiedReviewOutput(
        valid_output(),
        dropped_findings=(
            DroppedCandidateRecord(
                finding={
                    "path": "app/example.py",
                    "line": 15,
                    "start_line": None,
                    "severity": "medium",
                    "category": "performance",
                    "title": "Slow query",
                    "body": "N+1 query detected",
                    "suggestion": None,
                    "confidence": 0.85,
                    "rule_name": None,
                },
                drop_reason="severity_threshold",
                position=1,
            ),
        ),
        severity_threshold="critical",
    )

    success = asyncio.run(store.execute(RUN_ID, value))
    assert success is True
    assert len(stored_processed) == 1
    # Check that processed.dropped contains the dropped finding and overview count
    assert len(stored_processed[0].dropped) == 1
    assert "Below severity threshold (critical)" in stored_processed[0].review_body
    dropped_item = stored_processed[0].dropped[0]
    assert dropped_item.finding.title == "Slow query"
    assert dropped_item.drop_reason == "severity_threshold"
    assert dropped_item.bucket == "dropped"
    assert dropped_item.position == 1

    # Check postprocess_response logged in enter_publishing contains dropped item
    assert len(entered_publishing) == 1
    dropped_log = entered_publishing[0]["postprocess_response"]["dropped"]
    assert dropped_log == [{"index": 1, "drop_reason": "severity_threshold"}]
    assert len(queue.published) == 1


def test_store_review_output_appends_overflow_footer_with_run_url() -> None:
    from app.modules.reviews.application.store_review_output import (
        PublishingGuard,
        StoreReviewOutput,
    )

    stored_processed: list[ProcessedReviewOutput] = []
    entered_publishing: list[dict[str, Any]] = []
    run_url = "https://ui.test/runs/00000000-0000-0000-0000-000000000408"

    class FakeReviewRepo:
        async def get_post_processing_input(self, run_id: UUID) -> FindingPostProcessingInput:
            return FindingPostProcessingInput(
                diff_map=_added_file_diff_map("app/example.py", 14),
                checked_paths=frozenset({"app/example.py"}),
                rule_names=frozenset(),
                allowed_v2_rules=frozenset(),
            )

        async def lock_for_publishing(self, run_id: UUID) -> PublishingGuard:
            return PublishingGuard(
                state="running",
                worker_id="test-worker",
                cancel_requested=False,
                head_sha="head_sha_123",
                head_current=True,
                repository_review_event="APPROVE",
            )

        async def store_review_output(
            self,
            run_id: UUID,
            answer: dict[str, Any],
            parsed: ReviewOutput,
            processed: ProcessedReviewOutput,
        ) -> ReviewPublication:
            stored_processed.append(processed)
            return ReviewPublication(
                head_sha="head_sha_123",
                findings=(),
                review_body=processed.review_body,
            )

        async def enter_publishing(self, run_id: UUID, **kwargs: Any) -> None:
            entered_publishing.append(kwargs)

    class FakeUoW:
        def __init__(self) -> None:
            self.reviews = FakeReviewRepo()

        async def __aenter__(self) -> FakeUoW:
            return self

        async def __aexit__(
            self,
            exc_type: type[BaseException] | None,
            exc: BaseException | None,
            tb: TracebackType | None,
        ) -> None:
            pass

        async def commit(self) -> None:
            pass

        async def rollback(self) -> None:
            pass

    class FakeQueue:
        async def publish_review(self, pointer: Any) -> None:
            pass

    notes = [
        {
            "path": "app/example.py",
            "line": 12,
            "start_line": None,
            "severity": "low",
            "category": "correctness",
            "title": f"Overflow {number}",
            "body": "x" * 1200,
            "suggestion": None,
            "confidence": 0.4,
            "rule_name": None,
        }
        for number in range(1, 7)
    ]
    payload: dict[str, object] = {
        "findings": [
            {
                "path": "app/example.py",
                "line": 12,
                "start_line": None,
                "severity": "high",
                "category": "correctness",
                "title": "Blocking defect",
                "body": "The transaction boundary is missing.",
                "suggestion": None,
                "confidence": 0.9,
                "rule_name": None,
            },
            *notes,
        ],
        "summary": {
            "problem": "A required value is not validated.",
            "done_well": "The endpoint remains small.",
            "effort": "small",
        },
    }

    from app.modules.reviews.application.review_output import (
        DroppedCandidateRecord,
        VerifiedReviewOutput,
    )

    store = StoreReviewOutput(
        cast(Any, lambda: FakeUoW()),
        FakeQueue(),
        worker_id="test-worker",
        run_url=lambda _: run_url,
    )
    verified = VerifiedReviewOutput(
        payload,
        dropped_findings=(
            DroppedCandidateRecord(
                finding={
                    "path": "app/example.py",
                    "line": 12,
                    "start_line": None,
                    "severity": "medium",
                    "category": "correctness",
                    "title": "Below the verifier threshold",
                    "body": "Kept only to label the overview line.",
                    "suggestion": None,
                    "confidence": 0.5,
                    "rule_name": None,
                },
                drop_reason="severity_threshold",
                position=0,
            ),
        ),
        severity_threshold="critical",
    )
    assert asyncio.run(store.execute(RUN_ID, verified)) is True
    assert len(stored_processed) == 1
    body = stored_processed[0].review_body
    assert body.endswith(run_url)
    assert "Below severity threshold (critical)" in body
    assert "Below severity threshold (all)" not in body
    assert re.search(r"\*\*Ещё \d+ наход(?:ка|ки|ок):\*\*", body) is not None
    assert len(body) <= 4000
    response = entered_publishing[0]["postprocess_response"]
    assert response["omitted_body_only_count"] == stored_processed[0].omitted_body_only_count
    assert response["omitted_body_only_count"] > 0


def test_store_review_output_repacks_body_before_overflow_footer() -> None:
    from app.modules.reviews.application.store_review_output import (
        PublishingGuard,
        StoreReviewOutput,
    )

    stored_processed: list[ProcessedReviewOutput] = []

    class FakeReviewRepo:
        async def get_post_processing_input(self, run_id: UUID) -> FindingPostProcessingInput:
            return FindingPostProcessingInput(
                diff_map=_added_file_diff_map("app/example.py", 14),
                checked_paths=frozenset({"app/example.py"}),
                rule_names=frozenset(),
                allowed_v2_rules=frozenset(),
            )

        async def lock_for_publishing(self, run_id: UUID) -> PublishingGuard:
            return PublishingGuard(
                state="running",
                worker_id="test-worker",
                cancel_requested=False,
                head_sha="head_sha_123",
                head_current=True,
                repository_review_event="APPROVE",
            )

        async def store_review_output(
            self,
            run_id: UUID,
            answer: dict[str, Any],
            parsed: ReviewOutput,
            processed: ProcessedReviewOutput,
        ) -> ReviewPublication:
            stored_processed.append(processed)
            return ReviewPublication(
                head_sha="head_sha_123",
                findings=(),
                review_body=processed.review_body,
            )

        async def enter_publishing(self, run_id: UUID, **kwargs: Any) -> None:
            pass

    class FakeUoW:
        def __init__(self) -> None:
            self.reviews = FakeReviewRepo()

        async def __aenter__(self) -> FakeUoW:
            return self

        async def __aexit__(
            self,
            exc_type: type[BaseException] | None,
            exc: BaseException | None,
            tb: TracebackType | None,
        ) -> None:
            pass

        async def commit(self) -> None:
            pass

        async def rollback(self) -> None:
            pass

    run_url = "https://ui.test/runs/00000000-0000-0000-0000-000000000408"
    notes = [
        {
            "path": "app/example.py",
            "line": 12,
            "start_line": None,
            "severity": "low",
            "category": "correctness",
            "title": f"Overflow {number}",
            "body": "x" * (1173 + (number % 3)),
            "suggestion": None,
            "confidence": 0.4,
            "rule_name": None,
        }
        for number in range(1, 7)
    ]
    payload: dict[str, object] = {
        "findings": [
            {
                "path": "app/example.py",
                "line": 12,
                "start_line": None,
                "severity": "high",
                "category": "correctness",
                "title": "Blocking defect",
                "body": "The transaction boundary is missing.",
                "suggestion": None,
                "confidence": 0.9,
                "rule_name": None,
            },
            *notes,
        ],
        "summary": {
            "problem": "A required value is not validated.",
            "done_well": "The endpoint remains small.",
            "effort": "small",
        },
    }

    class FakeQueue:
        async def publish_review(self, pointer: Any) -> None:
            pass

    from app.modules.reviews.application.review_output import (
        DroppedCandidateRecord,
        VerifiedReviewOutput,
    )

    store = StoreReviewOutput(
        cast(Any, lambda: FakeUoW()),
        FakeQueue(),
        worker_id="test-worker",
        run_url=lambda _: run_url,
    )
    verified = VerifiedReviewOutput(
        payload,
        dropped_findings=(
            DroppedCandidateRecord(
                finding={
                    "path": "app/example.py",
                    "line": 12,
                    "start_line": None,
                    "severity": "medium",
                    "category": "correctness",
                    "title": "Below the verifier threshold",
                    "body": "Kept only to label the overview line.",
                    "suggestion": None,
                    "confidence": 0.5,
                    "rule_name": None,
                },
                drop_reason="severity_threshold",
                position=0,
            ),
        ),
        severity_threshold="critical",
    )
    assert asyncio.run(store.execute(RUN_ID, verified)) is True
    assert len(stored_processed) == 1
    body = stored_processed[0].review_body
    assert stored_processed[0].omitted_body_only_count > 0
    assert len(body) <= 4000
    assert run_url in body
    assert stored_processed[0].review_body_budget_degraded is False
    assert "Below severity threshold (critical)" in body
    assert "Below severity threshold (all)" not in body


def test_publish_review_output_repacks_overflow_with_verifier_threshold() -> None:
    """The inline publisher repacks under the verifier threshold and still writes the footer."""
    from app.modules.reviews.application.review_output import (
        DroppedCandidateRecord,
        VerifiedReviewOutput,
    )

    stored_processed: list[ProcessedReviewOutput] = []

    class RecordingReviews:
        async def get_post_processing_input(self, run_id: UUID) -> FindingPostProcessingInput:
            assert run_id == RUN_ID
            return FindingPostProcessingInput(
                diff_map=_added_file_diff_map("app/example.py", 14),
                checked_paths=frozenset({"app/example.py"}),
                rule_names=frozenset(),
                severity_threshold="all",
            )

        async def store_review_output(
            self,
            run_id: UUID,
            model_output: dict[str, object],
            parsed: ReviewOutput,
            processed: ProcessedReviewOutput,
        ) -> ReviewPublication:
            assert run_id == RUN_ID
            stored_processed.append(processed)
            return ReviewPublication(
                "a" * 40,
                (),
                processed.review_body,
                "stable-key",
            )

        async def mark_review_published(self, run_id: UUID) -> None:
            assert run_id == RUN_ID

    repository = RecordingReviews()
    factory = RecordingFactory(cast(Any, repository))
    provider = RecordingProvider()
    notes = [
        {
            "path": "app/example.py",
            "line": 12,
            "start_line": None,
            "severity": "low",
            "category": "correctness",
            "title": f"Overflow {number}",
            "body": "x" * (1173 + (number % 3)),
            "suggestion": None,
            "confidence": 0.4,
            "rule_name": None,
        }
        for number in range(1, 7)
    ]
    payload: dict[str, object] = {
        "findings": [
            {
                "path": "app/example.py",
                "line": 12,
                "start_line": None,
                "severity": "high",
                "category": "correctness",
                "title": "Blocking defect",
                "body": "The transaction boundary is missing.",
                "suggestion": None,
                "confidence": 0.9,
                "rule_name": None,
            },
            *notes,
        ],
        "summary": {
            "problem": "A required value is not validated at this time.",
            "done_well": "The endpoint remains small.",
            "effort": "small",
        },
    }
    verified = VerifiedReviewOutput(
        payload,
        dropped_findings=(
            DroppedCandidateRecord(
                finding={
                    "path": "app/example.py",
                    "line": 12,
                    "start_line": None,
                    "severity": "medium",
                    "category": "correctness",
                    "title": "Below the verifier threshold",
                    "body": "Kept only to label the overview line.",
                    "suggestion": None,
                    "confidence": 0.5,
                    "rule_name": None,
                },
                drop_reason="severity_threshold",
                position=0,
            ),
        ),
        severity_threshold="critical",
    )

    assert asyncio.run(PublishReviewOutput(factory, provider).execute(RUN_ID, verified)) is True

    assert len(stored_processed) == 1
    body = stored_processed[0].review_body
    assert stored_processed[0].omitted_body_only_count > 0
    assert len(body) <= 4000
    assert stored_processed[0].review_body_budget_degraded is False
    assert re.search(r"\*\*Ещё \d+ наход(?:ка|ки|ок)\*\*", body) is not None
    assert "http" not in body
    assert "Below severity threshold (critical)" in body
    assert "Below severity threshold (all)" not in body
    assert provider.calls[0][1] == body


def test_store_review_output_marks_degraded_body_in_postprocess_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A summary too long for the budget degrades the body and writes the flag (#170)."""
    from app.modules.reviews.application.store_review_output import (
        PublishingGuard,
        StoreReviewOutput,
    )

    entered: list[dict[str, Any]] = []
    stored_processed: list[ProcessedReviewOutput] = []

    class FakeReviewRepo:
        async def get_post_processing_input(self, run_id: UUID) -> FindingPostProcessingInput:
            return FindingPostProcessingInput(
                diff_map=_added_file_diff_map("app/example.py", 14),
                checked_paths=frozenset({"app/example.py"}),
                rule_names=frozenset(),
                allowed_v2_rules=frozenset(),
            )

        async def lock_for_publishing(self, run_id: UUID) -> PublishingGuard:
            return PublishingGuard(
                state="running",
                worker_id="test-worker",
                cancel_requested=False,
                head_sha="head_sha_123",
                head_current=True,
                repository_review_event="APPROVE",
            )

        async def store_review_output(
            self,
            run_id: UUID,
            answer: dict[str, Any],
            parsed: ReviewOutput,
            processed: ProcessedReviewOutput,
        ) -> ReviewPublication:
            stored_processed.append(processed)
            return ReviewPublication(
                head_sha="head_sha_123",
                findings=(),
                review_body=processed.review_body,
            )

        async def enter_publishing(self, run_id: UUID, **kwargs: Any) -> None:
            entered.append(kwargs)

    class FakeUoW:
        def __init__(self) -> None:
            self.reviews = FakeReviewRepo()

        async def __aenter__(self) -> FakeUoW:
            return self

        async def __aexit__(
            self,
            exc_type: type[BaseException] | None,
            exc: BaseException | None,
            tb: TracebackType | None,
        ) -> None:
            pass

        async def commit(self) -> None:
            pass

        async def rollback(self) -> None:
            pass

    class FakeQueue:
        async def publish_review(self, pointer: Any) -> None:
            pass

    run_url = "https://ui.test/runs/00000000-0000-0000-0000-000000000408"
    payload: dict[str, object] = {
        "findings": [
            {
                "path": "app/example.py",
                "line": 12,
                "start_line": None,
                "severity": "high",
                "category": "correctness",
                "title": "Blocking defect",
                "body": "The transaction boundary is missing.",
                "suggestion": None,
                "confidence": 0.9,
                "rule_name": None,
            },
            {
                "path": "app/example.py",
                "line": 12,
                "start_line": None,
                "severity": "low",
                "category": "correctness",
                "title": "Overflow note",
                "body": "x" * 1200,
                "suggestion": None,
                "confidence": 0.4,
                "rule_name": None,
            },
        ],
        "summary": {
            "problem": "p" * 3500 + ".",
            "done_well": "d" * 500 + ".",
            "effort": "small",
        },
    }
    # Backend prose is authoritative after #213; inject a long backend summary
    # to retain the #170 degraded-body boundary test without trusting model prose.
    long_backend_summary = ReviewOutput.model_validate(payload).summary
    monkeypatch.setattr(
        "app.modules.reviews.application.findings_post_processor.summary_from_result",
        lambda *_args, **_kwargs: long_backend_summary,
    )
    store = StoreReviewOutput(
        cast(Any, lambda: FakeUoW()),
        FakeQueue(),
        worker_id="test-worker",
        run_url=lambda _: run_url,
    )

    assert asyncio.run(store.execute(RUN_ID, payload)) is True

    response = entered[0]["postprocess_response"]
    assert response["review_body_budget_degraded"] is True
    assert response["omitted_body_only_count"] == 1
    assert len(stored_processed[0].review_body) <= 4000
    assert stored_processed[0].review_body.endswith(run_url)


def test_store_review_output_persists_dropped_findings_coercion_tail() -> None:
    from app.modules.reviews.application.review_output import (
        DroppedCandidateRecord,
        EvidenceRef,
        ReviewFindingV2,
        VerifiedReviewOutput,
        coerce_dropped_findings,
    )
    from app.modules.reviews.application.store_review_output import (
        PublishingGuard,
        StoreReviewOutput,
    )

    class FakeReviewRepo:
        async def get_post_processing_input(self, run_id: UUID) -> FindingPostProcessingInput:
            return FindingPostProcessingInput(
                diff_map=_added_file_diff_map("app/example.py", 14),
                checked_paths=frozenset({"app/example.py"}),
                rule_names=frozenset(),
                allowed_v2_rules=frozenset(),
            )

        async def lock_for_publishing(self, run_id: UUID) -> PublishingGuard:
            return PublishingGuard(
                state="running",
                worker_id="test-worker",
                cancel_requested=False,
                head_sha="head_sha_123",
                head_current=True,
                repository_review_event="APPROVE",
            )

        async def store_review_output(
            self,
            run_id: UUID,
            answer: dict[str, Any],
            parsed: ReviewOutput,
            processed: ProcessedReviewOutput,
        ) -> ReviewPublication:
            return ReviewPublication(head_sha="head_sha_123", findings=(), review_body="body")

        async def enter_publishing(self, run_id: UUID, **kwargs: Any) -> None:
            pass

    class FakeUoW:
        def __init__(self) -> None:
            self.reviews = FakeReviewRepo()

        async def __aenter__(self) -> FakeUoW:
            return self

        async def __aexit__(
            self,
            exc_type: type[BaseException] | None,
            exc: BaseException | None,
            tb: TracebackType | None,
        ) -> None:
            pass

        async def commit(self) -> None:
            pass

        async def rollback(self) -> None:
            pass

    class FakeQueue:
        async def publish_review(self, pointer: Any) -> None:
            pass

    store = StoreReviewOutput(
        cast(Any, lambda: FakeUoW()), FakeQueue(), worker_id="test-worker", run_url=lambda _: None
    )

    # Corrupted drop record through store.execute raises InvalidReviewOutput with run_id
    corrupted_store_value = VerifiedReviewOutput(
        valid_output(),
        dropped_findings=[{"finding": {"bad": 1}, "drop_reason": "x", "position": 0}],
    )
    with pytest.raises(InvalidReviewOutput) as exc_info:
        asyncio.run(store.execute(RUN_ID, corrupted_store_value))
    assert str(RUN_ID) in str(exc_info.value)

    # Malformed dropped_findings fail closed with InvalidReviewOutput and include run_id
    for bad_dropped in (
        "not-a-list",
        ["not-a-dict"],
        [{"finding": "not-a-dict", "drop_reason": "x", "position": 0}],
        [{"finding": {}, "drop_reason": "", "position": 0}],
        [{"finding": {"bad": 1}, "drop_reason": "x", "position": 0}],
    ):
        with pytest.raises(InvalidReviewOutput) as exc_info:
            coerce_dropped_findings(RUN_ID, bad_dropped)
        assert str(RUN_ID) in str(exc_info.value)

    assert coerce_dropped_findings(RUN_ID, None) == ()

    # Also test coerce_dropped_findings with ReviewFinding and ReviewFindingV2 instances
    rf_v1 = parse_review_output(valid_output()).findings[0]
    rf_v2 = ReviewFindingV2(
        path="app/example.py",
        line=15,
        start_line=None,
        severity="medium",
        category="performance",
        title="Slow query",
        body="N+1 query detected",
        suggestion=None,
        confidence=0.85,
        rule_name=None,
        evidence_refs=[EvidenceRef(block_id="b1", start_line=15, end_line=15)],
    )
    coerced = coerce_dropped_findings(
        RUN_ID,
        [
            DroppedCandidateRecord(finding=rf_v1, drop_reason="duplicate", position=0),
            DroppedCandidateRecord(finding=rf_v2, drop_reason="contradicted", position=1),
        ],
    )
    assert len(coerced) == 2
    assert coerced[0].drop_reason == "duplicate"
    assert coerced[1].drop_reason == "contradicted"


def test_publish_review_output_persists_dropped_findings() -> None:
    from app.modules.reviews.application.review_output import (
        DroppedCandidateRecord,
        VerifiedReviewOutput,
    )

    stored_processed: list[ProcessedReviewOutput] = []

    class Repo(RecordingRepository):
        async def store_review_output(
            self,
            run_id: UUID,
            model_output: dict[str, object],
            parsed: ReviewOutput,
            processed: ProcessedReviewOutput,
        ) -> ReviewPublication | None:
            stored_processed.append(processed)
            assert "_dropped_findings" not in model_output
            return self.publication

    repo = Repo(_context(), _publication())
    factory = RecordingFactory(repo)
    provider = RecordingProvider()
    value = VerifiedReviewOutput(
        valid_output(),
        dropped_findings=[
            {
                "finding": {
                    "path": "app/example.py",
                    "line": 15,
                    "start_line": None,
                    "severity": "medium",
                    "category": "performance",
                    "title": "Dropped candidate",
                    "body": "Unshown evidence in prompt",
                    "suggestion": None,
                    "confidence": 0.8,
                    "rule_name": None,
                },
                "drop_reason": "evidence_not_shown",
                "position": 1,
            },
            DroppedCandidateRecord(
                finding={
                    "path": "app/example.py",
                    "line": 16,
                    "start_line": None,
                    "severity": "low",
                    "category": "readability",
                    "title": "Below threshold item",
                    "body": "Filtered in pass 6",
                    "suggestion": None,
                    "confidence": 0.8,
                    "rule_name": None,
                },
                drop_reason="severity_threshold",
                position=2,
            ),
        ],
        severity_threshold="critical",
    )
    assert len(value) == 2
    assert set(value) == {"findings", "summary"}
    assert asyncio.run(PublishReviewOutput(factory, provider).execute(RUN_ID, value)) is True
    assert len(stored_processed) == 1
    assert len(stored_processed[0].dropped) == 2
    assert stored_processed[0].dropped[0].drop_reason == "evidence_not_shown"
    assert stored_processed[0].dropped[1].drop_reason == "severity_threshold"
    assert "Below severity threshold (critical)" in stored_processed[0].review_body

    # Raw dict with _dropped_findings is rejected by strict schema validation
    raw_with_underscore = valid_output()
    raw_with_underscore["_dropped_findings"] = "not-a-list"
    with pytest.raises(InvalidReviewOutput):
        asyncio.run(PublishReviewOutput(factory, provider).execute(RUN_ID, raw_with_underscore))

    # Corrupted drop record through PublishReviewOutput.execute
    # raises InvalidReviewOutput with run_id
    corrupted_publish_value = VerifiedReviewOutput(
        valid_output(),
        dropped_findings=[{"finding": {"bad": 1}, "drop_reason": "x", "position": 0}],
    )
    with pytest.raises(InvalidReviewOutput) as exc_info:
        asyncio.run(PublishReviewOutput(factory, provider).execute(RUN_ID, corrupted_publish_value))
    assert str(RUN_ID) in str(exc_info.value)
