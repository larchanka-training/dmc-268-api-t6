"""Policy and context stage of a run: what it enforces and what it records."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest

from app.modules.code_index.domain.enums import RagIndexStatus
from app.modules.code_index.domain.rag_index import RagIndex
from app.modules.reviews.application.ast_resolution import RevisionTree
from app.modules.reviews.application.ast_symbols import (
    AstFacts,
    AstFailureReason,
    ParseRequest,
)
from app.modules.reviews.application.check_runs import RunProviderCheckpoint
from app.modules.reviews.application.context_provenance import LineRange, SourceRef, SourceRole
from app.modules.reviews.application.diff_map import DiffMap, DiffSide, VcsProviderName
from app.modules.reviews.application.execute_review import ExecuteReviewRun
from app.modules.reviews.application.get_run_diff import DiffSnapshot
from app.modules.reviews.application.policy_documents import PolicyEntryKind, PolicyTreeEntry
from app.modules.reviews.application.policy_errors import PolicyError, PolicyErrorCode
from app.modules.reviews.application.prepare_policy_snapshot import PreparePolicySnapshot
from app.modules.reviews.application.process_run import RunVcsInput
from app.modules.reviews.application.prompt_builder import PullRequestMeta
from app.modules.reviews.application.rag_index import OpenReviewIndex, RagReadiness
from app.modules.reviews.application.review_context import (
    BuildReviewContext,
    ContextBlock,
    ContextLevel,
    ContextStatus,
    Representation,
    ReviewContextPayload,
)
from app.modules.reviews.application.review_context_stage import (
    EstimatedTokenCounter,
    ReviewContextStage,
    context_summary,
)
from app.modules.reviews.application.run_failures import RunCancelled, RunFailure, RunSkipped
from app.modules.reviews.application.vcs_diff import (
    MergeBaseUnavailable,
    PullRequestLocator,
    VcsPullRequest,
    _is_generated,
)
from app.modules.reviews.infrastructure.tree_sitter_facts import extract_facts
from app.worker import WorkerSettings
from tests.test_prepare_policy_snapshot import Store, Uow
from tests.test_rag_readiness import FakeReviewIndexUow, StubReviewIndexAccess

_RUN = UUID("00000000-0000-0000-0000-000000000002")
_HEAD = "a" * 40
_BASE = "b" * 40
_LOCATOR = PullRequestLocator(17, "octo/repo", 7)
_SOURCE = "def greet(name):\n    extra = 1\n    return name\n"
_PATCH = "@@ -1,2 +1,3 @@\n def greet(name):\n+    extra = 1\n     return name"


async def _checkpoint() -> None:
    return None


@dataclass
class Runs:
    snapshots: list[DiffSnapshot] | None

    async def get_run_vcs_input(self, run_id: UUID) -> RunVcsInput:
        return RunVcsInput(UUID(int=5), UUID(int=1), _HEAD, _BASE, _LOCATOR)

    async def get_run_snapshots(self, run_id: UUID) -> list[DiffSnapshot] | None:
        return self.snapshots


@dataclass
class Vcs:
    head_sha: str = _HEAD
    merge_base: str | Exception = "c" * 40
    files_changed: int = 2

    async def get_pull_request(
        self, locator: PullRequestLocator, *, checkpoint: RunProviderCheckpoint
    ) -> VcsPullRequest:
        await checkpoint()
        meta = PullRequestMeta(
            title="t",
            description=None,
            author="octo",
            source_branch="f",
            target_branch="main",
            labels=(),
            files_changed=self.files_changed,
            lines_added=0,
            lines_removed=0,
            is_draft=False,
            is_fork=False,
        )
        return VcsPullRequest(locator, self.head_sha, _BASE, meta)

    async def get_merge_base(
        self, pull_request: VcsPullRequest, *, checkpoint: RunProviderCheckpoint
    ) -> str:
        await checkpoint()
        if isinstance(self.merge_base, Exception):
            raise self.merge_base
        return self.merge_base

    async def get_diff(
        self, pull_request: VcsPullRequest, *, checkpoint: RunProviderCheckpoint
    ) -> tuple[Any, ...]:
        raise AssertionError("the stored diff is reused")

    async def get_blob(
        self, locator: PullRequestLocator, sha: str, *, checkpoint: RunProviderCheckpoint
    ) -> bytes:
        raise AssertionError("blobs are read through the revision source")


@dataclass
class Policy:
    rules: bytes | None = b"```review-ignore\ngenerated/**\n```\n"
    error: PolicyError | None = None

    async def list_entries(self, tree_oid: str | None) -> Mapping[str, PolicyTreeEntry]:
        if self.error is not None:
            raise self.error
        if tree_oid is not None:
            return {"rules.md": PolicyTreeEntry(PolicyEntryKind.FILE, "3" * 40, 5)}
        if self.rules is None:
            return {}
        return {".review": PolicyTreeEntry(PolicyEntryKind.DIRECTORY, "1" * 40, None)}

    async def read_blob(self, oid: str) -> bytes:
        assert self.rules is not None
        return self.rules


class Repository:
    async def tree(self, revision: str) -> RevisionTree:
        return RevisionTree(revision, {"src/app.py": "d" * 40}, complete=True)

    async def read_blob(self, blob_sha: str) -> bytes:
        return _SOURCE.encode()


class Parser:
    async def parse_many(
        self, requests: tuple[ParseRequest, ...]
    ) -> dict[str, AstFacts | AstFailureReason]:
        return {item.key: extract_facts(item.language, item.source) for item in requests}

    async def aclose(self) -> None:
        return None


@dataclass
class Trace:
    steps: list[tuple[str, dict[str, Any], Any]] = field(default_factory=list)

    async def record(
        self,
        run_id: UUID,
        tool: str,
        request: dict[str, Any],
        response: Any,
        started_at: datetime,
        duration_ms: int,
    ) -> None:
        self.steps.append((tool, request, response))


def _stored(path: str, patch: str) -> str:
    """The patch as ``StoreDiffSnapshot`` keeps it for the UI: under a git header."""
    return f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n{patch}"


def _snapshots() -> list[DiffSnapshot]:
    generated = "@@ -1 +1 @@\n-a\n+b"
    return [
        DiffSnapshot(
            "src/app.py", _stored("src/app.py", _PATCH), "d" * 40, "modified", None, 1, 0, 1
        ),
        DiffSnapshot(
            "generated/api.py",
            _stored("generated/api.py", generated),
            "e" * 40,
            "modified",
            None,
            1,
            1,
            2,
            omission_reason="generated",
        ),
    ]


def _stage(
    trace: Trace,
    *,
    store: Store | None = None,
    policy: Policy | None = None,
    vcs: Vcs | None = None,
    snapshots: list[DiffSnapshot] | None = None,
    none: bool = False,
    empty: bool = False,
    builtin_excluded: Callable[[str], bool] = lambda path: False,
    context_builder: Callable[[RunVcsInput], BuildReviewContext] | None = None,
    rag: OpenReviewIndex | None = None,
) -> ReviewContextStage:
    shared = store or Store()
    provider = vcs or Vcs()
    return ReviewContextStage(
        checkpoint=_checkpoint,
        runs=Runs(None if none else [] if empty else (snapshots or _snapshots())),
        vcs=provider,
        merge_base=provider,
        prepare_policy=PreparePolicySnapshot(
            uow_factory=lambda: Uow(shared), source_factory=lambda pins: policy or Policy()
        ),
        context_builder=context_builder
        or (
            lambda run: BuildReviewContext(
                source=Repository(), parser=Parser(), counter=EstimatedTokenCounter()
            )
        ),
        trace=trace,
        builtin_excluded=builtin_excluded,
        rag=rag,
    )


def test_stage_records_the_policy_and_the_context_without_any_document_or_code_text() -> None:
    trace = Trace()

    payload = asyncio.run(_stage(trace).run(_RUN))

    assert payload is not None
    assert payload.status is ContextStatus.REVIEW
    assert [step[0] for step in trace.steps] == ["policy.load", "context.build"]
    policy, context = trace.steps[0][2], trace.steps[1][2]
    assert policy["ignore_patterns"] == 1
    assert policy["documents"][0] == {
        "path": ".review/rules.md",
        "presence": "present",
        "bytes": 34,
        "lines": 3,
    }
    assert policy["documents"][1]["presence"] == "missing"
    assert trace.steps[0][1] == {"policy_base_sha": _BASE}
    assert (context["status"], context["ignored_files"], context["reviewable_lines"]) == (
        "review",
        1,
        1,
    )
    assert context["diff"]["diff_old_sha"] == "c" * 40
    assert context["levels_present"] == ["L1", "L3"]
    assert context["omissions"] == [
        {"reason": "ignored", "path": None, "count": 1, "diagnostic_path": None}
    ]
    assert {block["path"] for block in context["blocks"]} == {"src/app.py"}
    assert context["blocks"][0]["ranges"] == [[1, 3]]
    recorded = repr(trace.steps)
    assert "extra = 1" not in recorded
    assert "generated" not in recorded


def _block(block_id: str, level: ContextLevel, role: SourceRole, tokens: int) -> ContextBlock:
    source = SourceRef(
        role, UUID(int=5), _HEAD, "src/app.py", None, (), DiffSide.RIGHT, (LineRange(1, 3),), "t"
    )
    return ContextBlock(
        block_id, level, None, source, Representation.EXACT_LINES, "x", tokens, False
    )


def test_context_trace_counts_tokens_by_source_role_so_retrieved_excerpts_show() -> None:
    diff_map = DiffMap(
        schema_version=1,
        parser_version="unified-hunks/v1",
        provider=VcsProviderName.GITHUB,
        repository_id=UUID(int=5),
        code_change_number=7,
        policy_base_sha=_BASE,
        head_sha=_HEAD,
        diff_old_sha=None,
        file_list_complete=True,
        incomplete_reasons=(),
        files=(),
        digest="d" * 64,
    )
    payload = ReviewContextPayload(
        status=ContextStatus.REVIEW,
        reason=None,
        policy_snapshot_id=UUID(int=9),
        policy_digest="p" * 64,
        diff_digest="d" * 64,
        blocks=(
            _block("b1", ContextLevel.L1, SourceRole.DIFF, 40),
            _block("b2", ContextLevel.L3, SourceRole.SOURCE_EXCERPT, 25),
            _block("b3", ContextLevel.L3, SourceRole.RETRIEVED_EXCERPT, 60),
            _block("b4", ContextLevel.L3, SourceRole.RETRIEVED_EXCERPT, 30),
        ),
        omissions=(),
        imports=(),
        levels_present=(ContextLevel.L1, ContextLevel.L3),
        reviewable_lines=1,
        ignored_files=0,
        tokens_used=155,
    )

    summary = context_summary(diff_map, payload)

    # The level view merges retrieved excerpts into L3; the role view keeps them apart.
    assert summary["tokens_by_level"] == {"L1": 40, "L3": 115}
    assert summary["tokens_by_role"] == {"diff": 40, "source_excerpt": 25, "retrieved_excerpt": 90}
    assert [block["role"] for block in summary["blocks"]] == [
        "diff",
        "source_excerpt",
        "retrieved_excerpt",
        "retrieved_excerpt",
    ]


def test_retry_reuses_the_stored_policy_snapshot() -> None:
    store = Store()
    first = Trace()
    asyncio.run(_stage(first, store=store).run(_RUN))

    changed = Policy(rules=b"# other rules\n")
    second = Trace()
    asyncio.run(_stage(second, store=store, policy=changed).run(_RUN))

    assert second.steps[0][2]["policy_digest"] == first.steps[0][2]["policy_digest"]
    assert second.steps[0][2]["ignore_patterns"] == 1


def test_unknown_merge_base_is_recorded_as_unknown_not_as_the_policy_base() -> None:
    trace = Trace()
    failing = Vcs(merge_base=MergeBaseUnavailable("down"))

    asyncio.run(_stage(trace, vcs=failing).run(_RUN))

    assert trace.steps[1][2]["diff"]["diff_old_sha"] is None


def test_policy_error_ends_the_attempt_and_stays_in_the_trace() -> None:
    trace = Trace()
    broken = Policy(rules=b"```review-ignore\na?.py\n```\n")
    stage = _stage(trace, policy=broken)

    with pytest.raises(PolicyError) as caught:
        asyncio.run(stage.run(_RUN))

    assert caught.value.error_code == "policy_ignore_pattern_invalid"
    assert [step[0] for step in trace.steps] == ["policy.load"]
    assert trace.steps[0][2]["error"]["type"] == "PolicyError"
    assert "policy_ignore_pattern_invalid" in trace.steps[0][2]["error"]["message"]


def test_unreadable_policy_is_a_retryable_failure_not_an_empty_policy() -> None:
    denied = Policy(error=PolicyError(PolicyErrorCode.FETCH_TRANSIENT, "GitHub answered 502"))

    with pytest.raises(PolicyError) as caught:
        asyncio.run(_stage(Trace(), policy=denied).run(_RUN))

    assert caught.value.retryable is True


def test_change_with_every_file_ignored_skips_the_run() -> None:
    trace = Trace()
    stage = _stage(trace, policy=Policy(rules=b"```review-ignore\n**\n```\n"))

    with pytest.raises(RunSkipped) as caught:
        asyncio.run(stage.run(_RUN))

    assert caught.value.reason == "all_changes_ignored"
    assert trace.steps[1][2]["status"] == "all_changes_ignored"
    assert trace.steps[1][2]["blocks"] == []


def test_stage_names_what_it_withheld_from_model_inputs() -> None:
    stage = _stage(Trace())

    assert stage.excluded("generated/api.py") is False
    asyncio.run(stage.run(_RUN))

    assert stage.excluded("generated/api.py") is True
    assert stage.excluded("src/app.py") is False


def test_failed_context_build_keeps_the_exclusions_and_does_not_fail_the_review() -> None:
    class Broken:
        async def execute(self, **kwargs: object) -> object:
            raise RuntimeError("the tree is unavailable")

    trace = Trace()
    stage = _stage(trace)
    stage._context_builder = lambda run: Broken()  # type: ignore[assignment,return-value]

    assert asyncio.run(stage.run(_RUN)) is None

    # The policy was established before the enrichment failed: it stays in force.
    assert stage.excluded("generated/api.py") is True
    assert stage.excluded("src/app.py") is False
    assert [step[0] for step in trace.steps] == ["policy.load", "context.build"]
    assert trace.steps[1][2]["error"]["type"] == "RuntimeError"


def test_change_of_builtin_excluded_files_only_is_skipped_before_the_context_build() -> None:
    class Broken:
        async def execute(self, **kwargs: object) -> object:
            raise RuntimeError("the tree is unavailable")

    moved = DiffSnapshot(
        "src/client.py",
        _stored("src/client.py", "@@ -1 +1 @@\n-a\n+b"),
        "f" * 40,
        "renamed",
        "dist/client.py",
        1,
        1,
        2,
    )
    trace = Trace()
    stage = _stage(
        trace,
        policy=Policy(rules=None),
        vcs=Vcs(files_changed=1),
        snapshots=[moved],
        builtin_excluded=_is_generated,
    )
    stage._context_builder = lambda run: Broken()  # type: ignore[assignment,return-value]

    # No mask of the repository and a failing builder: the skip is still mandatory.
    with pytest.raises(RunSkipped) as skipped:
        asyncio.run(stage.run(_RUN))

    assert skipped.value.reason == "all_changes_ignored"
    assert stage.excluded("src/client.py") is True
    assert trace.steps[-1][2]["status"] == "all_changes_ignored"


def _binary(path: str = "logo.png") -> DiffSnapshot:
    return DiffSnapshot(path, None, "9" * 40, "modified", None, 0, 0, 0, omission_reason="binary")


def test_binary_files_count_as_exclusions_when_the_whole_change_is_excluded() -> None:
    generated = _snapshots()[1]

    # A masked file next to a binary one, and binary files alone: nothing to review.
    for snapshots in ([generated, _binary()], [_binary(), _binary("icon.png")]):
        stage = _stage(Trace(), snapshots=snapshots)
        with pytest.raises(RunSkipped):
            asyncio.run(stage.run(_RUN))

    # A removed binary file is an exclusion as well.
    removed = DiffSnapshot(
        "old.png", None, None, "removed", None, 0, 0, 0, omission_reason="binary"
    )
    with pytest.raises(RunSkipped):
        asyncio.run(_stage(Trace(), vcs=Vcs(files_changed=1), snapshots=[removed]).run(_RUN))

    # One reviewable file next to them keeps the run going.
    mixed = _stage(Trace(), vcs=Vcs(files_changed=3), snapshots=[*_snapshots(), _binary()])
    assert asyncio.run(mixed.run(_RUN)) is not None


def test_binary_file_of_the_change_is_not_read_through_an_import() -> None:
    class Sources(Repository):
        read: list[str] = []

        async def tree(self, revision: str) -> RevisionTree:
            return RevisionTree(
                revision, {"src/app.py": "d" * 40, "src/models.py": "8" * 40}, complete=True
            )

        async def read_blob(self, blob_sha: str) -> bytes:
            Sources.read.append(blob_sha)
            if blob_sha == "8" * 40:
                return b"\x00\nclass User:\n    pass\n"
            return b"from .models import User\n\n\ndef greet(name):\n    return User()\n"

    app = DiffSnapshot(
        "src/app.py",
        _stored("src/app.py", "@@ -4,1 +4,2 @@\n def greet(name):\n+    return User()"),
        "d" * 40,
        "modified",
        None,
        1,
        0,
        1,
    )
    stage = _stage(Trace(), policy=Policy(rules=None), snapshots=[app, _binary("src/models.py")])
    stage._context_builder = lambda run: BuildReviewContext(
        source=Sources(), parser=Parser(), counter=EstimatedTokenCounter()
    )

    payload = asyncio.run(stage.run(_RUN))

    assert payload is not None
    assert stage.excluded("src/models.py") is True
    assert [ref.resolution.value for ref in payload.imports] == ["excluded"]
    assert all(block.source.path != "src/models.py" for block in payload.blocks)
    assert "8" * 40 not in Sources.read


def test_incomplete_file_list_does_not_prove_that_every_change_is_excluded() -> None:
    # The pull request reports two files, one is known and it is ignored.
    stage = _stage(Trace(), snapshots=[_snapshots()[1]])

    payload = asyncio.run(stage.run(_RUN))

    assert payload is not None
    assert (payload.status, payload.reason) == (
        ContextStatus.SUMMARY_ONLY,
        "diff_inventory_incomplete",
    )


def test_source_of_a_copy_into_an_excluded_place_stays_reviewable() -> None:
    copied = DiffSnapshot(
        "dist/app.py",
        _stored("dist/app.py", "@@ -1 +1 @@\n-a\n+b"),
        "f" * 40,
        "copied",
        "src/app.py",
        1,
        1,
        2,
        omission_reason="generated",
    )
    stage = _stage(
        Trace(),
        policy=Policy(rules=None),
        snapshots=[_snapshots()[0], copied],
        builtin_excluded=_is_generated,
    )

    payload = asyncio.run(stage.run(_RUN))

    assert payload is not None and payload.status is ContextStatus.REVIEW
    assert stage.excluded("dist/app.py") is True
    assert stage.excluded("src/app.py") is False


def test_change_without_files_is_skipped_only_when_the_provider_counts_none() -> None:
    trace = Trace()
    with pytest.raises(RunSkipped) as skipped:
        asyncio.run(_stage(trace, vcs=Vcs(files_changed=0), snapshots=[], empty=True).run(_RUN))

    assert skipped.value.reason == "no_changes"
    assert trace.steps[-1][2]["status"] == "no_changes"

    # The provider counts two files and none is known: that is not an empty change.
    payload = asyncio.run(_stage(Trace(), snapshots=[], empty=True).run(_RUN))
    assert payload is not None and payload.reason == "diff_inventory_incomplete"


def test_policy_error_while_the_context_is_built_stops_the_run() -> None:
    class OverBudget:
        async def execute(self, **kwargs: object) -> object:
            # What the matcher raises for a path behind an import.
            raise PolicyError(PolicyErrorCode.MATCH_BUDGET_EXCEEDED, "over the budget")

    stage = _stage(Trace())
    stage._context_builder = lambda run: OverBudget()  # type: ignore[assignment,return-value]

    with pytest.raises(PolicyError) as failure:
        asyncio.run(stage.run(_RUN))

    assert failure.value.error_code == "policy_match_budget_exceeded"


def test_builtin_exclusions_are_withheld_without_any_mask_of_the_repository() -> None:
    stage = _stage(Trace(), policy=Policy(rules=None), builtin_excluded=_is_generated)
    asyncio.run(stage.begin(_RUN))

    assert stage.excluded("dist/bundle.ts") is True
    assert stage.excluded("api/client.pb.go") is True
    assert stage.excluded("src/app.py") is False
    # A file copied or renamed out of a built-in exclusion is withheld under its new name.
    assert stage.excludes_change("src/bundle.ts", "dist/bundle.ts") is True


def test_cache_identity_changes_when_a_rename_withholds_a_name_no_mask_matches() -> None:
    plain = _stage(Trace())
    asyncio.run(plain.run(_RUN))

    moved = DiffSnapshot(
        "src/moved.py",
        _stored("src/moved.py", "@@ -1 +1 @@\n-a\n+b"),
        "f" * 40,
        "renamed",
        "generated/moved.py",
        1,
        1,
        2,
    )
    tainted = _stage(Trace(), snapshots=[*_snapshots(), moved])
    asyncio.run(tainted.run(_RUN))

    assert tainted.excluded("src/moved.py") is True
    # The same masks, another set of withheld names: an entry cached while
    # ``src/moved.py`` was allowed is not reused.
    assert plain.policy_key is not None and tainted.policy_key is not None
    assert plain.policy_key != tainted.policy_key


def test_rename_out_of_a_builtin_exclusion_stays_withheld_after_the_whole_stage() -> None:
    plain = _stage(Trace(), policy=Policy(rules=None), builtin_excluded=_is_generated)
    asyncio.run(plain.run(_RUN))

    moved = DiffSnapshot(
        "src/client.py",
        _stored("src/client.py", "@@ -1 +1 @@\n-a\n+b"),
        "f" * 40,
        "renamed",
        "dist/client.py",
        1,
        1,
        2,
    )
    stage = _stage(
        Trace(),
        policy=Policy(rules=None),
        snapshots=[*_snapshots(), moved],
        builtin_excluded=_is_generated,
    )
    asyncio.run(stage.run(_RUN))

    # No mask of the repository matches the new name: the built-in exclusion of the old
    # one withholds it for conventions, the review lists and the context alike.
    assert stage.excluded("src/client.py") is True
    assert stage.excluded("src/app.py") is False
    assert plain.policy_key != stage.policy_key


def test_path_outside_the_contract_is_always_withheld() -> None:
    stage = _stage(Trace())

    assert stage.excluded("src\\bad.py") is True
    assert stage.excluded("a\x85b.py") is True


def test_policy_part_that_cannot_be_established_fails_the_attempt() -> None:
    with pytest.raises(RunFailure) as changed:
        asyncio.run(_stage(Trace(), vcs=Vcs(head_sha="f" * 40)).run(_RUN))
    assert changed.value.error_code == "diff_fetch_failed"

    with pytest.raises(LookupError, match="no stored diff"):
        asyncio.run(_stage(Trace(), none=True).run(_RUN))

    class Unreachable(Vcs):
        async def get_pull_request(
            self, locator: PullRequestLocator, *, checkpoint: RunProviderCheckpoint
        ) -> VcsPullRequest:
            await checkpoint()
            raise RunFailure("diff_fetch_failed", "GitHub answered 502")

    stage = _stage(Trace(), vcs=Unreachable())
    with pytest.raises(RunFailure):
        asyncio.run(stage.run(_RUN))
    # The policy was loaded before the diff could not be read: its masks already apply.
    assert stage.excluded("generated/api.py") is True
    assert stage.policy_key is not None


def test_policy_is_established_before_the_diff_and_names_what_a_change_withholds() -> None:
    trace = Trace()
    stage = _stage(trace)

    assert stage.policy_key is None
    assert stage.excluded("generated/api.py") is False
    asyncio.run(stage.begin(_RUN))

    assert [step[0] for step in trace.steps] == ["policy.load"]
    assert stage.excluded("generated/other.py") is True
    assert stage.excludes_change("src/moved.py", "generated/old.py") is True
    assert stage.excludes_change("src/app.py", None) is False
    assert stage.excludes_change("src/app.py", "src\\bad.py") is True
    first_key = stage.policy_key

    # The policy is not loaded twice within one attempt.
    asyncio.run(stage.run(_RUN))
    assert [step[0] for step in trace.steps] == ["policy.load", "context.build"]
    assert stage.policy_key == first_key


def test_review_prompt_gets_neither_the_content_nor_the_name_of_an_ignored_file() -> None:
    from app.modules.reviews.application.conventions import GeneratedConventions
    from app.modules.reviews.application.execute_review import ReviewPromptInput
    from app.modules.reviews.application.prompt_builder import (
        ChangedFile,
        RepoConventions,
        ReviewContext,
    )

    seen: list[ReviewContext] = []
    meta = asyncio.run(Vcs().get_pull_request(_LOCATOR, checkpoint=_checkpoint)).meta

    class Processor:
        async def prepare(self, run_id: UUID) -> object:
            return GeneratedConventions.__new__(GeneratedConventions)

    class Prompts:
        async def get_review_prompt_input(
            self, run_id: UUID, conventions: object
        ) -> ReviewPromptInput:
            return ReviewPromptInput(
                system="system",
                rules=(),
                agents_md=None,
                conventions=RepoConventions((), ()),
                changed_files=(
                    ChangedFile("src/app.py", "modified", ()),
                    ChangedFile("generated/api.py", "modified", ()),
                ),
                omitted_files=("generated/big.py", "assets/logo.png"),
            )

    class Model:
        async def get_pull_request_meta(self, run_id: UUID) -> PullRequestMeta:
            return meta

        async def draft_review(self, *, context: ReviewContext) -> dict[str, object]:
            seen.append(context)
            return {}

    class Publisher:
        async def execute(self, run_id: UUID, model_output: object) -> bool:
            return True

    class Withheld:
        def excluded(self, path: str) -> bool:
            return path.startswith("generated/")

    def run(context_filter: Withheld | None) -> ReviewContext:
        seen.clear()
        pipeline = ExecuteReviewRun(
            Processor(),  # type: ignore[arg-type]
            Prompts(),
            Model(),
            Publisher(),
            context_filter=context_filter,
        )
        assert asyncio.run(pipeline.execute(_RUN)) is True
        return seen[0]

    filtered = run(Withheld())
    assert [file.path for file in filtered.changed_files] == ["src/app.py"]
    assert filtered.omitted_files == ("assets/logo.png",)

    untouched = run(None)
    assert [file.path for file in untouched.changed_files] == ["src/app.py", "generated/api.py"]
    assert untouched.omitted_files == ("generated/big.py", "assets/logo.png")


def test_policy_codes_have_a_retry_class_and_an_author_facing_text() -> None:
    from app.modules.reviews.application.check_runs import _ERROR_TEXTS
    from app.modules.reviews.application.policy_errors import RETRYABLE_POLICY_ERROR_CODES
    from app.modules.reviews.application.run_failures import (
        KNOWN_ERROR_CODES,
        RETRYABLE_ERROR_CODES,
        TERMINAL_POLICY_ERROR_CODES,
    )

    codes = {code.value for code in PolicyErrorCode}
    retryable = {code.value for code in RETRYABLE_POLICY_ERROR_CODES}

    assert codes <= KNOWN_ERROR_CODES
    assert (
        codes & RETRYABLE_ERROR_CODES
        == retryable
        == {
            "policy_fetch_transient",
            "policy_rate_limited",
        }
    )
    assert codes - retryable == TERMINAL_POLICY_ERROR_CODES
    assert codes | {"all_changes_ignored", "no_changes"} <= set(_ERROR_TEXTS)


def test_redis_address_is_optional_for_the_worker() -> None:
    env = {"DATABASE_URL": "postgresql://x", "RABBITMQ_URL": "amqp://x"}

    assert WorkerSettings.from_environment(env).redis_url is None
    assert WorkerSettings.from_environment({**env, "REDIS_URL": ""}).redis_url is None
    configured = WorkerSettings.from_environment({**env, "REDIS_URL": "redis://r:6379/0"})
    assert configured.redis_url == "redis://r:6379/0"


def test_stored_patch_without_hunks_and_an_unwrapped_patch_are_both_accepted() -> None:
    header_only = "diff --git a/img.png b/img.png\n--- a/img.png\n+++ b/img.png\n"
    snapshots = [
        DiffSnapshot("src/app.py", _PATCH, "d" * 40, "modified", None, 1, 0, 1),
        DiffSnapshot("img.png", header_only, "e" * 40, "modified", None, 0, 0, 0),
    ]
    trace = Trace()

    asyncio.run(_stage(trace, snapshots=snapshots).run(_RUN))

    context = trace.steps[1][2]
    assert context["status"] == "review"
    assert context["omissions"] == [
        {"reason": "patch_metadata_only", "path": "img.png", "count": 1, "diagnostic_path": None}
    ]


def test_execution_profile_takes_what_the_worker_knows_about_its_models() -> None:
    from app.modules.reviews.infrastructure.llm.settings import LlmSettings
    from app.worker import execution_profile_of

    assert execution_profile_of(None).review_model == ""
    settings = LlmSettings.from_env({"LLM_MODEL": "mistral-small-4", "LLM_API_KEYS": "sk-test"})
    profile = execution_profile_of(settings)

    assert (profile.profile_name, profile.review_model) == ("fast", "mistral-small-4")
    assert profile.primary_provider_route == settings.primary.provider
    assert profile.fallback_review_model is None
    # The verifier model and the versioned profiles are not known to the worker yet.
    assert profile.verifier_model == ""
    assert profile.is_complete is False


def test_check_run_says_where_a_policy_failure_is() -> None:
    from app.modules.reviews.application.check_runs import (
        CheckRunReport,
        CheckRunTarget,
        check_run_view,
    )
    from app.modules.reviews.application.policy_documents import normalize_document
    from app.modules.reviews.application.policy_errors import policy_error_detail
    from app.modules.reviews.application.review_ignore import parse_review_ignore

    def summary(error: PolicyError) -> str:
        report = CheckRunReport(
            CheckRunTarget(17, "octo/repo", _HEAD, _RUN),
            "failed",
            1,
            error.error_code,
            error_detail=policy_error_detail(error.message),
        )
        return check_run_view(report).summary

    with pytest.raises(PolicyError) as mask:
        parse_review_ignore("# Ignore\n\n```review-ignore\nok/**\nsrc/a?.py\n```\n")
    assert summary(mask.value) == (
        "`policy_ignore_pattern_invalid`: В блоке review-ignore недопустимая маска. "
        "Где: файл `.review/rules.md`, строка 5, позиция 6, маска `src/a?.py`."
    )

    # A long or hostile mask is quoted short and cannot break the markup around it.
    hostile = "a?" + "`" + "x" * 200
    with pytest.raises(PolicyError) as long_mask:
        parse_review_ignore(f"```review-ignore\n{hostile}\n```\n")
    quoted = summary(long_mask.value).split("маска ")[1]
    assert quoted == "`a?'" + "x" * 77 + "...`."
    assert quoted.count("`") == 2

    with pytest.raises(PolicyError) as size:
        normalize_document("AGENTS.md", b"a" * 87_040, blob_oid="f" * 40)
    assert summary(size.value).endswith("Где: файл `AGENTS.md`, фактически 87040, допустимо 65536.")

    # A failure without a location keeps the plain text of its code.
    plain = PolicyError(PolicyErrorCode.ACCESS_DENIED, "GitHub answered 403")
    assert policy_error_detail(plain.message) is None
    assert summary(plain) == (
        "`policy_access_denied`: Нет доступа к документам правил репозитория."
    )
    assert policy_error_detail(None) is None
    assert policy_error_detail("llm_timeout: slow") is None


@pytest.mark.parametrize("failure", ["cancel", "deadline", "optional"])
def test_context_stage_propagates_control_but_preserves_optional_fallback(failure: str) -> None:
    error = {
        "cancel": RunCancelled("installation_unavailable"),
        "deadline": RunFailure("deadline_exceeded"),
        "optional": RunFailure("diff_fetch_failed", "optional enrichment"),
    }[failure]

    class Broken(BuildReviewContext):
        async def execute(self, **kwargs: Any) -> Any:
            raise error

    trace = Trace()
    stage = _stage(
        trace,
        context_builder=lambda run: Broken(
            source=Repository(), parser=Parser(), counter=EstimatedTokenCounter()
        ),
    )
    if failure == "optional":
        assert asyncio.run(stage.run(_RUN)) is None
        assert stage.excluded("generated/api.py")
    else:
        with pytest.raises(type(error)) as caught:
            asyncio.run(stage.run(_RUN))
        assert caught.value is error
    assert trace.steps[-1][2]["error"]["type"] == type(error).__name__


# The index the review opens at the RAG gate (#144): pointers of a fake readiness.
_REPOSITORY = UUID(int=1)
_INDEXED = "6" * 40
_TARGET = "7" * 40
_USABLE = (RagIndexStatus.READY, RagIndexStatus.STALE)


def _readiness(status: RagIndexStatus, *, error_code: str | None = None) -> RagReadiness:
    usable = status in _USABLE
    index = RagIndex(
        status,
        _INDEXED if usable else None,
        datetime(2026, 10, 23, 9, 30, tzinfo=UTC) if usable else None,
        3 if usable else 0,
        9 if usable else 0,
        error_code,
    )
    return RagReadiness(enabled=True, index=index, default_head_sha=_TARGET)


def _gate(readiness: RagReadiness) -> tuple[OpenReviewIndex, StubReviewIndexAccess]:
    access = StubReviewIndexAccess(readiness)
    return OpenReviewIndex(lambda: FakeReviewIndexUow(access)), access


@dataclass
class RaisingIndexAccess:
    error: Exception
    opened: list[UUID] = field(default_factory=list)

    async def open_for_review(self, repository_id: UUID) -> RagReadiness:
        self.opened.append(repository_id)
        raise self.error


def _failing_gate(error: Exception) -> tuple[OpenReviewIndex, RaisingIndexAccess]:
    access = RaisingIndexAccess(error)
    return OpenReviewIndex(lambda: FakeReviewIndexUow(access)), access


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
        # No prompt version has a retrieved section yet: a usable index is not used.
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
    [RuntimeError("the database is gone"), RunFailure("diff_fetch_failed", "optional")],
    ids=["unexpected", "run-failure"],
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
