"""RAG gate and retrieval tracing in the review context stage."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from typing import Any
from uuid import UUID

import pytest
from psycopg.errors import LockNotAvailable
from sqlalchemy.exc import OperationalError

from app.modules.code_index.domain.enums import EdgeKind, IndexStatus, RagIndexStatus
from app.modules.code_index.domain.rag_index import RagIndex
from app.modules.reviews.application import rag_index
from app.modules.reviews.application.context_provenance import LineRange, SourceRef, SourceRole
from app.modules.reviews.application.diff_map import DiffMap, DiffSide, VcsProviderName
from app.modules.reviews.application.graph_retriever import (
    GraphHints,
    GraphTarget,
)
from app.modules.reviews.application.policy_errors import PolicyError, PolicyErrorCode
from app.modules.reviews.application.rag_index import (
    OpenReviewIndex,
    RagReadiness,
)
from app.modules.reviews.application.review_context import (
    BuildReviewContext,
    ContextLevel,
    ContextStatus,
    RagPlan,
    Representation,
    RetrievedBlock,
    ReviewContextPayload,
)
from app.modules.reviews.application.review_context_stage import (
    EstimatedTokenCounter,
    context_summary,
)
from app.modules.reviews.application.run_failures import RunCancelled, RunFailure, RunSkipped
from tests.review_context_stage_support import (
    _BASE,
    _HEAD,
    _INDEXED,
    _PINNED,
    _REPOSITORY,
    _RUN,
    _TARGET,
    _USABLE,
    Parser,
    Policy,
    RagRepository,
    Repository,
    Trace,
    _block,
    _FakeReviewIndexUow,
    _gate,
    _greet_hints,
    _rag_builder,
    _readiness,
    _stage,
)


@dataclass
class RaisingIndexAccess:
    error: Exception
    opened: list[UUID] = field(default_factory=list)

    async def open_for_review(self, repository_id: UUID) -> RagReadiness:
        self.opened.append(repository_id)
        raise self.error


def _failing_gate(error: Exception) -> tuple[OpenReviewIndex, RaisingIndexAccess]:
    access = RaisingIndexAccess(error)
    return OpenReviewIndex(lambda: _FakeReviewIndexUow(access)), access


def test_without_the_gate_the_stage_records_no_index_step() -> None:
    trace = Trace()

    assert asyncio.run(_stage(trace).run(_RUN)) is not None

    assert [step[0] for step in trace.steps] == ["policy.load", "context.build"]


def test_rag_toggle_off_records_no_index_step() -> None:
    trace = Trace()
    gate, access = _gate(RagReadiness(enabled=False))

    assert asyncio.run(_stage(trace, rag=gate).run(_RUN)) is not None

    # The adapter decides that RAG is off; the stage writes nothing for it.
    assert access.opened == [_REPOSITORY]
    assert [step[0] for step in trace.steps] == ["policy.load", "context.build"]


@pytest.mark.parametrize(
    ("status", "error_code", "response"),
    [
        (RagIndexStatus.EMPTY, None, {"outcome": "not_ready", "reason": "empty"}),
        # A code kept from an earlier failure is not the reason of a building index.
        (RagIndexStatus.BUILDING, "archive_unsafe", {"outcome": "not_ready", "reason": "building"}),
        (
            RagIndexStatus.FAILED,
            "limit_exceeded:files",
            {"outcome": "not_ready", "reason": "failed", "error_code": "limit_exceeded:files"},
        ),
        # The stage's pinned prompt (v2) has no retrieved section: a usable index is not used.
        (RagIndexStatus.READY, None, {"outcome": "not_ready", "reason": "prompt_version"}),
        (RagIndexStatus.STALE, None, {"outcome": "not_ready", "reason": "prompt_version"}),
    ],
    ids=["empty", "building", "failed", "ready", "stale"],
)
def test_index_readiness_is_recorded_after_the_context_build(
    status: RagIndexStatus, error_code: str | None, response: dict[str, str]
) -> None:
    trace = Trace()
    gate, access = _gate(_readiness(status, error_code=error_code))

    payload = asyncio.run(_stage(trace, rag=gate).run(_RUN))

    assert payload is not None and payload.status is ContextStatus.REVIEW
    assert access.opened == [_REPOSITORY]
    assert [step[0] for step in trace.steps] == ["policy.load", "context.build", "context.rag"]
    usable = status in _USABLE
    assert trace.steps[2][1] == {
        "index_status": status.value,
        "indexed_commit_sha": _INDEXED if usable else None,
        "default_head_sha": _TARGET,
        "stale": status is RagIndexStatus.STALE,
    }
    assert trace.steps[2][2] == response


def test_stale_index_is_opened_once_and_the_review_continues() -> None:
    trace = Trace()
    gate, access = _gate(_readiness(RagIndexStatus.STALE))

    payload = asyncio.run(asyncio.wait_for(_stage(trace, rag=gate).run(_RUN), timeout=5))

    assert payload is not None and payload.status is ContextStatus.REVIEW
    assert access.opened == [_REPOSITORY]
    assert [step[0] for step in trace.steps].count("context.rag") == 1


def test_not_ready_reason_comes_from_the_index_not_from_its_status() -> None:
    @dataclass(frozen=True)
    class PatchedIndex(RagIndex):
        @property
        def not_ready_reason(self) -> RagIndexStatus | None:
            return RagIndexStatus.BUILDING

    trace = Trace()
    index = PatchedIndex(RagIndexStatus.EMPTY, None, None, 0, 0, None)
    gate, _ = _gate(RagReadiness(enabled=True, index=index, default_head_sha=_TARGET))

    asyncio.run(_stage(trace, rag=gate).run(_RUN))

    assert trace.steps[-1][0] == "context.rag"
    assert trace.steps[-1][2] == {"outcome": "not_ready", "reason": "building"}


@pytest.mark.parametrize(
    ("status", "usable", "reason", "response"),
    [
        # A status of a usable index on an index that cannot serve the review.
        (
            RagIndexStatus.READY,
            False,
            RagIndexStatus.BUILDING,
            {"outcome": "not_ready", "reason": "building"},
        ),
        # A status of an index that is not ready on one the review may read.
        (
            RagIndexStatus.EMPTY,
            True,
            RagIndexStatus.EMPTY,
            {"outcome": "not_ready", "reason": "prompt_version"},
        ),
    ],
    ids=["ready-not-usable", "empty-usable"],
)
def test_usable_comes_from_the_index_not_from_its_status(
    status: RagIndexStatus,
    usable: bool,
    reason: RagIndexStatus,
    response: dict[str, str],
) -> None:
    @dataclass(frozen=True)
    class PatchedIndex(RagIndex):
        @property
        def usable(self) -> bool:
            return usable

        @property
        def not_ready_reason(self) -> RagIndexStatus | None:
            return reason

    trace = Trace()
    index = PatchedIndex(status, None, None, 0, 0, None)
    gate, _ = _gate(RagReadiness(enabled=True, index=index, default_head_sha=_TARGET))

    asyncio.run(_stage(trace, rag=gate).run(_RUN))

    assert trace.steps[-1][0] == "context.rag"
    assert trace.steps[-1][2] == response


def test_caught_build_failure_still_records_the_index_readiness() -> None:
    class Broken:
        async def execute(self, **kwargs: object) -> object:
            raise RuntimeError("the tree is unavailable")

    trace = Trace()
    gate, _ = _gate(_readiness(RagIndexStatus.EMPTY))
    stage = _stage(trace, rag=gate)
    stage._context_builder = lambda run: Broken()  # type: ignore[assignment,return-value]

    assert asyncio.run(stage.run(_RUN)) is None

    assert [step[0] for step in trace.steps] == ["policy.load", "context.build", "context.rag"]
    assert trace.steps[1][2]["error"]["type"] == "RuntimeError"
    assert trace.steps[2][2] == {"outcome": "not_ready", "reason": "empty"}


def test_index_readiness_is_recorded_before_the_skip_of_an_all_ignored_payload() -> None:
    class AllIgnored(BuildReviewContext):
        async def execute(self, **kwargs: Any) -> ReviewContextPayload:
            payload = await super().execute(**kwargs)
            return replace(payload, status=ContextStatus.ALL_CHANGES_IGNORED)

    trace = Trace()
    gate, _ = _gate(_readiness(RagIndexStatus.BUILDING))
    stage = _stage(
        trace,
        rag=gate,
        context_builder=lambda run: AllIgnored(
            source=Repository(), parser=Parser(), counter=EstimatedTokenCounter()
        ),
    )

    with pytest.raises(RunSkipped) as skipped:
        asyncio.run(stage.run(_RUN))

    assert skipped.value.reason == "all_changes_ignored"
    assert [step[0] for step in trace.steps] == ["policy.load", "context.build", "context.rag"]
    assert trace.steps[2][2] == {"outcome": "not_ready", "reason": "building"}


def test_skip_before_the_context_build_does_not_open_the_index() -> None:
    trace = Trace()
    gate, access = _gate(_readiness(RagIndexStatus.READY))
    stage = _stage(trace, rag=gate, policy=Policy(rules=b"```review-ignore\n**\n```\n"))

    with pytest.raises(RunSkipped):
        asyncio.run(stage.run(_RUN))

    assert access.opened == []
    assert [step[0] for step in trace.steps] == ["policy.load", "context.build"]


@pytest.mark.parametrize("failure", ["cancel", "deadline", "policy"])
def test_control_raised_by_the_build_ends_the_attempt_without_the_index_step(
    failure: str,
) -> None:
    error = {
        "cancel": RunCancelled("installation_unavailable"),
        "deadline": RunFailure("deadline_exceeded"),
        "policy": PolicyError(PolicyErrorCode.MATCH_BUDGET_EXCEEDED, "over the budget"),
    }[failure]

    class Broken(BuildReviewContext):
        async def execute(self, **kwargs: Any) -> Any:
            raise error

    trace = Trace()
    gate, access = _gate(_readiness(RagIndexStatus.READY))
    stage = _stage(
        trace,
        rag=gate,
        context_builder=lambda run: Broken(
            source=Repository(), parser=Parser(), counter=EstimatedTokenCounter()
        ),
    )

    with pytest.raises(type(error)) as caught:
        asyncio.run(stage.run(_RUN))

    assert caught.value is error
    assert access.opened == [_REPOSITORY]
    assert [step[0] for step in trace.steps] == ["policy.load", "context.build"]


@pytest.mark.parametrize(
    "error",
    [
        RuntimeError("the database is gone"),
        RunFailure("diff_fetch_failed", "optional"),
        # What the adapter raises when a writer of the repository row outlasts its lock
        # timeout: a gate failure, not the deadline.
        OperationalError(
            "SELECT repositories.id FROM repositories FOR SHARE",
            None,
            LockNotAvailable("canceling statement due to lock timeout"),
        ),
    ],
    ids=["unexpected", "run-failure", "lock-timeout"],
)
def test_failed_gate_is_recorded_and_the_review_continues_without_rag(error: Exception) -> None:
    trace = Trace()
    gate, access = _failing_gate(error)

    payload = asyncio.run(_stage(trace, rag=gate).run(_RUN))

    assert payload is not None and payload.status is ContextStatus.REVIEW
    assert access.opened == [_REPOSITORY]
    assert [step[0] for step in trace.steps] == ["policy.load", "context.build", "context.rag"]
    assert trace.steps[2][1] == {
        "index_status": None,
        "indexed_commit_sha": None,
        "default_head_sha": None,
        "stale": None,
    }
    assert trace.steps[2][2] == {"error": {"type": type(error).__name__, "message": str(error)}}


@pytest.mark.parametrize("failure", ["cancel", "deadline", "policy"])
def test_control_raised_by_the_gate_ends_the_attempt(failure: str) -> None:
    error = {
        "cancel": RunCancelled("installation_unavailable"),
        "deadline": RunFailure("deadline_exceeded"),
        "policy": PolicyError(PolicyErrorCode.ACCESS_DENIED, "GitHub answered 403"),
    }[failure]
    trace = Trace()
    gate, _ = _failing_gate(error)

    with pytest.raises(type(error)) as caught:
        asyncio.run(_stage(trace, rag=gate).run(_RUN))

    assert caught.value is error
    assert [step[0] for step in trace.steps] == ["policy.load"]


@pytest.fixture
def capable(monkeypatch: pytest.MonkeyPatch) -> None:
    """The pinned review prompt of the run declares a retrieved section."""
    monkeypatch.setattr(rag_index, "RETRIEVED_PROMPT_VERSIONS", frozenset({_PINNED}))


@dataclass
class FailingGraph:
    error: Exception
    reads: int = 0

    async def read_hints(self, repository_id: UUID, targets: Sequence[GraphTarget]) -> GraphHints:
        self.reads += 1
        raise self.error


@pytest.mark.usefixtures("capable")
@pytest.mark.parametrize("status", _USABLE, ids=["ready", "stale"])
def test_a_prompt_with_a_retrieved_section_records_what_the_rag_slot_used(
    status: RagIndexStatus,
) -> None:
    trace = Trace()
    gate, _ = _gate(_readiness(status))
    graph = _greet_hints("src/main.py", "src/gone.py")

    payload = asyncio.run(_stage(trace, rag=gate, context_builder=_rag_builder(graph)).run(_RUN))

    assert payload is not None and payload.status is ContextStatus.REVIEW
    (block,) = payload.retrieved
    assert (block.source.path, block.source.revision) == ("src/main.py", _HEAD)
    assert [read[1] for read in graph.reads] == [(GraphTarget("src/app.py", "greet"),)]
    assert [step[0] for step in trace.steps] == ["policy.load", "context.build", "context.rag"]
    # The request is what the gate read; the response counts what the slot did.
    assert trace.steps[2][1] == {
        "index_status": status.value,
        "indexed_commit_sha": _INDEXED,
        "default_head_sha": _TARGET,
        "stale": status is RagIndexStatus.STALE,
    }
    assert trace.steps[2][2] == {
        "outcome": "used",
        "candidates": {"found": 2, "confirmed": 1, "dropped": {"rag_candidate_missing": 1}},
        "tokens": block.tokens,
    }
    assert "src/" not in repr(trace.steps[2])
    # The context step lists the retrieved block by its side and revision, not its text.
    assert [item for item in trace.steps[1][2]["blocks"] if item["level"] == "RAG"] == [
        {
            "level": "RAG",
            "role": "retrieved_excerpt",
            "path": "src/main.py",
            "side": "RIGHT",
            "revision": _HEAD,
            "ranges": [[1, 1], [4, 5]],
            "representation": "exact_lines",
            "tokens": block.tokens,
            "reason": "rag_reverse_edge",
        }
    ]
    assert 'greet("octo")' not in repr(trace.steps)


def test_without_a_retrieved_section_a_usable_index_is_not_read() -> None:
    trace = Trace()
    gate, access = _gate(_readiness(RagIndexStatus.READY))
    graph = _greet_hints("src/main.py")

    payload = asyncio.run(_stage(trace, rag=gate, context_builder=_rag_builder(graph)).run(_RUN))

    assert payload is not None and (payload.retrieved, payload.rag) == ((), None)
    assert access.opened == [_REPOSITORY]
    assert graph.reads == []
    assert trace.steps[2][2] == {"outcome": "not_ready", "reason": "prompt_version"}


@pytest.mark.usefixtures("capable")
@pytest.mark.parametrize(
    ("status", "response"),
    [
        (IndexStatus.EMPTY, {"outcome": "not_ready", "reason": "empty"}),
        (IndexStatus.BUILDING, {"outcome": "not_ready", "reason": "building"}),
        (IndexStatus.FAILED, {"outcome": "not_ready", "reason": "failed", "error_code": None}),
    ],
    ids=["empty", "building", "failed"],
)
def test_an_index_no_longer_ready_at_the_graph_read_is_recorded_with_its_fresh_status(
    status: IndexStatus, response: dict[str, str | None]
) -> None:
    trace = Trace()
    gate, _ = _gate(_readiness(RagIndexStatus.READY))
    graph = _greet_hints("src/main.py", status=status)

    payload = asyncio.run(_stage(trace, rag=gate, context_builder=_rag_builder(graph)).run(_RUN))

    assert payload is not None and payload.retrieved == ()
    assert len(graph.reads) == 1
    # The request stays what the gate read: the index was ready then.
    assert trace.steps[2][1]["index_status"] == "ready"
    assert trace.steps[2][2] == response


@pytest.mark.usefixtures("capable")
def test_a_failing_rag_slot_is_recorded_and_the_other_levels_stay() -> None:
    graph = FailingGraph(RuntimeError("graph unavailable"))
    without = asyncio.run(_stage(Trace(), context_builder=_rag_builder(graph)).run(_RUN))
    trace = Trace()
    gate, _ = _gate(_readiness(RagIndexStatus.READY))

    payload = asyncio.run(_stage(trace, rag=gate, context_builder=_rag_builder(graph)).run(_RUN))

    assert without is not None and payload is not None
    assert graph.reads == 1
    assert (payload.blocks, payload.omissions, payload.retrieved) == (
        without.blocks,
        without.omissions,
        (),
    )
    assert [step[0] for step in trace.steps] == ["policy.load", "context.build", "context.rag"]
    assert trace.steps[2][1]["index_status"] == "ready"
    assert trace.steps[2][2] == {"error": {"type": "RuntimeError", "message": "graph unavailable"}}


@pytest.mark.usefixtures("capable")
def test_a_build_that_returns_before_the_rag_slot_records_its_reason_and_zero_counters() -> None:
    trace = Trace()
    gate, _ = _gate(_readiness(RagIndexStatus.READY))
    graph = _greet_hints("src/main.py")
    repository = RagRepository(ConnectionError("provider unavailable"))

    payload = asyncio.run(
        _stage(trace, rag=gate, context_builder=_rag_builder(graph, repository)).run(_RUN)
    )

    assert payload is not None and payload.retrieved == ()
    assert graph.reads == []
    assert trace.steps[2][2] == {
        "outcome": "used",
        "reason": "tree_unavailable",
        "candidates": {"found": 0, "confirmed": 0, "dropped": {}},
        "tokens": 0,
    }


@pytest.mark.usefixtures("capable")
def test_a_caught_build_failure_records_the_gate_without_rag_counters() -> None:
    plans: list[object] = []

    class Broken:
        async def execute(self, **kwargs: object) -> object:
            plans.append(kwargs["rag"])
            raise RuntimeError("the parser is gone")

    trace = Trace()
    gate, _ = _gate(_readiness(RagIndexStatus.STALE))
    stage = _stage(trace, rag=gate)
    stage._context_builder = lambda run: Broken()  # type: ignore[assignment,return-value]

    assert asyncio.run(stage.run(_RUN)) is None

    assert plans == [RagPlan()]
    assert [step[0] for step in trace.steps] == ["policy.load", "context.build", "context.rag"]
    assert trace.steps[2][1] == {
        "index_status": "stale",
        "indexed_commit_sha": _INDEXED,
        "default_head_sha": _TARGET,
        "stale": True,
    }
    # The gate let RAG in; the slot did not complete, so nothing is counted.
    assert trace.steps[2][2] == {"outcome": "used", "reason": "context_build_failed"}


def test_context_trace_lists_retrieved_blocks_with_their_side_and_revision() -> None:
    diff_map = DiffMap(
        schema_version=1,
        parser_version="unified-hunks/v1",
        provider=VcsProviderName.GITHUB,
        repository_id=UUID(int=5),
        code_change_number=7,
        policy_base_sha=_BASE,
        head_sha=_HEAD,
        diff_old_sha="c" * 40,
        file_list_complete=True,
        incomplete_reasons=(),
        files=(),
        digest="d" * 64,
    )

    def retrieved(
        side: DiffSide, revision: str, representation: Representation, reason: str
    ) -> RetrievedBlock:
        source = SourceRef(
            SourceRole.RETRIEVED_EXCERPT,
            UUID(int=5),
            revision,
            "src/main.py",
            "f" * 40,
            (),
            side,
            (LineRange(1, 1), LineRange(4, 5)),
            reason,
        )
        return RetrievedBlock(
            "RAG:src/main.py:1-1,4-5",
            ContextLevel.RAG,
            source,
            representation,
            "def main():",
            7,
            EdgeKind.USES_IMPORTED,
            _INDEXED,
        )

    payload = ReviewContextPayload(
        status=ContextStatus.REVIEW,
        reason=None,
        policy_snapshot_id=UUID(int=9),
        policy_digest="p" * 64,
        diff_digest="d" * 64,
        blocks=(_block("b1", ContextLevel.L1, SourceRole.DIFF, 40),),
        omissions=(),
        imports=(),
        levels_present=(ContextLevel.L1, ContextLevel.RAG),
        reviewable_lines=1,
        ignored_files=0,
        tokens_used=54,
        retrieved=(
            retrieved(DiffSide.RIGHT, _HEAD, Representation.EXACT_LINES, "rag_broken_reference"),
            # The definition a broken reference lost, at the old side.
            retrieved(DiffSide.LEFT, "c" * 40, Representation.SIGNATURE, "rag_broken_reference"),
        ),
    )

    summary = context_summary(diff_map, payload)

    assert [
        (item["level"], item["side"], item.get("revision"), item["representation"])
        for item in summary["blocks"]
    ] == [
        ("L1", "RIGHT", None, "exact_lines"),
        ("RAG", "RIGHT", _HEAD, "exact_lines"),
        ("RAG", "LEFT", "c" * 40, "signature"),
    ]
    assert summary["blocks"][2] == {
        "level": "RAG",
        "role": "retrieved_excerpt",
        "path": "src/main.py",
        "side": "LEFT",
        "revision": "c" * 40,
        "ranges": [[1, 1], [4, 5]],
        "representation": "signature",
        "tokens": 7,
        "reason": "rag_broken_reference",
    }
    assert "def main" not in repr(summary)
