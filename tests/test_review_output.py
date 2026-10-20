"""Public contract tests for strict model review output parsing."""

from __future__ import annotations

import asyncio
import json
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
    return FindingPostProcessingInput({"app/example.py": frozenset({10, 12})}, frozenset())


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
        hunk_lines={"app/example.py": frozenset({10, 12})},
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
                hunk_lines={"app/example.py": frozenset({10, 12})},
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
    store = StoreReviewOutput(cast(Any, lambda: FakeUoW()), queue, worker_id="test-worker")
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
                hunk_lines={"app/example.py": frozenset({10, 12})},
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

    queue = FakeQueue()
    store = StoreReviewOutput(cast(Any, lambda: FakeUoW()), queue, worker_id="test-worker")
    value = valid_output()
    value["_dropped_findings"] = [
        {
            "finding": {
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
            "drop_reason": "evidence_not_shown",
            "position": 1,
        }
    ]

    success = asyncio.run(store.execute(RUN_ID, value))
    assert success is True
    assert len(stored_processed) == 1
    # Check that processed.dropped contains the dropped finding
    assert len(stored_processed[0].dropped) == 1
    dropped_item = stored_processed[0].dropped[0]
    assert dropped_item.finding.title == "Slow query"
    assert dropped_item.drop_reason == "evidence_not_shown"
    assert dropped_item.bucket == "dropped"
    assert dropped_item.position == 1

    # Check postprocess_response logged in enter_publishing contains dropped item
    assert len(entered_publishing) == 1
    dropped_log = entered_publishing[0]["postprocess_response"]["dropped"]
    assert dropped_log == [{"index": 1, "drop_reason": "evidence_not_shown"}]
    assert len(queue.published) == 1


def test_publish_review_output_persists_dropped_findings() -> None:
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
    value = valid_output()
    value["_dropped_findings"] = [
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
        "not-a-dict",
        {"finding": "not-a-dict"},
    ]
    assert asyncio.run(PublishReviewOutput(factory, provider).execute(RUN_ID, value)) is True
    assert len(stored_processed) == 1
    assert len(stored_processed[0].dropped) == 1
    assert stored_processed[0].dropped[0].drop_reason == "evidence_not_shown"

    value_not_list = valid_output()
    value_not_list["_dropped_findings"] = "not-a-list"
    assert (
        asyncio.run(PublishReviewOutput(factory, provider).execute(RUN_ID, value_not_list)) is True
    )


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
    assert validate_v2_coordinates(f, ()) == (False, "unknown_path")

    # 2. File not truncated (total_lines is None)
    lines = (DiffLine(10, "added", "x=1"), DiffLine(12, "added", "x=2"))
    file_not_truncated = ChangedFile("app/main.py", "modified", lines)

    # Line not in shown
    assert validate_v2_coordinates(make_finding(line=15), (file_not_truncated,)) == (
        False,
        "line_absent",
    )
    # Start line not in shown
    assert validate_v2_coordinates(make_finding(line=12, start_line=11), (file_not_truncated,)) == (
        False,
        "line_absent",
    )

    # 3. File was truncated (total_lines = 100, shown lines 1..10)
    lines_trunc = tuple(DiffLine(i, "added", f"x_{i}") for i in range(1, 11))
    file_truncated = ChangedFile("app/main.py", "modified", lines_trunc, total_lines=100)

    # Line > total_lines
    assert validate_v2_coordinates(make_finding(line=105), (file_truncated,)) == (
        False,
        "line_absent",
    )
    # Line <= total_lines but not in shown (anchor_not_visible)
    assert validate_v2_coordinates(make_finding(line=50), (file_truncated,)) == (
        False,
        "anchor_not_visible",
    )
    # Truncated file with lines 5..10: start_line (2) not in shown, but <= total_lines
    file_trunc_5_10 = ChangedFile(
        "app/main.py",
        "modified",
        tuple(DiffLine(i, "added", f"x_{i}") for i in range(5, 11)),
        total_lines=100,
    )
    assert validate_v2_coordinates(make_finding(line=10, start_line=2), (file_trunc_5_10,)) == (
        False,
        "anchor_not_visible",
    )
    # Start line <= total_lines and in shown (success)
    assert validate_v2_coordinates(make_finding(line=10, start_line=8), (file_truncated,)) == (
        True,
        None,
    )

    # 4. Multiline range checks
    # Range has gap: range_gap
    # Shown: 1, 3 (gap at 2)
    file_gap = ChangedFile(
        "app/main.py", "modified", (DiffLine(1, "added", "a"), DiffLine(3, "added", "c"))
    )
    assert validate_v2_coordinates(make_finding(line=3, start_line=1), (file_gap,)) == (
        False,
        "range_gap",
    )

    # 5. All valid
    assert validate_v2_coordinates(make_finding(line=10, start_line=8), (file_truncated,)) == (
        True,
        None,
    )


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
