"""Flagged policy and context stage: recorded in the trace, never failing the review."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

import httpx
import pytest

from app.modules.reviews.application.ast_resolution import RevisionTree
from app.modules.reviews.application.ast_symbols import (
    AstFacts,
    AstFailureReason,
    ParseRequest,
)
from app.modules.reviews.application.execute_review import ExecuteReviewRun
from app.modules.reviews.application.get_run_diff import DiffSnapshot
from app.modules.reviews.application.policy_documents import PolicyEntryKind, PolicyTreeEntry
from app.modules.reviews.application.policy_errors import PolicyError, PolicyErrorCode
from app.modules.reviews.application.prepare_policy_snapshot import PreparePolicySnapshot
from app.modules.reviews.application.process_run import RunVcsInput
from app.modules.reviews.application.prompt_builder import PullRequestMeta
from app.modules.reviews.application.review_context import BuildReviewContext, ContextStatus
from app.modules.reviews.application.review_context_stage import (
    EstimatedTokenCounter,
    ReviewContextStage,
)
from app.modules.reviews.application.vcs_diff import PullRequestLocator, VcsPullRequest
from app.modules.reviews.infrastructure.tree_sitter_facts import extract_facts
from app.worker import WorkerSettings
from tests.test_prepare_policy_snapshot import Store, Uow

_RUN = UUID("00000000-0000-0000-0000-000000000002")
_HEAD = "a" * 40
_BASE = "b" * 40
_LOCATOR = PullRequestLocator(17, "octo/repo", 7)
_SOURCE = "def greet(name):\n    extra = 1\n    return name\n"
_PATCH = "@@ -1,2 +1,3 @@\n def greet(name):\n+    extra = 1\n     return name"


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

    async def get_pull_request(self, locator: PullRequestLocator) -> VcsPullRequest:
        meta = PullRequestMeta(
            title="t",
            description=None,
            author="octo",
            source_branch="f",
            target_branch="main",
            labels=(),
            files_changed=2,
            lines_added=0,
            lines_removed=0,
            is_draft=False,
            is_fork=False,
        )
        return VcsPullRequest(locator, self.head_sha, _BASE, meta)

    async def get_merge_base(self, pull_request: VcsPullRequest) -> str:
        if isinstance(self.merge_base, Exception):
            raise self.merge_base
        return self.merge_base

    async def get_diff(self, pull_request: VcsPullRequest) -> tuple[Any, ...]:
        raise AssertionError("the stored diff is reused")

    async def get_blob(self, locator: PullRequestLocator, sha: str) -> bytes:
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
) -> ReviewContextStage:
    shared = store or Store()
    provider = vcs or Vcs()
    return ReviewContextStage(
        runs=Runs(None if none else (snapshots or _snapshots())),
        vcs=provider,
        merge_base=provider,
        prepare_policy=PreparePolicySnapshot(
            uow_factory=lambda: Uow(shared), source_factory=lambda pins: policy or Policy()
        ),
        context_builder=lambda run: BuildReviewContext(
            source=Repository(), parser=Parser(), counter=EstimatedTokenCounter()
        ),
        trace=trace,
    )


def test_stage_records_the_policy_and_the_context_without_any_document_or_code_text() -> None:
    trace = Trace()

    payload = asyncio.run(_stage(trace).execute(_RUN))

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
    assert context["omissions"] == [{"reason": "ignored", "path": None, "count": 1}]
    assert {block["path"] for block in context["blocks"]} == {"src/app.py"}
    assert context["blocks"][0]["ranges"] == [[1, 3]]
    recorded = repr(trace.steps)
    assert "extra = 1" not in recorded
    assert "generated" not in recorded


def test_retry_reuses_the_stored_policy_snapshot() -> None:
    store = Store()
    first = Trace()
    asyncio.run(_stage(first, store=store).execute(_RUN))

    changed = Policy(rules=b"# other rules\n")
    second = Trace()
    asyncio.run(_stage(second, store=store, policy=changed).execute(_RUN))

    assert second.steps[0][2]["policy_digest"] == first.steps[0][2]["policy_digest"]
    assert second.steps[0][2]["ignore_patterns"] == 1


def test_unknown_merge_base_is_recorded_as_unknown_not_as_the_policy_base() -> None:
    trace = Trace()
    failing = Vcs(merge_base=httpx.ConnectError("down"))

    asyncio.run(_stage(trace, vcs=failing).execute(_RUN))

    assert trace.steps[1][2]["diff"]["diff_old_sha"] is None


def test_observe_swallows_a_policy_error_and_leaves_it_in_the_trace() -> None:
    trace = Trace()
    broken = Policy(error=PolicyError(PolicyErrorCode.ACCESS_DENIED, "GitHub answered 403"))

    assert asyncio.run(_stage(trace, policy=broken).observe(_RUN)) is None

    assert [step[0] for step in trace.steps] == ["policy.load"]
    assert trace.steps[0][2]["error"]["type"] == "PolicyError"
    assert "policy_access_denied" in trace.steps[0][2]["error"]["message"]


def test_execute_raises_what_observe_swallows() -> None:
    with pytest.raises(ValueError, match="head changed"):
        asyncio.run(_stage(Trace(), vcs=Vcs(head_sha="f" * 40)).execute(_RUN))
    with pytest.raises(LookupError, match="no stored diff"):
        asyncio.run(_stage(Trace(), none=True).execute(_RUN))
    assert asyncio.run(_stage(Trace(), none=True).observe(_RUN)) is None


def test_review_run_calls_the_stage_after_the_diff_is_stored_and_ignores_its_result() -> None:
    from app.modules.reviews.application.conventions import GeneratedConventions

    calls: list[str] = []

    class Processor:
        async def prepare(self, run_id: UUID) -> object:
            calls.append("prepare")
            return GeneratedConventions.__new__(GeneratedConventions)

    class Observer:
        async def observe(self, run_id: UUID) -> object:
            calls.append("observe")
            return None

    class Prompts:
        async def get_review_prompt_input(self, run_id: UUID, conventions: object) -> None:
            calls.append("prompt")
            return None

    def run(observer: Observer | None) -> bool:
        pipeline = ExecuteReviewRun(
            Processor(),  # type: ignore[arg-type]
            Prompts(),
            object(),  # type: ignore[arg-type]
            object(),  # type: ignore[arg-type]
            context_stage=observer,
        )
        return asyncio.run(pipeline.execute(_RUN))

    assert run(Observer()) is False
    assert calls == ["prepare", "observe", "prompt"]
    calls.clear()
    assert run(None) is False
    assert calls == ["prepare", "prompt"]


def test_feature_flag_is_off_unless_explicitly_enabled() -> None:
    env = {"DATABASE_URL": "postgresql://x", "RABBITMQ_URL": "amqp://x"}

    assert WorkerSettings.from_environment(env).review_context_pipeline is False
    for value in ("1", "true", "TRUE", "yes"):
        enabled = WorkerSettings.from_environment(
            {**env, "REVIEW_CONTEXT_PIPELINE_ENABLED": value, "REDIS_URL": "redis://r:6379/0"}
        )
        assert enabled.review_context_pipeline is True
        assert enabled.redis_url == "redis://r:6379/0"
    for value in ("", "0", "false", "off"):
        disabled = WorkerSettings.from_environment(
            {**env, "REVIEW_CONTEXT_PIPELINE_ENABLED": value}
        )
        assert disabled.review_context_pipeline is False


def test_stored_patch_without_hunks_and_an_unwrapped_patch_are_both_accepted() -> None:
    header_only = "diff --git a/img.png b/img.png\n--- a/img.png\n+++ b/img.png\n"
    snapshots = [
        DiffSnapshot("src/app.py", _PATCH, "d" * 40, "modified", None, 1, 0, 1),
        DiffSnapshot("img.png", header_only, "e" * 40, "modified", None, 0, 0, 0),
    ]
    trace = Trace()

    asyncio.run(_stage(trace, snapshots=snapshots).execute(_RUN))

    context = trace.steps[1][2]
    assert context["status"] == "review"
    assert context["omissions"] == [
        {"reason": "patch_metadata_only", "path": "img.png", "count": 1}
    ]
