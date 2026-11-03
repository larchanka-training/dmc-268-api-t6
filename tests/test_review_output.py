"""Public contract tests for strict model review output parsing."""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from types import TracebackType
from typing import Any, cast
from uuid import UUID

import pytest
from pydantic import ValidationError

from app.modules.reviews.application.findings_post_processor import ProcessedReviewOutput
from app.modules.reviews.application.review_output import (
    FindingPostProcessingInput,
    InvalidReviewOutput,
    PublishedFinding,
    PublishReviewOutput,
    ReviewOutput,
    ReviewPublication,
    comment_body,
    parse_review_output,
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


RUN_ID = UUID("00000000-0000-0000-0000-000000000408")


@dataclass
class RecordingRepository:
    context: FindingPostProcessingInput | None
    publication: ReviewPublication | None
    raw_objects: list[dict[str, object]] = field(default_factory=list)
    marked: int = 0

    async def get_post_processing_input(self, run_id: UUID) -> FindingPostProcessingInput | None:
        assert run_id == RUN_ID
        return self.context

    async def store_review_output(
        self,
        run_id: UUID,
        model_output: dict[str, object],
        parsed: ReviewOutput,
        processed: ProcessedReviewOutput,
    ) -> ReviewPublication | None:
        assert run_id == RUN_ID
        assert parsed.findings[0].title == "Missing transaction boundary"
        self.raw_objects.append(model_output)
        return self.publication

    async def mark_review_published(self, run_id: UUID) -> None:
        assert run_id == RUN_ID
        self.marked += 1


@dataclass
class RecordingUnitOfWork:
    reviews: RecordingRepository
    commits: int = 0
    active: bool = False

    async def __aenter__(self) -> RecordingUnitOfWork:
        self.active = True
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.active = False

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        return None


@dataclass
class RecordingFactory:
    repository: RecordingRepository
    units: list[RecordingUnitOfWork] = field(default_factory=list)

    def __call__(self) -> RecordingUnitOfWork:
        unit = RecordingUnitOfWork(self.repository)
        self.units.append(unit)
        return unit


@dataclass
class RecordingProvider:
    calls: list[tuple[str, str, tuple[PublishedFinding, ...], str]] = field(default_factory=list)
    units: list[RecordingUnitOfWork] | None = None

    async def publish_review(
        self,
        *,
        commit_sha: str,
        body: str,
        findings: tuple[PublishedFinding, ...],
        idempotency_key: str,
    ) -> None:
        if self.units is not None:
            assert len(self.units) == 2  # context read and output write have completed
            assert all(not unit.active for unit in self.units)
        self.calls.append((commit_sha, body, findings, idempotency_key))


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


def test_validate_v2_coordinates_all_branches() -> None:
    from app.modules.reviews.application.prompt_builder import ChangedFile, DiffLine
    from app.modules.reviews.application.review_output import (
        EvidenceRef,
        ReviewFindingV2,
        validate_v2_coordinates,
    )

    def make_finding(
        path: str = "app/main.py",
        line: int = 10,
        start_line: int | None = None,
    ) -> ReviewFindingV2:
        return ReviewFindingV2(
            path=path,
            line=line,
            start_line=start_line,
            severity="high",
            category="correctness",
            title="Title",
            body="Body",
            suggestion=None,
            confidence=0.9,
            rule_name=None,
            evidence_refs=[
                EvidenceRef(block_id="b1", start_line=start_line or line, end_line=line)
            ],
        )

    # 1. unknown_path
    f = make_finding(path="unknown.py", line=10)
    assert validate_v2_coordinates(f, (), original_files=()) == (False, "unknown_path")

    # 2. File not truncated
    lines = (DiffLine(10, "added", "x=1"), DiffLine(12, "added", "x=2"))
    file_not_truncated = ChangedFile("app/main.py", "modified", lines)

    # Line not in shown
    assert validate_v2_coordinates(
        make_finding(line=15), (file_not_truncated,), original_files=(file_not_truncated,)
    ) == (
        False,
        "line_absent",
    )
    # Start line not in shown
    assert validate_v2_coordinates(
        make_finding(line=12, start_line=11),
        (file_not_truncated,),
        original_files=(file_not_truncated,),
    ) == (
        False,
        "line_absent",
    )

    # 3. File was truncated (total_lines = 100, shown lines 1..10)
    lines_orig_100 = tuple(DiffLine(i, "added", f"x_{i}") for i in range(1, 101))
    file_orig_100 = ChangedFile("app/main.py", "modified", lines_orig_100)
    lines_trunc = tuple(DiffLine(i, "added", f"x_{i}") for i in range(1, 11))
    file_truncated = ChangedFile("app/main.py", "modified", lines_trunc, total_lines=100)

    # Line > total_lines
    assert validate_v2_coordinates(
        make_finding(line=105), (file_truncated,), original_files=(file_orig_100,)
    ) == (
        False,
        "line_absent",
    )
    # Line <= total_lines but not in shown (anchor_not_visible)
    assert validate_v2_coordinates(
        make_finding(line=50), (file_truncated,), original_files=(file_orig_100,)
    ) == (
        False,
        "anchor_not_visible",
    )
    # Truncated file with lines 5..10: line 2 before hunk start is line_absent when not in original
    file_trunc_5_10 = ChangedFile(
        "app/main.py",
        "modified",
        tuple(DiffLine(i, "added", f"x_{i}") for i in range(5, 11)),
        total_lines=100,
    )
    file_orig_5_100 = ChangedFile(
        "app/main.py",
        "modified",
        tuple(DiffLine(i, "added", f"x_{i}") for i in range(5, 101)),
    )
    assert validate_v2_coordinates(
        make_finding(line=10, start_line=2), (file_trunc_5_10,), original_files=(file_orig_5_100,)
    ) == (
        False,
        "line_absent",
    )
    # Start line <= total_lines and in shown (success)
    assert validate_v2_coordinates(
        make_finding(line=10, start_line=8), (file_truncated,), original_files=(file_orig_100,)
    ) == (
        True,
        None,
    )

    # 3b. With pre-fit original_files provided
    orig_file_5000 = ChangedFile(
        "app/main.py",
        "modified",
        (
            *tuple(DiffLine(i, "added", f"x_{i}") for i in range(5000, 5400)),
            DiffLine(5401, "added", "gap_line"),
            DiffLine(6000, "added", "second_hunk"),
        ),
    )
    shown_file_5000 = ChangedFile(
        "app/main.py",
        "modified",
        tuple(DiffLine(i, "added", f"x_{i}") for i in range(5000, 5057)),
        total_lines=402,
    )
    assert validate_v2_coordinates(
        make_finding(line=5005, start_line=5000),
        (shown_file_5000,),
        original_files=(orig_file_5000,),
    ) == (True, None)
    assert validate_v2_coordinates(
        make_finding(line=5300),
        (shown_file_5000,),
        original_files=(orig_file_5000,),
    ) == (False, "anchor_not_visible")
    assert validate_v2_coordinates(
        make_finding(line=5060, start_line=5050),
        (shown_file_5000,),
        original_files=(orig_file_5000,),
    ) == (False, "anchor_not_visible")
    assert validate_v2_coordinates(
        make_finding(line=5005),
        (),
        original_files=(orig_file_5000,),
    ) == (False, "anchor_not_visible")
    assert validate_v2_coordinates(
        make_finding(line=3),
        (shown_file_5000,),
        original_files=(orig_file_5000,),
    ) == (False, "line_absent")
    assert validate_v2_coordinates(
        make_finding(line=5005, start_line=3),
        (shown_file_5000,),
        original_files=(orig_file_5000,),
    ) == (False, "line_absent")
    assert validate_v2_coordinates(
        make_finding(line=5401, start_line=5399),
        (shown_file_5000,),
        original_files=(orig_file_5000,),
    ) == (False, "cross_hunk_range")
    assert validate_v2_coordinates(
        make_finding(line=6000, start_line=5005),
        (shown_file_5000,),
        original_files=(orig_file_5000,),
    ) == (False, "cross_hunk_range")

    # 4. Non-1-based truncated hunk (e.g. lines 5000..5056 shown out of total_lines=400)
    orig_file_full_5000 = ChangedFile(
        "app/main.py",
        "modified",
        tuple(DiffLine(i, "added", f"x_{i}") for i in range(5000, 5400)),
    )
    file_trunc_5000 = ChangedFile(
        "app/main.py",
        "modified",
        tuple(DiffLine(i, "added", f"x_{i}") for i in range(5000, 5057)),
        total_lines=400,
    )
    assert validate_v2_coordinates(
        make_finding(line=5005), (file_trunc_5000,), original_files=(orig_file_full_5000,)
    ) == (True, None)
    assert validate_v2_coordinates(
        make_finding(line=5300), (file_trunc_5000,), original_files=(orig_file_full_5000,)
    ) == (
        False,
        "anchor_not_visible",
    )
    assert validate_v2_coordinates(
        make_finding(line=3), (file_trunc_5000,), original_files=(orig_file_full_5000,)
    ) == (
        False,
        "line_absent",
    )
    assert validate_v2_coordinates(
        make_finding(line=5600), (file_trunc_5000,), original_files=(orig_file_full_5000,)
    ) == (
        False,
        "line_absent",
    )
    assert validate_v2_coordinates(
        make_finding(line=5005, start_line=3),
        (file_trunc_5000,),
        original_files=(orig_file_full_5000,),
    ) == (
        False,
        "line_absent",
    )
    assert validate_v2_coordinates(
        make_finding(line=5056, start_line=5005),
        (file_trunc_5000,),
        original_files=(orig_file_full_5000,),
    ) == (True, None)

    # 5. RIGHT candidate on a removed-only line number via original_files -> line_absent
    file_removed_and_added = ChangedFile(
        "app/main.py",
        "modified",
        (DiffLine(10, "removed", "old_code"), DiffLine(20, "added", "new_code")),
    )
    file_shown_added_only = ChangedFile(
        "app/main.py",
        "modified",
        (DiffLine(20, "added", "new_code"),),
    )
    # Line 10 exists only as removed line -> line_absent
    assert validate_v2_coordinates(
        make_finding(line=10), (file_shown_added_only,), original_files=(file_removed_and_added,)
    ) == (
        False,
        "line_absent",
    )

    # 6. Multiline range checks: cross_hunk_range
    # Range spans two hunks (break > 1: line 1 and line 3): cross_hunk_range
    file_gap = ChangedFile(
        "app/main.py", "modified", (DiffLine(1, "added", "a"), DiffLine(3, "added", "c"))
    )
    assert validate_v2_coordinates(
        make_finding(line=3, start_line=1), (file_gap,), original_files=(file_gap,)
    ) == (
        False,
        "cross_hunk_range",
    )

    # Range spans two distinct hunks (10..12 and 100..102): cross_hunk_range
    file_two_hunks = ChangedFile(
        "app/main.py",
        "modified",
        (
            DiffLine(10, "added", "a"),
            DiffLine(11, "added", "b"),
            DiffLine(12, "added", "c"),
            DiffLine(100, "added", "x"),
            DiffLine(101, "added", "y"),
            DiffLine(102, "added", "z"),
        ),
    )
    assert validate_v2_coordinates(
        make_finding(line=101, start_line=10), (file_two_hunks,), original_files=(file_two_hunks,)
    ) == (
        False,
        "cross_hunk_range",
    )

    # 7. All valid
    assert validate_v2_coordinates(
        make_finding(line=10, start_line=8), (file_truncated,), original_files=(file_orig_100,)
    ) == (
        True,
        None,
    )


def test_validate_v2_coordinates_drops_an_anchor_on_a_proven_ignored_file_first() -> None:
    from app.modules.reviews.application.prompt_builder import ChangedFile, DiffLine
    from app.modules.reviews.application.review_output import (
        EvidenceRef,
        ReviewFindingV2,
        validate_v2_coordinates,
    )

    def make_finding(path: str) -> ReviewFindingV2:
        return ReviewFindingV2(
            path=path,
            line=10,
            start_line=None,
            severity="high",
            category="correctness",
            title="Title",
            body="Body",
            suggestion=None,
            confidence=0.9,
            rule_name=None,
            evidence_refs=[EvidenceRef(block_id="b1", start_line=10, end_line=10)],
        )

    shown = ChangedFile("app/main.py", "modified", (DiffLine(10, "added", "x=1"),))
    ignored = frozenset({"generated/api.py"})

    # CVS §6.2 pass 2: the stage proved the name, so the check precedes the lookup.
    assert validate_v2_coordinates(
        make_finding("generated/api.py"), (shown,), original_files=(shown,), ignored_paths=ignored
    ) == (False, "ignored_file")
    # It also precedes the existence of the line.
    masked = ChangedFile("generated/api.py", "modified", (DiffLine(1, "added", "y=1"),))
    assert validate_v2_coordinates(
        make_finding("generated/api.py"),
        (shown,),
        original_files=(shown, masked),
        ignored_paths=ignored,
    ) == (False, "ignored_file")
    # Another name, even under the same mask, proves nothing.
    assert validate_v2_coordinates(
        make_finding("generated/invented.py"),
        (shown,),
        original_files=(shown,),
        ignored_paths=ignored,
    ) == (False, "unknown_path")
    assert validate_v2_coordinates(
        make_finding("app/main.py"), (shown,), original_files=(shown,), ignored_paths=ignored
    ) == (True, None)


def test_validate_v2_attribution_all_branches() -> None:
    from app.modules.reviews.application.policy_snapshot import (
        CallManifest,
        ManifestBlock,
        SourceRole,
    )
    from app.modules.reviews.application.prompt_builder import PullRequestMeta
    from app.modules.reviews.application.review_output import (
        EvidenceRef,
        ReviewFindingV2,
        validate_v2_attribution,
    )

    pr_meta = PullRequestMeta(
        title="PR",
        description=None,
        author="alice",
        source_branch="feature",
        target_branch="main",
        labels=(),
        files_changed=1,
        lines_added=10,
        lines_removed=0,
        is_draft=False,
        is_fork=False,
        head_sha="head123",
        diff_old_sha="old123",
    )

    def make_finding(
        evidence_refs: list[EvidenceRef],
        rule_name: str | None = None,
    ) -> ReviewFindingV2:
        return ReviewFindingV2(
            path="app/main.py",
            line=10,
            start_line=None,
            severity="high",
            category="correctness",
            title="Title",
            body="Body",
            suggestion=None,
            confidence=0.9,
            rule_name=rule_name,
            evidence_refs=evidence_refs,
        )

    # Block not found
    manifest_empty = CallManifest(
        call_id=UUID("00000000-0000-0000-0000-000000000001"),
        stage="review",
        recovery_kind="none",
        blocks=(),
    )
    f = make_finding([EvidenceRef(block_id="missing", start_line=1, end_line=1)])
    ok, err = validate_v2_attribution(f, manifest_empty, pr_meta=pr_meta)
    assert not ok and "evidence_not_shown" in str(err)

    # Block has invalid role
    block_invalid_role = ManifestBlock(
        block_id="b_invalid",
        source_role=SourceRole.POLICY_DOCUMENT,
        source_kind="repository_rules",
    )
    manifest_inv = CallManifest(
        call_id=UUID("00000000-0000-0000-0000-000000000001"),
        stage="review",
        recovery_kind="none",
        blocks=(block_invalid_role,),
    )
    f = make_finding([EvidenceRef(block_id="b_invalid", start_line=1, end_line=1)])
    ok, err = validate_v2_attribution(f, manifest_inv, pr_meta=pr_meta)
    assert not ok and "evidence_source_disallowed" in str(err)

    # LEFT revision match vs mismatch
    left_block_ok = ManifestBlock(
        block_id="left_ok",
        source_role=SourceRole.DIFF,
        source_kind="source_code",
        side="LEFT",
        revision="old123",
        shown_ranges=((1, 10),),
    )
    left_block_mismatch = ManifestBlock(
        block_id="left_mismatch",
        source_role=SourceRole.DIFF,
        source_kind="source_code",
        side="LEFT",
        revision="wrong_old",
        shown_ranges=((1, 10),),
    )
    right_block_mismatch = ManifestBlock(
        block_id="right_mismatch",
        source_role=SourceRole.DIFF,
        source_kind="source_code",
        side="RIGHT",
        revision="wrong_head",
        shown_ranges=((1, 10),),
    )
    manifest_revs = CallManifest(
        call_id=UUID("00000000-0000-0000-0000-000000000001"),
        stage="review",
        recovery_kind="none",
        blocks=(left_block_ok, left_block_mismatch, right_block_mismatch),
    )
    # LEFT mismatch
    f_left_bad = make_finding([EvidenceRef(block_id="left_mismatch", start_line=1, end_line=1)])
    ok, err = validate_v2_attribution(f_left_bad, manifest_revs, pr_meta=pr_meta)
    assert not ok and "revision_mismatch" in str(err)

    # RIGHT mismatch
    f_right_bad = make_finding([EvidenceRef(block_id="right_mismatch", start_line=1, end_line=1)])
    ok, err = validate_v2_attribution(f_right_bad, manifest_revs, pr_meta=pr_meta)
    assert not ok and "revision_mismatch" in str(err)

    # LEFT match succeeds
    f_left_ok = make_finding([EvidenceRef(block_id="left_ok", start_line=1, end_line=1)])
    ok, err = validate_v2_attribution(f_left_ok, manifest_revs, pr_meta=pr_meta)
    assert ok and err is None

    # Range not shown
    f_range_bad = make_finding([EvidenceRef(block_id="left_ok", start_line=20, end_line=25)])
    ok, err = validate_v2_attribution(f_range_bad, manifest_revs, pr_meta=pr_meta)
    assert not ok and "evidence_not_shown" in str(err)

    # Rule attribution checks when policy/defaults blocks are absent vs present
    for bad_rule in (".review/rules.md", "AGENTS.md", "service-defaults:N1"):
        f_rule_bad = make_finding(
            [EvidenceRef(block_id="left_ok", start_line=1, end_line=1)],
            rule_name=bad_rule,
        )
        ok, err = validate_v2_attribution(f_rule_bad, manifest_revs, pr_meta=pr_meta)
        assert not ok and "evidence_source_disallowed" in str(err)

    manifest_with_all_policies = CallManifest(
        call_id=UUID("00000000-0000-0000-0000-000000000001"),
        stage="review",
        recovery_kind="none",
        blocks=(
            left_block_ok,
            ManifestBlock(
                block_id="policy-rules",
                source_role=SourceRole.POLICY_DOCUMENT,
                source_kind="repository_rules",
                start_line=1,
                end_line=1,
                shown_line_count=1,
                shown_ranges=((1, 1),),
            ),
            ManifestBlock(
                block_id="policy-agents",
                source_role=SourceRole.POLICY_DOCUMENT,
                source_kind="project_conventions_document",
                start_line=1,
                end_line=1,
                shown_line_count=1,
                shown_ranges=((1, 1),),
            ),
            ManifestBlock(
                block_id="service-defaults",
                source_role=SourceRole.SERVICE_DEFAULTS,
                source_kind="service_defaults",
            ),
        ),
    )
    for ok_rule in (".review/rules.md", "AGENTS.md", "service-defaults:N1"):
        f_rule_ok = make_finding(
            [EvidenceRef(block_id="left_ok", start_line=1, end_line=1)],
            rule_name=ok_rule,
        )
        ok, err = validate_v2_attribution(f_rule_ok, manifest_with_all_policies, pr_meta=pr_meta)
        assert ok and err is None

    # LEFT block with diff_old_sha=None fails closed with revision_mismatch
    pr_meta_no_old = PullRequestMeta(
        title="PR",
        description=None,
        author="alice",
        source_branch="feature",
        target_branch="main",
        labels=(),
        files_changed=1,
        lines_added=10,
        lines_removed=0,
        is_draft=False,
        is_fork=False,
        head_sha="head123",
        diff_old_sha=None,
    )
    ok, err = validate_v2_attribution(f_left_ok, manifest_revs, pr_meta=pr_meta_no_old)
    assert not ok and "revision_mismatch" in str(err)

    # pr_meta=None skips revision check
    ok, err = validate_v2_attribution(f_left_ok, manifest_revs, pr_meta=None)
    assert ok and err is None


def test_review_output_v2_and_answers_edge_branches(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.modules.reviews.infrastructure.llm.answers as answers_mod
    from app.modules.reviews.application.review_output import (
        EvidenceRef,
        RequirementRef,
        ReviewFindingV2,
        ReviewOutputV2,
        ReviewSummary,
        parse_review_output_v2,
    )
    from app.modules.reviews.infrastructure.llm.answers import (
        InvalidAnswer,
        _bounded,
        parse_review_answer,
        validate_review_answer,
    )

    with pytest.raises(ValidationError, match="start_line must be <= end_line"):
        EvidenceRef(block_id="b1", start_line=5, end_line=3)

    req_ok = RequirementRef(block_id="b1", start_line=1, end_line=2, check_id="N1")
    assert req_ok.check_id == "N1"
    with pytest.raises(ValidationError, match="start_line must be <= end_line"):
        RequirementRef(block_id="b1", start_line=5, end_line=3, check_id=None)

    base_kw: dict[str, Any] = {
        "path": "app/main.py",
        "line": 10,
        "start_line": None,
        "severity": "high",
        "category": "correctness",
        "title": "Valid title",
        "body": "Body",
        "suggestion": None,
        "confidence": 0.9,
        "rule_name": None,
        "evidence_refs": [EvidenceRef(block_id="b1", start_line=10, end_line=10)],
    }
    with pytest.raises(ValidationError, match="start_line must be strictly earlier than line"):
        ReviewFindingV2(**{**base_kw, "start_line": 10})
    with pytest.raises(ValidationError, match="title must not end with a period"):
        ReviewFindingV2(**{**base_kw, "title": "Ends with period."})
    with pytest.raises(ValidationError, match="title must be one line"):
        ReviewFindingV2(**{**base_kw, "title": "Line 1\nLine 2"})

    f_high = ReviewFindingV2(**base_kw)
    f_low = ReviewFindingV2(**{**base_kw, "severity": "low", "confidence": 0.5})
    out_v2 = ReviewOutputV2(
        findings=[f_high, f_low],
        summary=ReviewSummary(problem="Problem.", done_well="Done well.", effort="small"),
    )
    assert len(out_v2.findings) == 2
    with pytest.raises(ValidationError, match="findings must be severity then confidence ordered"):
        ReviewOutputV2(
            findings=[f_low, f_high],
            summary=ReviewSummary(problem="Problem.", done_well="Done well.", effort="small"),
        )

    # parse_review_output strips evidence_refs from v2 dict findings
    raw_with_v2 = {
        "findings": [
            {
                **{k: v for k, v in base_kw.items() if k != "evidence_refs"},
                "evidence_refs": [{"block_id": "b1", "start_line": 10, "end_line": 10}],
            }
        ],
        "summary": {"problem": "Problem.", "done_well": "Done well.", "effort": "small"},
    }
    parsed_v1 = parse_review_output(raw_with_v2)
    assert len(parsed_v1.findings) == 1
    with pytest.raises(InvalidReviewOutput):
        parse_review_output("[]")

    # parse_review_output_v2 str, dict, and error
    assert len(parse_review_output_v2(raw_with_v2).findings) == 1
    assert len(parse_review_output_v2(json.dumps(raw_with_v2)).findings) == 1
    with pytest.raises(InvalidReviewOutput):
        parse_review_output_v2("{bad")

    # answers.py edge branches
    raw_valid_v1 = json.dumps(
        {
            "findings": [],
            "summary": {"problem": "None.", "done_well": "Good.", "effort": "none"},
        }
    )
    assert parse_review_answer(raw_valid_v1).findings == []
    assert "…" in _bounded("x" * 500)

    def _raise_invalid_review_output(_obj: object) -> ReviewOutput:
        try:
            ReviewSummary.model_validate({"problem": "", "done_well": "d", "effort": "none"})
        except ValidationError as ve:
            raise InvalidReviewOutput("bad") from ve
        raise AssertionError("unreachable")

    monkeypatch.setattr(answers_mod, "parse_review_output", _raise_invalid_review_output)
    with pytest.raises(InvalidAnswer):
        validate_review_answer(raw_valid_v1)
