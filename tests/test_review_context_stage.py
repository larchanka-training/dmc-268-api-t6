"""Policy and context stage of a run: what it enforces and what it records."""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import replace
from datetime import timedelta
from typing import Any, Literal
from uuid import UUID

import pytest

from app.modules.code_index.domain.enums import RagIndexStatus
from app.modules.reviews.application import rag_index, review_ignore
from app.modules.reviews.application.ast_resolution import RevisionTree
from app.modules.reviews.application.check_runs import RunProviderCheckpoint
from app.modules.reviews.application.context_provenance import SourceRole
from app.modules.reviews.application.diff_map import DiffMap, DiffSide, VcsProviderName
from app.modules.reviews.application.execute_review import ExecuteReviewRun
from app.modules.reviews.application.get_run_diff import DiffSnapshot
from app.modules.reviews.application.policy_errors import PolicyError, PolicyErrorCode
from app.modules.reviews.application.policy_snapshot import PolicySnapshot
from app.modules.reviews.application.prepare_policy_snapshot import PreparePolicySnapshot
from app.modules.reviews.application.process_run import RunVcsInput
from app.modules.reviews.application.prompt_builder import FileStatus, PullRequestMeta
from app.modules.reviews.application.review_context import (
    BuildReviewContext,
    ContextLevel,
    ContextStatus,
    ReviewContextPayload,
    SummaryOnlyReview,
)
from app.modules.reviews.application.review_context_stage import (
    EstimatedTokenCounter,
    ReviewContextStage,
    context_summary,
)
from app.modules.reviews.application.review_ignore import IgnoreMatcher
from app.modules.reviews.application.run_failures import RunCancelled, RunFailure, RunSkipped
from app.modules.reviews.application.vcs_diff import (
    MergeBaseUnavailable,
    PullRequestLocator,
    VcsPullRequest,
    _is_generated,
)
from app.modules.reviews.application.verdict import SeverityThresholdValue
from app.worker import WorkerSettings
from tests.execute_review_fakes import _AcceptingCheckpointPublisher
from tests.policy_snapshot_fakes import Store as Store
from tests.policy_snapshot_fakes import Uow as Uow
from tests.review_context_stage_support import (
    _BASE as _BASE,
)
from tests.review_context_stage_support import (
    _HEAD as _HEAD,
)
from tests.review_context_stage_support import (
    _LOCATOR as _LOCATOR,
)
from tests.review_context_stage_support import (
    _PATCH as _PATCH,
)
from tests.review_context_stage_support import (
    _PINNED as _PINNED,
)
from tests.review_context_stage_support import (
    _RUN as _RUN,
)
from tests.review_context_stage_support import (
    _SOURCE as _SOURCE,
)
from tests.review_context_stage_support import (
    Parser as Parser,
)
from tests.review_context_stage_support import (
    Payloads as Payloads,
)
from tests.review_context_stage_support import (
    Policy as Policy,
)
from tests.review_context_stage_support import (
    Repository as Repository,
)
from tests.review_context_stage_support import (
    Runs as Runs,
)
from tests.review_context_stage_support import (
    Trace as Trace,
)
from tests.review_context_stage_support import (
    Vcs as Vcs,
)
from tests.review_context_stage_support import (
    _block as _block,
)
from tests.review_context_stage_support import (
    _checkpoint as _checkpoint,
)
from tests.review_context_stage_support import (
    _gate as _gate,
)
from tests.review_context_stage_support import (
    _greet_hints as _greet_hints,
)
from tests.review_context_stage_support import (
    _mark_summary_only_rows as _mark_summary_only_rows,
)
from tests.review_context_stage_support import (
    _rag_builder as _rag_builder,
)
from tests.review_context_stage_support import (
    _readiness as _readiness,
)
from tests.review_context_stage_support import (
    _snapshots as _snapshots,
)
from tests.review_context_stage_support import (
    _stage as _stage,
)
from tests.review_context_stage_support import (
    _stored as _stored,
)
from tests.run_coverage_fakes import empty_snapshot


def test_stage_records_the_policy_and_the_context_without_any_document_or_code_text() -> None:
    trace = Trace()
    stage = _stage(trace)

    payload = asyncio.run(stage.run(_RUN))

    assert payload is not None
    assert stage.diff_old_sha == "c" * 40
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


def test_stored_masks_that_disagree_with_the_rules_text_still_decide_the_run(
    caplog: pytest.LogCaptureFixture,
) -> None:
    review_ignore._snapshot_ignore_policy.cache_clear()
    store = Store()
    asyncio.run(_stage(Trace(), store=store).run(_RUN))
    assert store.snapshot is not None
    # The rules text still says generated/**; the stored masks no longer do.
    store.snapshot = replace(store.snapshot, ignore_patterns=("other/**",))
    trace = Trace()
    stage = _stage(trace, store=store)

    with caplog.at_level(logging.WARNING, logger=review_ignore.__name__):
        payload = asyncio.run(stage.run(_RUN))

    # The source lines are diagnostics: their loss never fails the run, and the stored
    # masks, not the re-parsed ones, decide what is withheld.
    assert payload is not None
    assert [step[0] for step in trace.steps] == ["policy.load", "context.build"]
    assert (stage.excluded("other/a.py"), stage.excluded("generated/b.py")) == (True, False)
    assert [
        record.getMessage() for record in caplog.records if record.name == review_ignore.__name__
    ] == [
        "Ignore source lines are unavailable, the stored masks apply without them: "
        "the re-parsed masks differ from the stored ones "
        f"repository_id=00000000-0000-0000-0000-000000000001 policy_base_sha={_BASE}"
    ]


def test_unknown_merge_base_is_recorded_as_unknown_not_as_the_policy_base() -> None:
    trace = Trace()
    failing = Vcs(merge_base=MergeBaseUnavailable("down"))
    stage = _stage(trace, vcs=failing)

    asyncio.run(stage.run(_RUN))

    assert trace.steps[1][2]["diff"]["diff_old_sha"] is None
    assert stage.diff_old_sha is None
    assert stage.policy_key is not None


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


def test_policy_error_records_its_diagnostic_in_the_policy_load_action() -> None:
    trace = Trace()
    masks = "\n".join(f"d{index}/**" for index in range(257))
    crowded = Policy(rules=f"```review-ignore\n{masks}\n```\n".encode())

    with pytest.raises(PolicyError):
        asyncio.run(_stage(trace, policy=crowded).run(_RUN))

    [(tool, _, response)] = trace.steps
    assert tool == "policy.load"
    # RFS §9: every field of the diagnostic; the message is the safe one without the
    # location, which stays only in the Run message.
    assert response["error"]["diagnostic"] == {
        "code": "policy_ignore_limit_exceeded",
        "source_path": ".review/rules.md",
        "policy_base_sha": _BASE,
        "message": "The review-ignore block has more than 256 masks",
        "line": 1,
        "column": None,
        "pattern_index": None,
        "limit": 256,
        "actual": 257,
        "actual_is_lower_bound": False,
        "retryable": False,
    }
    assert response["error"]["type"] == "PolicyError"
    assert response["error"]["message"].startswith("policy_ignore_limit_exceeded: ")


def test_match_budget_error_names_the_pinned_policy_base(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(review_ignore, "MAX_TRANSITIONS_PER_PATH", 1)
    stage = _stage(Trace(), policy=Policy(rules=b"```review-ignore\n**/*.md\n```\n"))

    with pytest.raises(PolicyError) as caught:
        asyncio.run(stage.run(_RUN))

    assert caught.value.code is PolicyErrorCode.MATCH_BUDGET_EXCEEDED
    assert caught.value.diagnostic()["policy_base_sha"] == _BASE


def test_mask_error_diagnostic_keeps_line_column_and_mask_index_apart() -> None:
    trace = Trace()
    broken = Policy(rules=b"# Rules\n\n```review-ignore\nok/**\nsrc/a?.py\n```\n")

    with pytest.raises(PolicyError):
        asyncio.run(_stage(trace, policy=broken).run(_RUN))

    # Line 5, column 6 and the second mask: three different numbers, so a swap shows.
    assert trace.steps[0][2]["error"]["diagnostic"] == {
        "code": "policy_ignore_pattern_invalid",
        "source_path": ".review/rules.md",
        "policy_base_sha": _BASE,
        "message": "Invalid review-ignore mask: the character is reserved in a mask",
        "line": 5,
        "column": 6,
        "pattern_index": 1,
        "limit": None,
        "actual": None,
        "actual_is_lower_bound": False,
        "retryable": False,
    }


def test_profile_check_failure_names_the_pinned_policy_base() -> None:
    from dataclasses import replace

    from app.modules.reviews.application.policy_snapshot import ExecutionProfile
    from tests.policy_snapshot_fakes import _pins

    trace = Trace()
    profile = replace(ExecutionProfile.fast("model-a", ""), tokenizer_version="tokenizer/v0")

    with pytest.raises(PolicyError) as caught:
        asyncio.run(_stage(trace, store=Store(pins=_pins(execution_profile=profile))).run(_RUN))

    assert caught.value.code is PolicyErrorCode.SNAPSHOT_INTEGRITY_ERROR
    [(tool, _, response)] = trace.steps
    diagnostic = response["error"]["diagnostic"]
    # The check does not know the base; the policy.load step fills it, no document is named.
    assert (tool, diagnostic["policy_base_sha"], diagnostic["source_path"]) == (
        "policy.load",
        _BASE,
        None,
    )


def test_matcher_error_in_the_context_build_is_recorded_in_that_step() -> None:
    class Probing:
        async def execute(self, *, matcher: IgnoreMatcher, **kwargs: object) -> object:
            return matcher.match("a/../b.py")

    trace = Trace()
    stage = _stage(trace)
    stage._context_builder = lambda run: Probing()  # type: ignore[assignment,return-value]

    with pytest.raises(PolicyError):
        asyncio.run(stage.run(_RUN))

    assert [step[0] for step in trace.steps] == ["policy.load", "context.build"]
    # A repository path, not a policy document: no source_path, but the pinned base.
    assert trace.steps[1][2]["error"]["diagnostic"] == {
        "code": "policy_path_invalid",
        "source_path": None,
        "policy_base_sha": _BASE,
        "message": "A repository path violates the path contract",
        "line": None,
        "column": None,
        "pattern_index": None,
        "limit": None,
        "actual": None,
        "actual_is_lower_bound": False,
        "retryable": False,
    }


def test_unreadable_policy_is_a_retryable_failure_not_an_empty_policy() -> None:
    trace = Trace()
    denied = Policy(error=PolicyError(PolicyErrorCode.FETCH_TRANSIENT, "GitHub answered 502"))

    with pytest.raises(PolicyError) as caught:
        asyncio.run(_stage(trace, policy=denied).run(_RUN))

    assert caught.value.retryable is True
    assert trace.steps[0][2]["error"]["diagnostic"]["retryable"] is True


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


def test_stage_names_the_changed_files_an_anchor_proves_ignored() -> None:
    # CVS §6.2 pass 2: every name of a changed file of proven identity that a mask, a
    # built-in exclusion or the generated marker excluded. A binary or empty file, the
    # source of a copy and a file of unresolved identity are not proven ignored files;
    # under a mask a binary or empty file is (coverage classifies ``ignored`` first).
    def changed(
        path: str, status: FileStatus = "modified", previous: str | None = None
    ) -> DiffSnapshot:
        patch = _stored(path, "@@ -1 +1 @@\n-a\n+b")
        return DiffSnapshot(path, patch, "f" * 40, status, previous, 1, 1, 2)

    snapshots = [
        *_snapshots(),
        DiffSnapshot("generated/gone.py", None, None, "removed", None, 0, 1, 1),
        changed("src/moved.py", "renamed", "generated/moved.py"),
        changed("generated/copy.py", "copied", "src/app.py"),
        changed("dist/bundle.ts"),
        DiffSnapshot("src/schema.py", None, "8" * 40, omission_reason="generated"),
        _binary(),
        _binary("generated/logo.png"),
        empty_snapshot(),
        replace(empty_snapshot(), filename="generated/__init__.py"),
        DiffSnapshot("generated/lost.py", None, status="renamed"),
    ]
    stage = _stage(
        Trace(),
        vcs=Vcs(files_changed=len(snapshots)),
        snapshots=snapshots,
        builtin_excluded=_is_generated,
    )
    assert stage.ignored_file_paths == frozenset()

    asyncio.run(stage.run(_RUN))

    assert stage.ignored_file_paths == {
        "generated/api.py",
        "generated/gone.py",
        "src/moved.py",
        "generated/moved.py",
        "generated/copy.py",
        "dist/bundle.ts",
        "src/schema.py",
        "generated/logo.png",
        "generated/__init__.py",
    }
    # Withheld from every model input all the same, yet not proven ignored files.
    assert all(stage.excluded(path) for path in ("logo.png", "pkg/__init__.py"))


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


def test_stage_keeps_the_payload_of_the_attempt_and_never_a_stale_one() -> None:
    stage = _stage(Trace())

    payload = asyncio.run(stage.run(_RUN))

    assert payload is not None and stage.payload is payload

    class Broken:
        async def execute(self, **kwargs: object) -> object:
            raise RuntimeError("the tree is unavailable")

    stage._context_builder = lambda run: Broken()  # type: ignore[assignment,return-value]

    assert asyncio.run(stage.run(_RUN)) is None
    # A caught build failure leaves no payload, not the one of an earlier build.
    assert stage.payload is None


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
    trace = Trace()
    stage = _stage(trace, snapshots=[_snapshots()[1]])

    with pytest.raises(SummaryOnlyReview):
        asyncio.run(stage.run(_RUN))

    build = trace.steps[-1][2]
    assert (build["status"], build["reason"]) == ("summary_only", "diff_inventory_incomplete")


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
    trace = Trace()
    with pytest.raises(SummaryOnlyReview):
        asyncio.run(_stage(trace, snapshots=[], empty=True).run(_RUN))
    assert trace.steps[-1][2]["reason"] == "diff_inventory_incomplete"


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
    tainted = _stage(Trace(), vcs=Vcs(files_changed=3), snapshots=[*_snapshots(), moved])
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
        vcs=Vcs(files_changed=3),
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

    class Publisher(_AcceptingCheckpointPublisher):
        async def execute(self, run_id: UUID, model_output: object) -> bool:
            return True

    class Withheld:
        diff_old_sha: str | None = None
        head_sha: str | None = None
        snapshot_for_llm: PolicySnapshot | None = None
        payload: ReviewContextPayload | None = None
        ignored_file_paths: frozenset[str] = frozenset()

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


def test_ignore_limit_text_names_a_too_long_mask_in_correct_russian() -> None:
    from app.modules.reviews.application.check_runs import _ERROR_TEXTS, failure_reason

    assert failure_reason("policy_ignore_limit_exceeded") == (
        "В блоке review-ignore слишком много масок или слишком длинная маска."
    )
    # The misspelt word, not the prefix of the correct «длинная».
    assert [text for text in _ERROR_TEXTS.values() if re.search(r"\bдлинна\b", text)] == []


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
    from app.modules.reviews.application.prompt_budget import validate_execution_profile_versions
    from app.modules.reviews.infrastructure.llm.settings import LlmSettings
    from app.worker import execution_profile_of

    assert execution_profile_of(None).review_model == ""
    settings = LlmSettings.from_env({"LLM_MODEL": "mistral-small-4", "LLM_API_KEYS": "sk-test"})
    profile = execution_profile_of(settings)

    assert (profile.profile_name, profile.review_model) == ("fast", "mistral-small-4")
    assert profile.primary_provider_route == settings.primary.provider
    assert profile.fallback_review_model is None
    assert profile.verifier_model == "mistral-small-4"
    assert profile.tokenizer_version == "tokenizer/v1"
    assert profile.counter_version == "counter/v2"
    assert profile.recovery_profile_version == "recovery/v1"
    assert profile.budget_profile_version == "budget/v1"
    validate_execution_profile_versions(profile)
    assert profile.is_complete is True


def test_prepare_snapshot_and_stage_begin_raise_on_unknown_profile_version() -> None:
    from dataclasses import replace
    from unittest.mock import AsyncMock

    from app.modules.reviews.application.policy_errors import PolicyError, PolicyErrorCode
    from tests.execute_review_fakes import _make_test_snapshot
    from tests.policy_snapshot_fakes import _pins

    # 1. PreparePolicySnapshot.execute raises on unknown budget_profile_version in pins
    bad_pins = _pins(
        execution_profile=replace(_pins().execution_profile, budget_profile_version="garbage")
    )
    store = Store(pins=bad_pins)
    prepare = PreparePolicySnapshot(
        uow_factory=lambda: Uow(store), source_factory=lambda p: Policy()
    )
    with pytest.raises(PolicyError) as exc_info_prep:
        asyncio.run(prepare.execute(_RUN))
    assert exc_info_prep.value.code == PolicyErrorCode.SNAPSHOT_INTEGRITY_ERROR
    assert "Unknown budget_profile_version 'garbage'" in str(exc_info_prep.value)

    # 2. PreparePolicySnapshot.execute raises on unknown recovery_profile_version
    # in pre-existing snapshot
    snap_valid = _make_test_snapshot()
    snap_bad_recovery = replace(
        snap_valid,
        execution_profile=replace(snap_valid.execution_profile, recovery_profile_version="garbage"),
    )
    store_with_snap = Store(snapshot=snap_bad_recovery)
    prepare_with_snap = PreparePolicySnapshot(
        uow_factory=lambda: Uow(store_with_snap), source_factory=lambda p: Policy()
    )
    with pytest.raises(PolicyError) as exc_info_snap:
        asyncio.run(prepare_with_snap.execute(_RUN))
    assert exc_info_snap.value.code == PolicyErrorCode.SNAPSHOT_INTEGRITY_ERROR
    assert "Unknown recovery_profile_version 'garbage'" in str(exc_info_snap.value)

    # 3. ReviewContextStage.begin raises on unknown profile version via prepare execution
    stage = _stage(Trace(), store=store)
    with pytest.raises(PolicyError) as exc_info_stage:
        asyncio.run(stage.begin(_RUN))
    assert exc_info_stage.value.code == PolicyErrorCode.SNAPSHOT_INTEGRITY_ERROR
    assert "Unknown budget_profile_version 'garbage'" in str(exc_info_stage.value)

    # 4. ReviewContextStage.begin line 273 validates returned snapshot
    stage_mocked = _stage(Trace())
    stage_mocked._prepare_policy = AsyncMock()
    stage_mocked._prepare_policy.execute.return_value = snap_bad_recovery
    with pytest.raises(PolicyError) as exc_info_stage_273:
        asyncio.run(stage_mocked.begin(_RUN))
    assert exc_info_stage_273.value.code == PolicyErrorCode.SNAPSHOT_INTEGRITY_ERROR
    assert "Unknown recovery_profile_version 'garbage'" in str(exc_info_stage_273.value)


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
        "Where: file `.review/rules.md`, line 5, column 6, mask `src/a?.py`."
    )

    # A long or hostile mask is quoted short and cannot break the markup around it.
    hostile = "a?" + "`" + "x" * 200
    with pytest.raises(PolicyError) as long_mask:
        parse_review_ignore(f"```review-ignore\n{hostile}\n```\n")
    quoted = summary(long_mask.value).split("mask ")[1]
    assert quoted == "`a?'" + "x" * 77 + "...`."
    assert quoted.count("`") == 2

    with pytest.raises(PolicyError) as size:
        normalize_document("AGENTS.md", b"a" * 87_040, blob_oid="f" * 40)
    assert summary(size.value).endswith("Where: file `AGENTS.md`, actual 87040, allowed 65536.")

    # A failure without a location keeps the plain text of its code.
    plain = PolicyError(PolicyErrorCode.ACCESS_DENIED, "GitHub answered 403")
    assert policy_error_detail(plain.message) is None
    assert summary(plain) == (
        "`policy_access_denied`: Нет доступа к документам правил репозитория."
    )
    assert policy_error_detail(None) is None
    assert policy_error_detail("llm_timeout: slow") is None


@pytest.mark.parametrize(
    "failure",
    [
        "cancel",
        "deadline",
        "bitbucket_limit",
        "rate_limited",
        "request_budget",
        "response_budget",
        "optional",
    ],
)
def test_context_stage_propagates_control_but_preserves_optional_fallback(failure: str) -> None:
    error = {
        "cancel": RunCancelled("installation_unavailable"),
        "deadline": RunFailure("deadline_exceeded"),
        "bitbucket_limit": RunFailure("bitbucket_review_limit_exceeded"),
        "rate_limited": RunFailure("diff_fetch_failed", "provider 429", rate_limited=True),
        "request_budget": RunFailure("bitbucket_request_budget_exceeded"),
        "response_budget": RunFailure("bitbucket_response_budget_exceeded"),
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
    if failure in {"optional", "request_budget", "response_budget"}:
        assert asyncio.run(stage.run(_RUN)) is None
        assert stage.excluded("generated/api.py")
        assert isinstance(error, RunFailure)
        assert trace.steps[-1][2]["error"]["code"] == error.error_code
    else:
        with pytest.raises(type(error)) as caught:
            asyncio.run(stage.run(_RUN))
        assert caught.value is error
    assert trace.steps[-1][2]["error"]["type"] == type(error).__name__


@pytest.mark.parametrize(
    "error_code", ["bitbucket_request_budget_exceeded", "bitbucket_response_budget_exceeded"]
)
@pytest.mark.parametrize("failure_point", ["tree", "read_blob"])
def test_real_build_records_degraded_budget_codes_in_context_build(
    error_code: str,
    failure_point: Literal["tree", "read_blob"],
) -> None:
    class DegradedSource(Repository):
        async def tree(self, revision: str) -> RevisionTree:
            if failure_point == "tree":
                raise RunFailure(error_code, "budget exhausted")
            return await super().tree(revision)

        async def read_blob(self, blob_sha: str) -> bytes:
            if failure_point == "read_blob":
                raise RunFailure(error_code, "budget exhausted")
            return await super().read_blob(blob_sha)

    trace, payloads = Trace(), Payloads()
    stage = _stage(
        trace,
        context_builder=lambda run: BuildReviewContext(
            source=DegradedSource(), parser=Parser(), counter=EstimatedTokenCounter()
        ),
        payloads=payloads,
    )
    payload = asyncio.run(stage.run(_RUN))

    assert payload is not None
    assert payload.degraded_error_codes == (error_code,)
    (build,) = [response for tool, _, response in trace.steps if tool == "context.build"]
    assert build["degraded_error_codes"] == [error_code]
    assert payloads.rows[-1][1]["degraded_error_codes"] == [error_code]


@pytest.mark.parametrize(("threshold", "cap"), [("critical", 1), ("warning", 3)])
def test_publication_settings_leave_conventions_cache_identity_unchanged(
    threshold: SeverityThresholdValue, cap: int
) -> None:
    baseline = Store()
    assert baseline.pins is not None
    changed = Store(pins=replace(baseline.pins, severity_threshold=threshold, max_comments=cap))
    first = _stage(Trace(), store=baseline)
    second = _stage(Trace(), store=changed)
    asyncio.run(first.run(_RUN))
    asyncio.run(second.run(_RUN))
    # Unchanged _stage/Policy fixture archived from baseline commit 1ae74b1.
    assert first.policy_key == "24ab4f16f70b7124f9af6fd8e6000572622dc1f868f1980616b2519fd03c9821"
    assert second.policy_key == "24ab4f16f70b7124f9af6fd8e6000572622dc1f868f1980616b2519fd03c9821"


@pytest.mark.parametrize("error_code", ["diff_fetch_failed", "commit_messages_fetch_failed"])
def test_context_stage_preserves_required_authority_failure_from_real_builder(
    error_code: Literal["diff_fetch_failed", "commit_messages_fetch_failed"],
    caplog: pytest.LogCaptureFixture,
) -> None:
    from app.modules.reviews.application.run_failures import RequiredSourceUnavailable

    failure = RequiredSourceUnavailable(error_code, retry_after=timedelta(seconds=180))

    class Unavailable(Repository):
        async def tree(self, revision: str) -> RevisionTree:
            raise failure

    trace = Trace()
    stage = _stage(
        trace,
        context_builder=lambda run: BuildReviewContext(
            source=Unavailable(), parser=Parser(), counter=EstimatedTokenCounter()
        ),
    )
    with pytest.raises(RequiredSourceUnavailable) as caught:
        asyncio.run(stage.run(_RUN))
    assert caught.value is failure
    assert caught.value.error_code == error_code
    assert caught.value.retry_after == timedelta(seconds=180)
    assert stage.excluded("generated/api.py")
    assert trace.steps[-1][2]["error"]["type"] == "RequiredSourceUnavailable"
    assert "Review context build failed" not in caplog.text


def test_real_stage_diff_identity_uses_persisted_provider() -> None:
    from dataclasses import replace

    class ProviderRuns(Runs):
        def __init__(self, provider: str) -> None:
            super().__init__(_snapshots())
            self.provider = provider

        async def get_run_vcs_input(self, run_id: UUID) -> RunVcsInput:
            value = await super().get_run_vcs_input(run_id)
            return replace(
                value,
                locator=replace(
                    value.locator,
                    provider=self.provider,
                    origin_provider_installation_id=UUID(int=99),
                    repository_external_id=17,
                ),
            )

    async def scenario(provider: str) -> ReviewContextPayload:
        store = Store()
        vcs = Vcs()
        stage = ReviewContextStage(
            runs=ProviderRuns(provider),
            checkpoint=_checkpoint,
            vcs=vcs,
            merge_base=vcs,
            prepare_policy=PreparePolicySnapshot(
                uow_factory=lambda: Uow(store), source_factory=lambda pins: Policy()
            ),
            context_builder=lambda run: BuildReviewContext(
                source=Repository(), parser=Parser(), counter=EstimatedTokenCounter()
            ),
        )
        await stage.begin(_RUN)
        value = await stage.run(_RUN)
        assert value is not None and value.status is ContextStatus.REVIEW
        assert stage.head_sha == _HEAD
        assert stage.diff_old_sha == "c" * 40
        return value

    github = asyncio.run(scenario("github"))
    gitlab = asyncio.run(scenario("gitlab"))
    assert gitlab.diff_digest != github.diff_digest


def test_real_gitlab_stage_uses_policy_start_mergebase_old_and_fork_head_sources() -> None:
    from dataclasses import replace

    import httpx

    from app.modules.integrations.gitlab.infrastructure.run_http import GitLabRunHttp
    from app.modules.reviews.infrastructure.gitlab_policy_source import GitLabPolicyDocumentSource
    from app.modules.reviews.infrastructure.gitlab_repository_tree import GitLabRepositoryTree
    from app.modules.reviews.infrastructure.gitlab_revision_source import GitLabRevisionSource
    from app.modules.reviews.infrastructure.gitlab_vcs import HttpGitLabVcsProvider
    from tests.test_gitlab_repository_sources import SourceRouter, checkpoint
    from tests.test_gitlab_run_http import INPUT, POLICY, Credentials
    from tests.test_gitlab_vcs import reply

    patch = "@@ -1,2 +1,2 @@\n def greet(name):\n-    return name\n+    return str(name)\n"
    old = b"def greet(name):\n    return name\n"
    new = b"def greet(name):\n    return str(name)\n"
    stored = [DiffSnapshot("src/app.py", patch, "d" * 40, "modified", None, 1, 1, 2)]

    class RunPort:
        async def get_run_vcs_input(self, run_id: UUID) -> RunVcsInput | None:
            return INPUT

        async def get_run_snapshots(self, run_id: UUID) -> list[DiffSnapshot] | None:
            return stored

    async def scenario() -> None:
        router = SourceRouter()
        router.files["src/app.py"] = ("d" * 40, new)
        router.vcs.canonical["changes_count"] = "1"
        router.vcs.files = [
            {
                "old_path": "src/app.py",
                "new_path": "src/app.py",
                "new_file": False,
                "deleted_file": False,
                "renamed_file": False,
                "diff": patch,
            }
        ]

        def external(request: httpx.Request) -> httpx.Response:
            if (
                request.url.path.endswith("/repository/tree")
                and request.url.params.get("ref") == "c" * 40
            ):
                router.requests.append(request)
                return reply(
                    [
                        {
                            "id": "4" * 40,
                            "path": "src/app.py",
                            "name": "app.py",
                            "type": "blob",
                            "mode": "100644",
                        }
                    ]
                )
            if request.method == "HEAD" and request.url.params.get("ref") == "c" * 40:
                router.requests.append(request)
                return reply(
                    raw=b"",
                    headers={
                        "X-Gitlab-Blob-Id": "4" * 40,
                        "X-Gitlab-Size": str(len(old)),
                        "X-Gitlab-Ref": "c" * 40,
                    },
                )
            if "/repository/blobs/" + "4" * 40 + "/" in request.url.path:
                router.requests.append(request)
                return reply(raw=old)
            return router.response(request)

        async with GitLabRunHttp(
            credentials=Credentials(), origin_policy=POLICY, transport=httpx.MockTransport(external)
        ) as http:
            trees = GitLabRepositoryTree(http=http, checkpoint=checkpoint)
            vcs = HttpGitLabVcsProvider(http=http)
            revision = GitLabRevisionSource(trees=trees, locator=INPUT.locator)
            store = Store()
            assert store.pins is not None
            store.pins = replace(store.pins, repository_id=INPUT.repository_id, run_id=_RUN)
            stage = ReviewContextStage(
                runs=RunPort(),
                checkpoint=checkpoint,
                vcs=vcs,
                merge_base=vcs,
                prepare_policy=PreparePolicySnapshot(
                    uow_factory=lambda: Uow(store),
                    source_factory=lambda pins: GitLabPolicyDocumentSource(
                        trees=trees, policy_base_sha=pins.policy_base_sha
                    ),
                ),
                context_builder=lambda run: BuildReviewContext(
                    source=revision, parser=Parser(), counter=EstimatedTokenCounter()
                ),
            )
            await stage.begin(_RUN)
            payload = await stage.run(_RUN)
            assert payload is not None and payload.status is ContextStatus.REVIEW
            # Literal DiffMap/v1 contract fixture: MR12, repository ...0003,
            # head a40/start b40/mergebase c40, modified src/app.py/blob d40.
            # Established independently from canonical fixture JSON with stdlib
            # SHA256; no production builder participates in this expected value.
            assert payload.diff_digest == (
                "eb5a4694260256582fbc26767c001e976cdcc7a9282c965a279ed337f90764a3"
            )
            refs = [
                source
                for block in payload.blocks
                for source in (block.source, *block.related_sources)
            ]
            assert any(
                source.side is DiffSide.LEFT and source.revision == "c" * 40 for source in refs
            )
            assert any(
                source.side is DiffSide.RIGHT and source.revision == INPUT.head_sha
                for source in refs
            )
            old_tree = await revision.tree("c" * 40)
            assert old_tree.complete and old_tree.blob_shas["src/app.py"] == "4" * 40
            assert await revision.read_blob("4" * 40) == old
            assert store.snapshot is not None and store.snapshot.policy_base_sha == INPUT.base_sha
        assert any(
            "/projects/17/repository/tree" in str(request.url)
            and request.url.params.get("ref") == INPUT.base_sha
            for request in router.requests
        )
        assert any(
            "/projects/17/repository/tree" in str(request.url)
            and request.url.params.get("ref") == "c" * 40
            for request in router.requests
        )
        assert any(
            "/projects/29/repository/tree" in str(request.url)
            and request.url.params.get("ref") == INPUT.head_sha
            for request in router.requests
        )

    asyncio.run(scenario())


class _AllIgnoredByTheBuild(BuildReviewContext):
    """A build that itself finds every change ignored, after the stage's own check."""

    async def execute(self, **kwargs: Any) -> ReviewContextPayload:
        payload = await super().execute(**kwargs)
        return replace(payload, status=ContextStatus.ALL_CHANGES_IGNORED)


class _BrokenBuild:
    async def execute(self, **kwargs: object) -> object:
        raise RuntimeError("the tree is unavailable")


@pytest.mark.parametrize("kind", ["review", "summary_only", "rag"])
def test_stage_saves_its_context_build_response_as_the_context_payload(
    kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    trace, payloads = Trace(), Payloads()
    if kind == "rag":
        monkeypatch.setattr(rag_index, "RETRIEVED_PROMPT_VERSIONS", frozenset({_PINNED}))
        gate, _ = _gate(_readiness(RagIndexStatus.READY))
        builder = _rag_builder(_greet_hints("src/main.py"))
        stage = _stage(trace, rag=gate, context_builder=builder, payloads=payloads)
    elif kind == "summary_only":
        # The pull request reports two files; only the ignored one is known.
        stage = _stage(trace, snapshots=[_snapshots()[1]], payloads=payloads)
    else:
        stage = _stage(trace, payloads=payloads)

    if kind == "summary_only":
        with pytest.raises(SummaryOnlyReview):
            asyncio.run(stage.run(_RUN))
    else:
        payload = asyncio.run(stage.run(_RUN))
        assert payload is not None and stage.payload is payload

    (build,) = [response for tool, _, response in trace.steps if tool == "context.build"]
    # The one row of the attempt is the build's own trace response, key for key.
    assert payloads.rows == [(_RUN, build)]
    assert (build["status"], build["reason"]) == {
        "review": ("review", None),
        "summary_only": ("summary_only", "diff_inventory_incomplete"),
        "rag": ("review", None),
    }[kind]
    if kind == "summary_only":
        assert build["blocks"] == []
    else:
        assert build["omissions"] == [
            {"reason": "ignored", "path": None, "count": 1, "diagnostic_path": None}
        ]
        assert [block["level"] for block in build["blocks"]].count("RAG") == (kind == "rag")


@pytest.mark.parametrize(
    "case",
    ["no_changes", "ignored_before_the_build", "ignored_by_the_build", "build_failure"],
)
def test_stage_saves_no_context_payload_on_a_skip_or_a_caught_build_failure(case: str) -> None:
    trace, payloads = Trace(), Payloads()
    if case == "no_changes":
        stage = _stage(trace, vcs=Vcs(files_changed=0), snapshots=[], empty=True, payloads=payloads)
    elif case == "ignored_before_the_build":
        stage = _stage(
            trace, policy=Policy(rules=b"```review-ignore\n**\n```\n"), payloads=payloads
        )
    elif case == "ignored_by_the_build":
        stage = _stage(
            trace,
            context_builder=lambda run: _AllIgnoredByTheBuild(
                source=Repository(), parser=Parser(), counter=EstimatedTokenCounter()
            ),
            payloads=payloads,
        )
    else:
        stage = _stage(trace, payloads=payloads)
        stage._context_builder = lambda run: _BrokenBuild()  # type: ignore[assignment,return-value]

    if case == "build_failure":
        assert asyncio.run(stage.run(_RUN)) is None
    else:
        with pytest.raises(RunSkipped):
            asyncio.run(stage.run(_RUN))

    # The attempt reached its context.build record, and no context_payloads row was written.
    last = trace.steps[-1]
    assert last[0] == "context.build"
    assert (last[2].get("status"), "error" in last[2]) == {
        "no_changes": ("no_changes", False),
        "ignored_before_the_build": ("all_changes_ignored", False),
        "ignored_by_the_build": ("all_changes_ignored", False),
        "build_failure": (None, True),
    }[case]
    assert payloads.rows == []
    assert stage.payload is None


def test_a_failed_context_payload_write_fails_the_attempt() -> None:
    trace = Trace()
    stage = _stage(trace, payloads=Payloads(error=RuntimeError("context_payloads is down")))

    # Not an optional build failure: the attempt ends, and no payload is kept for the review.
    with pytest.raises(RuntimeError, match="context_payloads is down"):
        asyncio.run(stage.run(_RUN))

    assert stage.payload is None
    assert [step[0] for step in trace.steps] == ["policy.load", "context.build"]
    assert trace.steps[1][2]["status"] == "review"


def test_neither_the_context_build_nor_its_payload_names_an_ignored_file() -> None:
    trace, payloads = Trace(), Payloads()

    asyncio.run(_stage(trace, payloads=payloads).run(_RUN))

    # Exactly one write, so the absence below is not that of a missing row.
    [(run_id, summary)] = payloads.rows
    assert run_id == _RUN
    # ``generated/api.py`` is ignored by the policy: counted, never named.
    assert summary["ignored_files"] == 1
    assert summary["omissions"] == [
        {"reason": "ignored", "path": None, "count": 1, "diagnostic_path": None}
    ]
    assert "generated" not in repr(payloads.rows)
    assert "generated" not in repr(trace.steps)


def test_context_trace_escapes_the_diagnostic_path_of_an_unsupported_name() -> None:
    raw = "src\\bad" + chr(0x202E) + ".py"
    rejected = DiffSnapshot(
        raw, _stored("bad.py", "@@ -1 +1 @@\n-a\n+b"), "f" * 40, "modified", None, 1, 1, 2
    )
    trace, payloads = Trace(), Payloads()
    stage = _stage(
        trace, snapshots=[*_snapshots(), rejected], vcs=Vcs(files_changed=3), payloads=payloads
    )

    payload = asyncio.run(stage.run(_RUN))

    assert payload is not None
    # The payload keeps the provider's name as it came; only the trace text is escaped.
    assert [
        item.diagnostic_path for item in payload.omissions if item.reason == "unsupported_path"
    ] == [raw]
    (build,) = [response for tool, _, response in trace.steps if tool == "context.build"]
    assert [item for item in build["omissions"] if item["reason"] == "unsupported_path"] == [
        {
            "reason": "unsupported_path",
            "path": None,
            "count": 1,
            "diagnostic_path": "src\\\\bad\\u202e.py",
        }
    ]
    assert payloads.rows == [(_RUN, build)]
