"""A run reviews the diff of its pinned base, also after the PR was retargeted (#168, part 3)."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field, replace
from typing import Any
from uuid import UUID

import pytest

from app.modules.reviews.application.check_runs import RunProviderCheckpoint, failure_reason
from app.modules.reviews.application.diff_map import DiffMap, VcsProviderName, build_diff_map
from app.modules.reviews.application.post_processing_diff_map import diff_map_from_snapshots
from app.modules.reviews.application.review_context import (
    BuildReviewContext,
    ReviewContextPayload,
    SummaryOnlyReview,
)
from app.modules.reviews.application.review_context_stage import (
    EstimatedTokenCounter,
    vcs_file_from_snapshot,
)
from app.modules.reviews.application.run_failures import (
    KNOWN_ERROR_CODES,
    RETRYABLE_ERROR_CODES,
    RunFailure,
    RunSkipped,
    classify_failure,
)
from app.modules.reviews.application.vcs_diff import (
    FetchVcsReviewInput,
    PullRequestLocator,
    VcsPullRequest,
)
from tests.test_review_context_stage import (
    _HEAD,
    _LOCATOR,
    _RUN,
    Parser,
    Repository,
    Trace,
    Vcs,
    _checkpoint,
    _snapshots,
    _stage,
)
from tests.test_run_coverage import code_snapshot, empty_snapshot

_STAGE_LOGGER = "app.modules.reviews.application.review_context_stage"


@dataclass
class Retargeted(Vcs):
    """GitHub reports another base than the one the run pinned (``Run.base_sha`` = ``b``)."""

    live_base: str = "e" * 40
    merge_base_requests: list[str] = field(default_factory=list)

    async def get_pull_request(
        self, locator: PullRequestLocator, *, checkpoint: RunProviderCheckpoint
    ) -> VcsPullRequest:
        pull_request = await super().get_pull_request(locator, checkpoint=checkpoint)
        return replace(pull_request, base_sha=self.live_base)

    async def get_merge_base(
        self, pull_request: VcsPullRequest, *, checkpoint: RunProviderCheckpoint
    ) -> str:
        self.merge_base_requests.append(pull_request.base_sha)
        return await super().get_merge_base(pull_request, checkpoint=checkpoint)


class CapturingBuild(BuildReviewContext):
    """The real context build; it keeps every diff map the stage hands it."""

    def __init__(self, diff_maps: list[DiffMap]) -> None:
        super().__init__(source=Repository(), parser=Parser(), counter=EstimatedTokenCounter())
        self._diff_maps = diff_maps

    async def execute(self, *, diff_map: DiffMap, **kwargs: Any) -> ReviewContextPayload:
        self._diff_maps.append(diff_map)
        return await super().execute(diff_map=diff_map, **kwargs)


def _run(vcs: Vcs) -> tuple[Trace, DiffMap | None]:
    """The stage's diff map, or None when an incomplete list stops before the builder.

    #313 records ``context.build`` and raises ``SummaryOnlyReview`` before RAG and the
    context build. A complete list still hands the map to the builder.
    """
    trace = Trace()
    diff_maps: list[DiffMap] = []
    stage = _stage(trace, vcs=vcs, context_builder=lambda run: CapturingBuild(diff_maps))
    try:
        assert asyncio.run(stage.run(_RUN)) is not None
    except SummaryOnlyReview:
        assert diff_maps == []
        return trace, None
    (diff_map,) = diff_maps
    return trace, diff_map


def _contexts(trace: Trace) -> list[dict[str, Any]]:
    return [response for tool, _, response in trace.steps if tool == "context.build"]


def _stage_warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == _STAGE_LOGGER and record.levelno == logging.WARNING
    ]


_COUNTS = pytest.mark.parametrize(
    ("files_changed", "complete", "reasons"),
    [(2, True, ()), (5, False, ("file_count_mismatch",))],
    ids=["count-matches", "count-differs"],
)


@_COUNTS
def test_a_retargeted_pr_is_mapped_on_the_pinned_run_base(
    files_changed: int,
    complete: bool,
    reasons: tuple[str, ...],
    caplog: pytest.LogCaptureFixture,
) -> None:
    vcs = Retargeted(files_changed=files_changed)

    with caplog.at_level(logging.WARNING, logger=_STAGE_LOGGER):
        trace, diff_map = _run(vcs)

    assert vcs.merge_base_requests == ["b" * 40]
    (context,) = _contexts(trace)
    assert context["diff"]["diff_old_sha"] == "c" * 40
    # The provider counter stays live: the pinned-base list is compared with it.
    assert context["diff"]["file_list_complete"] is complete
    assert context["diff"]["pr_base_sha"] == "e" * 40
    if complete:
        assert diff_map is not None
        assert diff_map.policy_base_sha == "b" * 40
        assert diff_map.diff_old_sha == "c" * 40
        assert diff_map.file_list_complete is True
        assert diff_map.incomplete_reasons == reasons
    else:
        # An incomplete list is summary-only before the builder (#313).
        assert diff_map is None
        assert context["status"] == "summary_only"
        assert context["reason"] == "diff_inventory_incomplete"
    assert _stage_warnings(caplog) == [
        "Pull request base differs from the pinned run base "
        f"run_id={_RUN} pinned_base_sha={'b' * 40} pr_base_sha={'e' * 40}"
    ]


@_COUNTS
def test_the_live_base_stays_out_of_the_diff_map_and_its_digest(
    files_changed: int, complete: bool, reasons: tuple[str, ...]
) -> None:
    trace, retargeted = _run(Retargeted(files_changed=files_changed))
    control_trace, control = _run(Vcs(files_changed=files_changed))

    # The live base is recorded beside the map. It is not part of the digest.
    (context,) = _contexts(trace)
    (control_context,) = _contexts(control_trace)
    assert context["diff"]["pr_base_sha"] == "e" * 40
    assert "pr_base_sha" not in control_context["diff"]
    assert context["diff"]["digest"] == control_context["diff"]["digest"]
    if complete:
        assert retargeted == control
        assert retargeted is not None
        assert retargeted.digest == control_context["diff"]["digest"]
    else:
        assert retargeted is None and control is None
        assert context["reason"] == "diff_inventory_incomplete"


@_COUNTS
def test_a_rebuild_from_the_run_base_and_the_trace_gives_the_stage_digest(
    files_changed: int, complete: bool, reasons: tuple[str, ...]
) -> None:
    trace, _ = _run(Retargeted(files_changed=files_changed))
    (context,) = _contexts(trace)

    # The form of the coverage restore (#283): Run.base_sha, the traced old side and
    # ``files_changed = len(durable) + (not complete)``.
    traced_complete = context["diff"]["file_list_complete"]
    assert traced_complete is complete
    stored = _snapshots()
    pull_request = asyncio.run(
        Vcs(files_changed=len(stored) + (not traced_complete)).get_pull_request(
            _LOCATOR, checkpoint=_checkpoint
        )
    )
    rebuilt = build_diff_map(
        replace(pull_request, base_sha="b" * 40),
        tuple(vcs_file_from_snapshot(item) for item in stored),
        provider=VcsProviderName.GITHUB,
        repository_id=UUID(int=1),
        diff_old_sha=context["diff"]["diff_old_sha"],
    )

    assert rebuilt.digest == context["diff"]["digest"]


def test_a_pr_on_its_pinned_base_records_no_live_base(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger=_STAGE_LOGGER):
        trace, diff_map = _run(Vcs())

    assert diff_map is not None
    assert diff_map.policy_base_sha == "b" * 40
    (context,) = _contexts(trace)
    assert "pr_base_sha" not in context["diff"]
    assert _stage_warnings(caplog) == []


@pytest.mark.parametrize(
    ("vcs", "diff"),
    [
        (Retargeted(files_changed=0), {"digest": None, "files": 0, "pr_base_sha": "e" * 40}),
        (Vcs(files_changed=0), {"digest": None, "files": 0}),
    ],
    ids=["retargeted", "pinned-base"],
)
def test_a_pr_without_changes_records_its_live_base_only_after_a_retarget(
    vcs: Vcs, diff: dict[str, Any]
) -> None:
    trace = Trace()
    stage = _stage(trace, vcs=vcs, empty=True)

    with pytest.raises(RunSkipped, match="no_changes"):
        asyncio.run(stage.run(_RUN))

    (context,) = _contexts(trace)
    assert context["diff"] == diff


def test_the_live_base_of_an_attempt_is_not_carried_into_the_next_one() -> None:
    vcs = Retargeted()
    trace = Trace()
    stage = _stage(trace, vcs=vcs)

    assert asyncio.run(stage.run(_RUN)) is not None
    vcs.live_base = "b" * 40
    assert asyncio.run(stage.run(_RUN)) is not None

    first, second = _contexts(trace)
    assert first["diff"]["pr_base_sha"] == "e" * 40
    assert "pr_base_sha" not in second["diff"]


@dataclass
class Fetching(Retargeted):
    """A provider whose file list counts its reads; the diff itself is empty."""

    diff_reads: int = 0

    async def get_diff(
        self, pull_request: VcsPullRequest, *, checkpoint: RunProviderCheckpoint
    ) -> tuple[Any, ...]:
        self.diff_reads += 1
        return ()


def test_a_pr_retargeted_before_the_diff_is_stored_fails_without_reading_the_diff() -> None:
    vcs = Fetching()
    fetch = FetchVcsReviewInput(vcs, checkpoint=_checkpoint, run_id=_RUN)

    with pytest.raises(RunFailure) as caught:
        asyncio.run(fetch.execute(_LOCATOR, _HEAD, "b" * 40))

    assert caught.value.error_code == "pr_base_changed"
    # The internal message keeps both public SHAs; the author text is the catalog one.
    assert caught.value.message == (
        f"pull request base changed after the run pinned it pinned={'b' * 40} live={'e' * 40}"
    )
    assert classify_failure(caught.value).error_code == "pr_base_changed"
    assert vcs.diff_reads == 0


def test_a_new_head_on_a_retargeted_pr_is_a_head_change_at_fetch() -> None:
    vcs = Fetching(head_sha="f" * 40)

    # The head is checked first: the attempt is retried and the webhook supersedes the run.
    with pytest.raises(ValueError, match="head SHA changed"):
        asyncio.run(
            FetchVcsReviewInput(vcs, checkpoint=_checkpoint).execute(_LOCATOR, _HEAD, "b" * 40)
        )

    assert vcs.diff_reads == 0


def test_a_new_head_on_a_retargeted_pr_fails_the_stage_as_a_head_change(
    caplog: pytest.LogCaptureFixture,
) -> None:
    trace = Trace()
    stage = _stage(trace, vcs=Retargeted(head_sha="f" * 40))

    with (
        caplog.at_level(logging.WARNING, logger=_STAGE_LOGGER),
        pytest.raises(RunFailure) as caught,
    ):
        asyncio.run(stage.run(_RUN))

    assert caught.value.error_code == "diff_fetch_failed"
    assert _contexts(trace) == []
    assert _stage_warnings(caplog) == []


def test_a_pr_on_its_pinned_base_reads_the_diff() -> None:
    vcs = Fetching(live_base="b" * 40)

    fetched = asyncio.run(
        FetchVcsReviewInput(vcs, checkpoint=_checkpoint).execute(_LOCATOR, _HEAD, "b" * 40)
    )

    assert fetched.pull_request.base_sha == "b" * 40
    assert vcs.diff_reads == 1


def test_a_changed_pr_base_is_terminal_and_tells_the_author_to_rerun() -> None:
    assert "pr_base_changed" in KNOWN_ERROR_CODES
    assert "pr_base_changed" not in RETRYABLE_ERROR_CODES
    assert (
        failure_reason("pr_base_changed")
        == "Целевая ветка PR изменилась после запуска ревью. Перезапустите ревью из UI."
    )


def test_the_publication_map_is_built_on_the_run_base() -> None:
    diff_map = diff_map_from_snapshots(
        repository_id=UUID(int=1),
        code_change_number=7,
        head_sha=_HEAD,
        base_sha="b" * 40,
        diff_old_sha="c" * 40,
        snapshots=_snapshots(),
    )

    assert diff_map.policy_base_sha == "b" * 40


@pytest.mark.parametrize("retargeted", [False, True])
def test_optional_context_failure_keeps_the_retarget_only_when_it_exists(
    retargeted: bool,
) -> None:
    trace = Trace()

    def broken_builder(run: object) -> BuildReviewContext:
        raise RuntimeError("optional context enrichment failed")

    stage = _stage(
        trace,
        vcs=Retargeted() if retargeted else Vcs(),
        snapshots=[code_snapshot(), empty_snapshot()],
        context_builder=broken_builder,
    )
    assert asyncio.run(stage.run(_RUN)) is None
    (context,) = _contexts(trace)
    assert context["error"] == {
        "type": "RuntimeError",
        "message": "optional context enrichment failed",
    }
    assert context["diff"]["diff_old_sha"] == "c" * 40
    assert context["coverage"]["status"] == "complete"
    assert context["coverage"]["reviewed_files"] == 1
    assert context["coverage"]["exclusion_counts"] == {"empty_file": 1}
    assert context["coverage"]["reason_counts"] == {}
    if retargeted:
        assert context["diff"]["pr_base_sha"] == "e" * 40
    else:
        assert "pr_base_sha" not in context["diff"]
