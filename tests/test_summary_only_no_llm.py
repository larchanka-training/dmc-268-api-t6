"""Summary-only runs finish with no model call (#313, plan D-6).

``ExecuteReviewRun`` is the worker entry after claim. Conventions are a cache miss:
the generator records ``llm.call`` and ``llm.repo_conventions`` when that miss reaches
the model. The review model records ``llm.call`` and ``llm.review_output``, and the
verifier records ``llm.call`` and ``llm.verify``, if those paths run.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from types import TracebackType
from typing import Any, Self
from uuid import UUID

from app.modules.reviews.application.check_runs import (
    CheckRunReport,
    CheckRunTarget,
    CheckRunView,
    RunProviderCheckpoint,
    check_run_view,
)
from app.modules.reviews.application.conventions import (
    ActiveConventionsPrompt,
    CachedConventions,
    ConventionsFile,
    ConventionsRequest,
    GeneratedConventions,
    GenerateRepoConventions,
    PolicyFilteredConventionsSource,
    RepositoryFile,
    RepositorySnapshot,
)
from app.modules.reviews.application.execute_review import ExecuteReviewRun, ReviewPromptInput
from app.modules.reviews.application.findings_post_processor import summary_from_result
from app.modules.reviews.application.get_run_diff import DiffSnapshot, review_files_from_snapshots
from app.modules.reviews.application.handle_review_run import ClaimedAttempt, DeliveryOutcome
from app.modules.reviews.application.policy_snapshot import CallManifest
from app.modules.reviews.application.prepare_policy_snapshot import PreparePolicySnapshot
from app.modules.reviews.application.process_run import (
    ReviewRunProcessor,
    RunConventionsInput,
    RunVcsInput,
)
from app.modules.reviews.application.prompt_builder import (
    PullRequestMeta,
    RepoConventions,
    ReviewContext,
)
from app.modules.reviews.application.reflection_pass import ReflectionPass
from app.modules.reviews.application.review_context import BuildReviewContext
from app.modules.reviews.application.review_context_stage import (
    EstimatedTokenCounter,
    ReviewContextStage,
)
from app.modules.reviews.application.run_coverage import CoverageItem, CoverageStatus, RunCoverage
from app.modules.reviews.application.run_failures import RunSkipped
from app.modules.reviews.application.store_review_output import NoModelAnswer
from app.modules.reviews.application.vcs_diff import OmissionReason, VcsFile, VcsPullRequest
from tests.test_prepare_policy_snapshot import Store, Uow
from tests.test_review_context_stage import (
    _BASE,
    _HEAD,
    _LOCATOR,
    _RUN,
    _SOURCE,
    Parser,
    Policy,
    Repository,
    Trace,
    Vcs,
    _checkpoint,
)
from tests.test_review_run_worker import RUN as _WORKER_RUN
from tests.test_review_run_worker import CheckRuns, FakeRun, handler
from tests.trigger_uow import processing_uow

_CODE_CHANGE = UUID(int=5)
_REPOSITORY = UUID(int=1)
_TEXT_BLOB = "d" * 40
_PNG_BLOB = "9" * 40
_PNG = b"\x89PNG\r\n\x1a\n"
_MODEL_TOOLS = frozenset({"llm.call", "llm.repo_conventions", "llm.review_output", "llm.verify"})
_PATTERNS = ("Pattern one.", "Pattern two.", "Pattern three.")
# Plan D-5, copied here so a wrong production sentence fails the test.
_DIFF_TOO_LARGE = "Построчное AI-ревью не выполнено: превышен лимит diff"
_DIFF_INVENTORY_INCOMPLETE = "Построчное AI-ревью не выполнено: список изменённых файлов неполон"
_NO_REVIEWABLE_FILES = "Построчное AI-ревью не выполнено: нет проверяемых файлов"
_SENTENCES = {
    "diff_too_large": _DIFF_TOO_LARGE,
    "diff_inventory_incomplete": _DIFF_INVENTORY_INCOMPLETE,
    "no_reviewable_files": _NO_REVIEWABLE_FILES,
}
_RECOMMENDATIONS = (
    "Recommendation one (from: standard/correctness)",
    "Recommendation two (from: standard/correctness)",
    "Recommendation three (from: standard/correctness)",
    "Recommendation four (from: standard/correctness)",
    "Recommendation five (from: standard/correctness)",
)


def _added_patch(lines: int) -> str:
    """A complete added-file patch of exactly ``lines`` lines, matching the VCS counters."""
    body = "".join(f"+x{number}\n" for number in range(lines))
    return f"@@ -0,0 +1,{lines} @@\n{body}"


def _text(lines: int) -> VcsFile:
    """One allowed text file whose VCS counters are ``lines`` added lines."""
    return VcsFile("src/app.py", "added", _TEXT_BLOB, None, lines, 0, lines, _added_patch(lines))


@dataclass
class GitHub(Vcs):
    files: tuple[VcsFile, ...] = ()
    blobs: dict[str, bytes] = field(default_factory=dict)

    async def get_diff(
        self, pull_request: VcsPullRequest, *, checkpoint: RunProviderCheckpoint
    ) -> tuple[VcsFile, ...]:
        await checkpoint()
        return self.files

    async def get_blob(
        self, locator: object, sha: str, *, checkpoint: RunProviderCheckpoint
    ) -> bytes:
        await checkpoint()
        return self.blobs[sha]


@dataclass
class Runs:
    """One run: the processor stores the fetcher's snapshot and the stage reads it back."""

    stored: list[DiffSnapshot] | None = None

    async def get_run_diff_input(self, run_id: UUID) -> None:
        raise AssertionError("the VCS path reads the run locator")

    async def get_run_vcs_input(self, run_id: UUID) -> RunVcsInput:
        return RunVcsInput(_CODE_CHANGE, _REPOSITORY, _HEAD, _BASE, _LOCATOR)

    async def get_run_snapshots(self, run_id: UUID) -> list[DiffSnapshot] | None:
        return self.stored

    async def store_diff_snapshots(
        self, run_id: UUID, code_change_id: UUID, head_sha: str, snapshots: list[DiffSnapshot]
    ) -> list[DiffSnapshot]:
        self.stored = snapshots
        return snapshots

    async def get_run_conventions_input(self, run_id: UUID) -> RunConventionsInput:
        return RunConventionsInput(_REPOSITORY, ActiveConventionsPrompt(UUID(int=7), "conventions"))


class ConventionsSource:
    async def fetch_agents_md(self, repository_id: UUID) -> RepositorySnapshot:
        return RepositorySnapshot(content=None, sha=None)

    async def fetch_tree(self, repository_id: UUID) -> tuple[RepositoryFile, ...]:
        return (RepositoryFile("src/app.py", len(_SOURCE)),)

    async def fetch_files(
        self, repository_id: UUID, paths: tuple[str, ...]
    ) -> tuple[RepositoryFile, ...]:
        return tuple(
            RepositoryFile(path, len(_SOURCE), _SOURCE) for path in paths if path == "src/app.py"
        )


class ConventionsStore:
    """A cache miss: nothing is stored, so the generator must call the model."""

    def __init__(self, trace: Trace) -> None:
        self._trace = trace

    async def get(
        self,
        repository_id: UUID,
        agents_md_sha: str | None,
        prompt_version_id: UUID,
        policy_key: str | None = None,
    ) -> CachedConventions | None:
        return None

    async def save_and_record_trace(
        self,
        run_id: UUID,
        conventions: CachedConventions,
        trace_files: tuple[ConventionsFile, ...],
    ) -> CachedConventions:
        await _note(self._trace, "llm.repo_conventions", {})
        return conventions


@dataclass
class ConventionsUow:
    conventions: ConventionsStore

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None

    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        return None


@dataclass
class ConventionsModel:
    trace: Trace

    async def draft_conventions(self, *, request: ConventionsRequest) -> dict[str, object]:
        await _note(self.trace, "llm.call", {"operation": "conventions"})
        return {
            "files": [{"path": path, "relevance": "Changed."} for path in request.traced_files],
            "key_patterns": list(_PATTERNS),
            "recommendations": list(_RECOMMENDATIONS),
        }


@dataclass
class Prompts:
    runs: Runs

    async def get_review_prompt_input(
        self, run_id: UUID, conventions: GeneratedConventions
    ) -> ReviewPromptInput:
        assert self.runs.stored is not None
        changed, omitted = review_files_from_snapshots(self.runs.stored)
        return ReviewPromptInput(
            system="system",
            rules=(),
            agents_md=conventions.agents_md,
            conventions=RepoConventions(
                conventions.conventions.key_patterns, conventions.conventions.recommendations
            ),
            changed_files=changed,
            omitted_files=omitted,
        )


@dataclass
class Model:
    trace: Trace
    files_changed: int

    async def get_pull_request_meta(self, run_id: UUID) -> PullRequestMeta:
        return PullRequestMeta(
            "t", None, "octo", "f", "main", (), self.files_changed, 0, 0, False, False
        )

    async def draft_review(self, *, context: ReviewContext) -> dict[str, object]:
        await _note(self.trace, "llm.call", {"operation": "review"})
        await _note(self.trace, "llm.review_output", {})
        return {"findings": []}


class Verifier:
    def __init__(self, trace: Trace) -> None:
        self._trace = trace

    def max_prompt_tokens(self) -> int:
        return 100_000

    async def verify(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        manifest: CallManifest,
        context: ReviewContext,
        **_: object,
    ) -> dict[str, Any]:
        await _note(
            self._trace,
            "llm.call",
            {
                "operation": "verify",
                "stage": manifest.stage,
                "prompt": bool(system_prompt and user_prompt),
                "files": len(context.changed_files),
            },
        )
        await _note(self._trace, "llm.verify", {})
        return {"decisions": []}


class DiffFlag:
    """The context.build payload write also marks the run's diff rows summary-only."""

    def __init__(self, runs: Runs) -> None:
        self._runs = runs

    async def save_context_payload(
        self, run_id: UUID, summary: dict[str, Any], *, mark_summary_only: bool = False
    ) -> None:
        if not mark_summary_only or self._runs.stored is None:
            return
        self._runs.stored = [
            replace(row, summary_only=True, review_patch=None) for row in self._runs.stored
        ]


@dataclass
class Publisher:
    published: list[object] = field(default_factory=list)

    async def execute(self, run_id: UUID, model_output: object) -> bool:
        self.published.append(model_output)
        return True


@dataclass
class Outcome:
    result: bool | None
    skipped: RunSkipped | None
    trace: Trace
    stored: list[DiffSnapshot]
    published: list[object]
    check_run: CheckRunView | None = None


def _run(
    files: tuple[VcsFile, ...],
    *,
    files_changed: int,
    blobs: dict[str, bytes] | None = None,
    attempts: int = 1,
    record_check_run: bool = False,
) -> Outcome:
    trace = Trace()
    runs = Runs()
    github = GitHub(
        files_changed=files_changed,
        files=files,
        blobs={_TEXT_BLOB: _SOURCE.encode(), **(blobs or {})},
    )
    store = Store()
    stage = ReviewContextStage(
        checkpoint=_checkpoint,
        runs=runs,
        vcs=github,
        merge_base=github,
        prepare_policy=PreparePolicySnapshot(
            uow_factory=lambda: Uow(store), source_factory=lambda pins: Policy()
        ),
        context_builder=lambda run: BuildReviewContext(
            source=Repository(), parser=Parser(), counter=EstimatedTokenCounter()
        ),
        trace=trace,
        payloads=DiffFlag(runs),
    )

    processor = ReviewRunProcessor(
        runs,
        None,
        conventions=GenerateRepoConventions(
            PolicyFilteredConventionsSource(ConventionsSource(), stage.excluded),
            ConventionsModel(trace),
            lambda: ConventionsUow(ConventionsStore(trace)),
        ),
        vcs_provider=github,
        trace=trace,
        uow_factory=processing_uow(runs),
        context_stage=stage,
    )
    publisher = Publisher()
    pipeline = ExecuteReviewRun(
        processor,
        Prompts(runs),
        Model(trace, files_changed),
        publisher,
        context_filter=stage,
        reflection_pass=ReflectionPass(Verifier(trace)),
    )
    if record_check_run:
        recorded = FakeRun()
        gateway = CheckRuns()

        async def attempt(claimed: ClaimedAttempt) -> bool:
            return await pipeline.execute(claimed.run_id)

        delivery = asyncio.run(handler(recorded, attempt, check_runs=gateway).execute(_WORKER_RUN))
        assert delivery is DeliveryOutcome.ACK
        assert runs.stored is not None
        recorded_skip = (
            RunSkipped(recorded.error_code)
            if recorded.state == "skipped" and recorded.error_code is not None
            else None
        )
        return Outcome(
            None,
            recorded_skip,
            trace,
            runs.stored,
            publisher.published,
            gateway.views[-1] if gateway.views else None,
        )
    skipped: RunSkipped | None = None
    result: bool | None = None
    for _ in range(attempts):
        try:
            result = asyncio.run(pipeline.execute(_RUN))
            skipped = None
        except RunSkipped as exc:
            skipped = exc
            result = None
    assert runs.stored is not None
    return Outcome(result, skipped, trace, runs.stored, publisher.published)


async def _note(trace: Trace, tool: str, request: dict[str, Any]) -> None:
    await trace.record(_RUN, tool, request, {}, datetime.now(UTC), 0)


def _context(trace: Trace) -> dict[str, Any]:
    builds = [response for tool, _, response in trace.steps if tool == "context.build"]
    assert len(builds) == 1
    assert isinstance(builds[0], dict)
    return builds[0]


def _calls(trace: Trace) -> list[dict[str, Any]]:
    return [request for tool, request, _ in trace.steps if tool == "llm.call"]


def _model_tools(trace: Trace) -> list[str]:
    return [tool for tool, _, _ in trace.steps if tool in _MODEL_TOOLS]


def _assert_no_model_call(trace: Trace) -> None:
    tools = _model_tools(trace)
    operations = [str(call.get("operation")) for call in _calls(trace)]
    assert tools == [], f"model call {operations} via {tools}"


def _assert_named_summary(reason: str) -> None:
    sentence = _SENTENCES[reason]
    summary = summary_from_result(
        (),
        (),
        RunCoverage(
            CoverageStatus.PARTIAL,
            0,
            reasons=(CoverageItem("src/app.py", "missing_patch"),),
        ),
        summary_reason=reason,
        name_summary_only=True,
    )
    assert summary.problem == f"{sentence}."
    assert summary.effort == "none"
    view = check_run_view(
        CheckRunReport(
            CheckRunTarget(1, "o/r", _HEAD, _RUN),
            "succeeded",
            1,
            verdict=None,
            inline_count=0,
            body_count=0,
            summary_only=True,
            summary_reason=reason,
            coverage_status="partial",
            saved_summary={
                "problem": summary.problem,
                "done_well": summary.done_well,
                "effort": summary.effort,
            },
            run_url="https://app.example/runs/1",
        )
    )
    assert view.title == "AI-ревью: только сводка"
    assert view.conclusion == "neutral"
    assert view.summary.startswith(sentence)
    assert "https://app.example/runs/1" in view.summary
    assert "PR слишком большой" not in view.summary
    if reason != "diff_too_large":
        assert _DIFF_TOO_LARGE not in view.summary
        assert _DIFF_TOO_LARGE not in summary.problem


def _assert_published_without_a_model(published: list[object]) -> None:
    assert len(published) == 1
    assert isinstance(published[0], NoModelAnswer)


def _assert_summary_only_trace(trace: Trace) -> None:
    assert [tool for tool, _, _ in trace.steps] == [
        "policy.load",
        "vcs.fetch_diff",
        "context.build",
    ]


def test_three_thousand_one_allowed_lines_make_no_model_call() -> None:
    outcome = _run((_text(3_001),), files_changed=1)
    build = _context(outcome.trace)

    assert outcome.skipped is None
    assert outcome.result is True
    assert build["diff"]["file_list_complete"] is True
    assert build["status"] == "summary_only"
    assert build["reason"] == "diff_too_large"
    assert build["coverage"]["status"] == "partial"
    assert outcome.stored and all(row.summary_only for row in outcome.stored)
    _assert_summary_only_trace(outcome.trace)
    _assert_no_model_call(outcome.trace)
    _assert_published_without_a_model(outcome.published)
    _assert_named_summary(build["reason"])


def test_incomplete_file_list_is_summary_only_without_a_model_call() -> None:
    outcome = _run((_text(1),), files_changed=2)
    build = _context(outcome.trace)

    assert outcome.skipped is None
    assert outcome.result is True
    assert build["diff"]["file_list_complete"] is False
    assert build["status"] == "summary_only"
    assert build["reason"] == "diff_inventory_incomplete"
    assert build["coverage"]["status"] == "partial"
    assert outcome.stored and all(row.summary_only for row in outcome.stored)
    assert all(row.review_patch is None and row.patch for row in outcome.stored)
    _assert_summary_only_trace(outcome.trace)
    _assert_no_model_call(outcome.trace)
    _assert_published_without_a_model(outcome.published)
    _assert_named_summary(build["reason"])


def test_incomplete_file_list_wins_over_the_line_threshold() -> None:
    outcome = _run((_text(3_001),), files_changed=2)
    build = _context(outcome.trace)

    assert outcome.skipped is None
    assert outcome.result is True
    assert build["diff"]["file_list_complete"] is False
    assert outcome.stored and all(row.summary_only for row in outcome.stored)
    assert build["status"] == "summary_only"
    assert build["reason"] == "diff_inventory_incomplete"
    _assert_summary_only_trace(outcome.trace)
    _assert_no_model_call(outcome.trace)
    _assert_published_without_a_model(outcome.published)
    _assert_named_summary(build["reason"])


def test_lock_binary_and_generated_files_are_skipped_without_a_model_call() -> None:
    files = (
        VcsFile(
            "package-lock.json",
            "modified",
            None,
            None,
            4,
            0,
            4,
            "@@ -1 +1 @@\n-a\n+b",
        ),
        VcsFile("assets/logo.png", "modified", _PNG_BLOB, None, 0, 0, 0, None),
        VcsFile("dist/app.min.js", "modified", None, None, 2, 0, 2, "@@ -1 +1 @@\n-a\n+b"),
    )
    outcome = _run(files, files_changed=3, blobs={_PNG_BLOB: _PNG}, record_check_run=True)
    build = _context(outcome.trace)

    assert outcome.skipped is not None
    assert outcome.skipped.reason == "all_changes_ignored"
    assert outcome.result is None
    assert build["status"] == "all_changes_ignored"
    assert build["reason"] == "all_changes_ignored"
    assert {row.filename: row.omission_reason for row in outcome.stored} == {
        "package-lock.json": "generated",
        "assets/logo.png": "binary",
        "dist/app.min.js": "generated",
    }
    _assert_no_model_call(outcome.trace)
    assert outcome.check_run is not None
    assert outcome.check_run.conclusion == "skipped"
    assert outcome.check_run.title == "AI-ревью пропущено"


def test_exactly_three_thousand_allowed_lines_still_calls_the_review_model() -> None:
    outcome = _run((_text(3_000),), files_changed=1)
    build = _context(outcome.trace)

    assert outcome.skipped is None
    assert outcome.result is True
    assert build["diff"]["file_list_complete"] is True
    assert any(call.get("operation") == "review" for call in _calls(outcome.trace))


def test_legacy_technical_only_change_is_not_summary_only_no_reviewable_files() -> None:
    # v2 is legacy. One too-large line is the inventory #130 left on the model path.
    too_large = VcsFile(
        "data/large.txt",
        "modified",
        _TEXT_BLOB,
        None,
        1,
        0,
        1,
        None,
        omission_reason=OmissionReason.TOO_LARGE,
    )
    outcome = _run((too_large,), files_changed=1)

    assert outcome.skipped is None
    assert outcome.result is True
    assert outcome.stored[0].omission_reason == "too_large"
    assert all(not row.summary_only for row in outcome.stored)
    assert any(call.get("operation") == "review" for call in _calls(outcome.trace))


def _builds(trace: Trace) -> list[dict[str, Any]]:
    builds = [response for tool, _, response in trace.steps if tool == "context.build"]
    assert builds
    assert all(isinstance(item, dict) for item in builds)
    return [item for item in builds if isinstance(item, dict)]


def test_a_second_attempt_of_residual_files_keeps_no_reviewable_files() -> None:
    missing = VcsFile("src/note.txt", "modified", _TEXT_BLOB, None, 3, 0, 3, None)
    outcome = _run((missing,), files_changed=1, attempts=2)
    builds = _builds(outcome.trace)

    assert [item["reason"] for item in builds] == ["no_reviewable_files", "no_reviewable_files"]
    assert outcome.stored and all(row.summary_only for row in outcome.stored)
    assert len(outcome.published) == 2
    assert all(isinstance(item, NoModelAnswer) for item in outcome.published)
    _assert_no_model_call(outcome.trace)
    _assert_named_summary("no_reviewable_files")


def test_a_second_attempt_of_an_incomplete_list_keeps_its_reason() -> None:
    outcome = _run((_text(1),), files_changed=2, attempts=2)
    builds = _builds(outcome.trace)

    assert [item["reason"] for item in builds] == [
        "diff_inventory_incomplete",
        "diff_inventory_incomplete",
    ]
    assert outcome.stored and all(row.summary_only for row in outcome.stored)
    assert len(outcome.published) == 2
    _assert_no_model_call(outcome.trace)
    _assert_named_summary("diff_inventory_incomplete")


def test_a_second_attempt_over_the_line_limit_stays_diff_too_large() -> None:
    outcome = _run((_text(3_001),), files_changed=1, attempts=2)
    builds = _builds(outcome.trace)

    assert [item["reason"] for item in builds] == ["diff_too_large", "diff_too_large"]
    _assert_no_model_call(outcome.trace)
    _assert_named_summary("diff_too_large")


def test_residual_unreviewable_file_makes_no_model_call() -> None:
    # Complete inventory, not a proven exclusion, not a legacy technical skip: the patch
    # is missing and the blob is ordinary text, so the builder's reason is no_reviewable_files.
    missing = VcsFile("src/note.txt", "modified", _TEXT_BLOB, None, 3, 0, 3, None)
    outcome = _run((missing,), files_changed=1)
    build = _context(outcome.trace)

    assert outcome.skipped is None
    assert outcome.result is True
    assert build["diff"]["file_list_complete"] is True
    assert outcome.stored[0].omission_reason == "missing_patch"
    assert build["coverage"]["status"] == "partial"
    assert "missing_patch" in build["coverage"]["reason_counts"]
    assert build["status"] == "summary_only"
    assert build["reason"] == "no_reviewable_files"
    assert outcome.stored and all(
        row.summary_only and row.review_patch is None for row in outcome.stored
    )
    _assert_summary_only_trace(outcome.trace)
    _assert_no_model_call(outcome.trace)
    _assert_published_without_a_model(outcome.published)
    _assert_named_summary(build["reason"])
