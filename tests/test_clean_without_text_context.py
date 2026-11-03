"""Effective exclusions and durable coverage agree before the first model call."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from uuid import UUID

import pytest

from app.modules.reviews.application.get_run_diff import DiffSnapshot
from app.modules.reviews.application.prepare_policy_snapshot import PreparePolicySnapshot
from app.modules.reviews.application.process_run import RunVcsInput
from app.modules.reviews.application.review_context import (
    BuildReviewContext,
    ContextStatus,
    SummaryOnlyReview,
)
from app.modules.reviews.application.review_context_stage import (
    EstimatedTokenCounter,
    ReviewContextStage,
)
from app.modules.reviews.application.run_failures import RunSkipped
from tests.test_prepare_policy_snapshot import Store, Uow
from tests.test_review_context_stage import Parser, Policy, Repository, Runs, Trace, Vcs
from tests.test_run_coverage import code_snapshot, empty_snapshot

_RUN = UUID(int=2)


async def checkpoint() -> None:
    return None


def stage_for(
    snapshots: list[DiffSnapshot],
    *,
    expected: int | None = None,
    rules: bytes | None = None,
    fail_build: bool = False,
) -> tuple[ReviewContextStage, Trace]:
    trace = Trace()
    store = Store()
    vcs = Vcs(files_changed=len(snapshots) if expected is None else expected)

    def builder(run: RunVcsInput) -> BuildReviewContext:
        if fail_build:
            raise RuntimeError("optional context enrichment failed")
        return BuildReviewContext(
            source=Repository(), parser=Parser(), counter=EstimatedTokenCounter()
        )

    stage = ReviewContextStage(
        runs=Runs(snapshots),
        checkpoint=checkpoint,
        vcs=vcs,
        merge_base=vcs,
        prepare_policy=PreparePolicySnapshot(
            uow_factory=lambda: Uow(store), source_factory=lambda pins: Policy(rules=rules)
        ),
        context_builder=builder,
        trace=trace,
    )
    return stage, trace


def test_stage_omits_empty_addition_and_records_complete_policy_effective_coverage() -> None:
    stage, trace = stage_for([code_snapshot(), empty_snapshot()])
    payload = asyncio.run(stage.run(_RUN))

    assert payload is not None and payload.status is ContextStatus.REVIEW
    assert stage.excluded("pkg/__init__.py") is True
    assert all(block.source.path != "pkg/__init__.py" for block in payload.blocks)
    result = trace.steps[-1][2]["coverage"]
    assert result["status"] == "complete"
    assert result["reviewed_files"] == 1
    assert result["exclusion_counts"] == {"empty_file": 1}
    assert result["head_sha"] == "a" * 40
    assert result["diff_digest"] == trace.steps[-1][2]["diff"]["digest"]


@pytest.mark.parametrize("reason", ["empty_file", "binary"])
def test_exclusion_only_stage_skips_before_any_model_input(reason: str) -> None:
    snapshot = (
        empty_snapshot()
        if reason == "empty_file"
        else DiffSnapshot("assets/a.png", None, omission_reason="binary")
    )
    stage, trace = stage_for([snapshot])

    with pytest.raises(RunSkipped, match="all_changes_ignored"):
        asyncio.run(stage.run(_RUN))

    assert trace.steps[-1][2]["coverage"]["status"] == "none"
    assert trace.steps[-1][2]["blocks"] == []


def test_custom_ignored_missing_patch_is_excluded_in_durable_coverage() -> None:
    missing = DiffSnapshot("private/secret.py", None, omission_reason="missing_patch")
    stage, trace = stage_for(
        [code_snapshot(), missing], rules=b"```review-ignore\nprivate/**\n```\n"
    )
    asyncio.run(stage.run(_RUN))

    result = trace.steps[-1][2]["coverage"]
    assert result["status"] == "complete"
    assert result["exclusion_counts"] == {"ignored": 1}
    assert result["reason_counts"] == {}
    assert "private/secret.py" not in repr(result)


def test_empty_marker_cannot_skip_invalid_identity_or_incomplete_inventory() -> None:
    invalid = replace(empty_snapshot(), filename="../escape.py")
    stage, trace = stage_for([invalid])
    asyncio.run(stage.run(_RUN))
    assert trace.steps[-1][2]["coverage"]["status"] == "partial"
    assert trace.steps[-1][2]["coverage"]["reason_counts"] == {"unsupported_path": 1}

    stage, trace = stage_for([empty_snapshot()], expected=2)
    with pytest.raises(SummaryOnlyReview):
        asyncio.run(stage.run(_RUN))
    assert trace.steps[-1][2]["status"] == "summary_only"
    assert trace.steps[-1][2]["reason"] == "diff_inventory_incomplete"
    assert trace.steps[-1][2]["coverage"]["status"] == "partial"
    assert trace.steps[-1][2]["coverage"]["reason_counts"] == {"diff_inventory_incomplete": 1}


def test_optional_builder_failure_preserves_effective_policy_coverage_for_finalization() -> None:
    stage, trace = stage_for([code_snapshot(), empty_snapshot()], fail_build=True)
    assert asyncio.run(stage.run(_RUN)) is None
    assert trace.steps[-1][2]["error"]["type"] == "RuntimeError"
    assert trace.steps[-1][2]["coverage"]["status"] == "complete"
    assert trace.steps[-1][2]["coverage"]["exclusion_counts"] == {"empty_file": 1}
