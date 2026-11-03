"""A snapshot-path change of technical skips only ends without any model call (#130 F5).

CVS §2.2 row :154 and §11: a complete inventory with at least one T and otherwise only
exclusions is published with a backend result, coverage none and no verdict, after
``context.build`` and before RAG, the context build, conventions, the review and the
verifier (DD2-DD5). T19 (exclusions only) still comes first; a legacy (<= v3) run keeps its
model calls; a summary-only change stops before the builder.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any, cast
from uuid import UUID

import pytest

from app.modules.reviews.application.conventions import (
    GeneratedConventions,
    GenerateRepoConventions,
)
from app.modules.reviews.application.execute_review import ExecuteReviewRun, ReviewPromptInput
from app.modules.reviews.application.get_run_diff import DiffSnapshot, StoreDiffSnapshot
from app.modules.reviews.application.policy_errors import PolicyError, PolicyErrorCode
from app.modules.reviews.application.process_run import ReviewRunProcessor, RunVcsInput
from app.modules.reviews.application.prompt_builder import ReviewContext
from app.modules.reviews.application.rag_index import OpenReviewIndex, RagReadiness
from app.modules.reviews.application.review_context import (
    BuildReviewContext,
    NoReviewableText,
    SummaryOnlyReview,
)
from app.modules.reviews.application.review_context_stage import (
    EstimatedTokenCounter,
    ReviewContextStage,
)
from app.modules.reviews.application.run_failures import RunCancelled, RunSkipped
from app.modules.reviews.application.store_review_output import NoModelAnswer
from app.modules.reviews.application.vcs_diff import VcsFile
from app.modules.reviews.infrastructure.blob_cache import InMemoryBlobCache
from tests.test_coverage_none_classification import (
    metadata_only_rename,
    unresolved_identity,
    unsupported_path,
)
from tests.test_review_context_stage import Parser, Payloads, Trace, Vcs
from tests.test_review_context_stage import Repository as SourceTree
from tests.test_review_context_stage import _stage as _context_stage
from tests.test_review_path import (
    CountingGitHub,
    PinnedRepository,
    Pipeline,
    RecordingConventions,
    _binding,
    _snapshot,
    _store,
)
from tests.test_run_processor_context_stage import RUN_ID
from tests.trigger_uow import processing_uow

_BINARY = DiffSnapshot("assets/logo.png", None, omission_reason="binary")
_MERGE_BASE = "c" * 40


@dataclass
class Builder:
    """The context builder factory, counting the builds it was asked for."""

    runs: list[RunVcsInput] = field(default_factory=list)

    def __call__(self, run: RunVcsInput) -> BuildReviewContext:
        self.runs.append(run)
        return BuildReviewContext(
            source=SourceTree(), parser=Parser(), counter=EstimatedTokenCounter()
        )


class Rag(OpenReviewIndex):
    """An index the review never reads: RAG is off, every open is counted."""

    def __init__(self) -> None:
        self.opened: list[UUID] = []

    async def execute(self, repository_id: UUID) -> RagReadiness:
        self.opened.append(repository_id)
        return RagReadiness(enabled=False)


@dataclass
class Stage:
    stage: ReviewContextStage
    trace: Trace
    payloads: Payloads
    builder: Builder
    rag: Rag

    def steps(self, tool: str) -> list[Any]:
        return [response for name, _, response in self.trace.steps if name == tool]


def _stage(snapshots: list[DiffSnapshot], *, version: str = "v4") -> Stage:
    trace, payloads, builder, rag = Trace(), Payloads(), Builder(), Rag()
    stage = _context_stage(
        trace,
        store=_store(version),
        snapshots=snapshots,
        vcs=Vcs(files_changed=len(snapshots)),
        context_builder=builder,
        rag=rag,
        payloads=payloads,
    )
    return Stage(stage, trace, payloads, builder, rag)


@pytest.mark.parametrize(
    ("snapshots", "reasons", "ignored"),
    [
        ([unsupported_path()], {"unsupported_path": 1}, 0),
        ([unresolved_identity()], {"identity_unresolved": 1}, 0),
        ([unsupported_path(), _BINARY], {"unsupported_path": 1}, 1),
        # Unreachable live (no adapter sends a metadata patch), routed like any T.
        ([metadata_only_rename()], {"unsupported_change": 1}, 0),
    ],
    ids=["unsupported-path", "identity", "with-binary", "unsupported-change"],
)
def test_a_technical_only_v4_change_records_its_context_and_ends_before_the_build(
    snapshots: list[DiffSnapshot], reasons: dict[str, int], ignored: int
) -> None:
    run = _stage(snapshots)

    with pytest.raises(NoReviewableText):
        asyncio.run(run.stage.run(RUN_ID))

    assert [tool for tool, _, _ in run.trace.steps] == ["policy.load", "context.build"]
    [response] = run.steps("context.build")
    coverage = response["coverage"]
    assert coverage["status"] == "none"
    assert coverage["reason_counts"] == reasons
    assert coverage["reviewed_files"] == 0
    assert response == {
        "status": "no_reviewable_text",
        "reason": "no_reviewable_text",
        "diff": {
            "digest": coverage["diff_digest"],
            "files": len(snapshots),
            "diff_old_sha": _MERGE_BASE,
            "file_list_complete": True,
        },
        "ignored_files": ignored,
        "blocks": [],
        "coverage": coverage,
    }
    # The attempt's one context.build is also its context_payloads row (#168 part 6).
    assert run.payloads.rows == [(RUN_ID, response)]
    assert run.builder.runs == []
    assert run.rag.opened == []
    assert run.stage.payload is None
    assert run.stage.diff_old_sha == _MERGE_BASE


@pytest.mark.parametrize(
    ("change", "coverage"),
    [("unsupported_path", "partial"), ("too_large", "none")],
    ids=["unsupported-path", "too-large"],
)
@pytest.mark.parametrize("version", ["v2", "v3"])
def test_the_same_change_on_a_legacy_run_is_built_for_the_model(
    version: str, change: str, coverage: str
) -> None:
    # Legacy unchanged: an unsupported path stays partial, and a too_large-only change keeps
    # the coverage none it has had since #283; both are built and go to the model.
    snapshots = [unsupported_path()] if change == "unsupported_path" else _too_large(1)
    run = _stage(snapshots, version=version)

    payload = asyncio.run(run.stage.run(RUN_ID))

    assert payload is not None
    assert len(run.builder.runs) == 1
    [response] = run.steps("context.build")
    assert response["status"] == "summary_only"
    assert response["coverage"]["status"] == coverage
    assert run.payloads.rows == [(RUN_ID, response)]


def test_an_exclusions_only_v4_change_is_still_skipped_by_t19_first() -> None:
    run = _stage([_BINARY])

    with pytest.raises(RunSkipped, match="all_changes_ignored"):
        asyncio.run(run.stage.run(RUN_ID))

    [response] = run.steps("context.build")
    assert response["status"] == "all_changes_ignored"
    # The T19 record keeps its shape (PIPELINE_SPEC): no old side, no inventory flag.
    assert response["diff"] == {"digest": response["coverage"]["diff_digest"], "files": 1}
    assert run.payloads.rows == []
    assert run.builder.runs == []


def _too_large(lines: int) -> list[DiffSnapshot]:
    """A too_large text file stored as the processor stores it: with the 3 000-line threshold."""

    @dataclass
    class Snapshots:
        async def store_diff_snapshots(
            self, run_id: UUID, code_change_id: UUID, head_sha: str, snapshots: list[DiffSnapshot]
        ) -> list[DiffSnapshot]:
            return snapshots

    files = [
        DiffSnapshot(
            "data/large.txt",
            None,
            "d" * 40,
            additions=lines,
            changes=lines,
            omission_reason="too_large",
        )
    ]
    return asyncio.run(
        StoreDiffSnapshot(Snapshots()).execute(
            run_id=RUN_ID,
            code_change_id=UUID(int=7),
            head_sha="a" * 40,
            files=files,
            excluded=lambda snapshot: False,
        )
    )


def test_a_too_large_change_takes_the_route_up_to_the_summary_only_threshold() -> None:
    at_limit, over = _too_large(3000), _too_large(3001)
    assert [item.summary_only for item in (*at_limit, *over)] == [False, True]

    routed = _stage(at_limit)
    with pytest.raises(NoReviewableText):
        asyncio.run(routed.stage.run(RUN_ID))
    assert routed.builder.runs == []

    # Over the threshold the change stops before the builder (#313).
    summary = _stage(over)
    with pytest.raises(SummaryOnlyReview):
        asyncio.run(summary.stage.run(RUN_ID))
    assert summary.builder.runs == []
    [response] = summary.steps("context.build")
    assert response["coverage"]["status"] == "partial"
    assert response["status"] == "summary_only"
    assert response["reason"] == "diff_too_large"
    assert response["blocks"] == []


def test_a_summary_only_flag_alone_does_not_choose_the_line_limit() -> None:
    # The persisted flag is not order 2. This inventory is one line, so the builder runs.
    run = _stage([replace(unsupported_path(), summary_only=True)])

    assert asyncio.run(run.stage.run(RUN_ID)) is not None

    assert len(run.builder.runs) == 1
    [response] = run.steps("context.build")
    assert response["reason"] != "diff_too_large"


def test_the_processor_lets_the_signal_through_before_the_conventions_call() -> None:
    events: list[str] = []
    repository = PinnedRepository(events, binding=_binding("v4"))
    conventions = RecordingConventions(events)
    github = CountingGitHub(
        files=(VcsFile("src\\bad.py", "modified", "1" * 40, None, 1, 1, 2, "@@ -1 +1 @@\n-a\n+b"),)
    )
    run = _stage([unsupported_path()])
    processor = ReviewRunProcessor(
        repository,
        None,
        blob_cache=InMemoryBlobCache(),
        conventions=cast(GenerateRepoConventions, conventions),
        vcs_provider=github,
        uow_factory=processing_uow(repository),
        context_stage=run.stage,
    )

    with pytest.raises(NoReviewableText):
        asyncio.run(processor.prepare(RUN_ID))

    assert events == ["diff stored"]
    assert conventions.calls == []
    assert [tool for tool, _, _ in run.trace.steps] == ["policy.load", "context.build"]


@dataclass
class Reflection:
    """A reflection pass the none route never reaches."""

    calls: list[str] = field(default_factory=list)

    def max_prompt_tokens(self) -> int:
        self.calls.append("max prompt tokens")
        return 100_000


@dataclass
class RoutedPipeline(Pipeline):
    """The use case's ports; ``raise_in`` names the one that raises ``error``.

    ``error`` is the route's signal unless a case gives another failure of the attempt.
    """

    raise_in: str = "prepare"
    error: Callable[[], Exception] = NoReviewableText
    published: list[object] = field(default_factory=list)

    async def prepare(self, run_id: UUID) -> GeneratedConventions:
        if self.raise_in == "prepare":
            self.calls.append("prepare")
            raise self.error()
        return await super().prepare(run_id)

    async def get_review_prompt_input(
        self, run_id: UUID, conventions: GeneratedConventions
    ) -> ReviewPromptInput:
        self.calls.append("prompt input")
        return await super().get_review_prompt_input(run_id, conventions)

    async def draft_review(self, *, context: ReviewContext) -> dict[str, object]:
        result = await super().draft_review(context=context)
        if self.raise_in == "draft review":
            raise self.error()
        return result

    async def execute(self, run_id: UUID, model_output: object) -> bool:
        self.published.append(model_output)
        return True


def test_execute_review_publishes_the_backend_result_without_any_model_input() -> None:
    pipeline = RoutedPipeline(_binding("v4"), snapshot=_snapshot(_store()))
    reflection = Reflection()

    result = asyncio.run(
        ExecuteReviewRun(
            pipeline, pipeline, pipeline, pipeline, reflection_pass=cast(Any, reflection)
        ).execute(RUN_ID)
    )

    assert result is True
    assert pipeline.calls == ["prepare"]
    [published] = pipeline.published
    assert isinstance(published, NoModelAnswer)
    assert reflection.calls == []


def test_execute_review_catches_the_signal_only_around_prepare() -> None:
    pipeline = RoutedPipeline(_binding("v3"), raise_in="draft review")

    with pytest.raises(NoReviewableText):
        asyncio.run(ExecuteReviewRun(pipeline, pipeline, pipeline, pipeline).execute(RUN_ID))

    assert pipeline.calls == ["prepare", "prompt input", "pull request meta", "draft review"]
    assert pipeline.published == []


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (
            lambda: PolicyError(PolicyErrorCode.SNAPSHOT_INTEGRITY_ERROR, "snapshot not ready"),
            PolicyError,
        ),
        (lambda: RunCancelled("cancel_requested"), RunCancelled),
    ],
    ids=["policy-error", "cancelled"],
)
def test_execute_review_catches_no_other_failure_of_prepare(
    error: Callable[[], Exception], expected: type[Exception]
) -> None:
    # Only the route's signal publishes the backend result: a policy failure or a cancel
    # of the attempt ends it as before, with nothing published.
    pipeline = RoutedPipeline(_binding("v4"), snapshot=_snapshot(_store()), error=error)

    with pytest.raises(expected):
        asyncio.run(ExecuteReviewRun(pipeline, pipeline, pipeline, pipeline).execute(RUN_ID))

    assert pipeline.calls == ["prepare"]
    assert pipeline.published == []
