"""Shared fakes and stage builders for review_context_stage tests."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from app.modules.code_index.domain.enums import IndexStatus, RagIndexStatus
from app.modules.code_index.domain.rag_index import RagIndex
from app.modules.reviews.application.ast_resolution import RevisionTree
from app.modules.reviews.application.ast_symbols import AstFacts, AstFailureReason, ParseRequest
from app.modules.reviews.application.check_runs import RunProviderCheckpoint
from app.modules.reviews.application.context_provenance import LineRange, SourceRef, SourceRole
from app.modules.reviews.application.diff_map import DiffSide
from app.modules.reviews.application.get_run_diff import DiffSnapshot
from app.modules.reviews.application.graph_retriever import GraphRetriever
from app.modules.reviews.application.policy_documents import PolicyEntryKind, PolicyTreeEntry
from app.modules.reviews.application.policy_errors import PolicyError
from app.modules.reviews.application.prepare_policy_snapshot import PreparePolicySnapshot
from app.modules.reviews.application.process_run import RunVcsInput
from app.modules.reviews.application.prompt_builder import PullRequestMeta
from app.modules.reviews.application.rag_context import CachedBlobSource
from app.modules.reviews.application.rag_index import (
    OpenReviewIndex,
    RagReadiness,
    ReviewIndexAccess,
)
from app.modules.reviews.application.review_context import (
    BuildReviewContext,
    ContextBlock,
    ContextLevel,
    Representation,
)
from app.modules.reviews.application.review_context_stage import (
    EstimatedTokenCounter,
    ReviewContextStage,
)
from app.modules.reviews.application.run_trace import ContextPayloadStore, RunTrace
from app.modules.reviews.application.vcs_diff import PullRequestLocator, VcsPullRequest
from app.modules.reviews.infrastructure.tree_sitter_facts import extract_facts
from tests.policy_snapshot_fakes import Store, Uow
from tests.rag_context_fakes import BlobCacheUow, BlobStore, Graph, SharedBlobCache, _uses

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


@dataclass
class Payloads:
    """``context_payloads`` of the run: every write the stage made, or a failing store."""

    rows: list[tuple[UUID, dict[str, Any]]] = field(default_factory=list)
    error: Exception | None = None

    snapshots: list[DiffSnapshot] | None = None

    async def save_context_payload(self, run_id: UUID, summary: dict[str, Any]) -> None:
        if self.error is not None:
            raise self.error
        self.rows.append((run_id, summary))

    async def save_summary_only_stop(
        self,
        run_id: UUID,
        summarize: Callable[[list[DiffSnapshot]], dict[str, Any]],
    ) -> dict[str, Any]:
        if self.error is not None:
            raise self.error
        if self.snapshots is None:
            raise AssertionError("the summary-only stop reloads stored rows")
        marked = _mark_summary_only_rows(self.snapshots)
        self.snapshots[:] = marked
        summary = summarize(self.snapshots)
        self.rows.append((run_id, summary))
        return summary


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


def _mark_summary_only_rows(snapshots: list[DiffSnapshot]) -> list[DiffSnapshot]:
    """In-memory stand-in for the store's summary-only UPDATE.

    A withheld row (no model patch, no omission) and a proven empty file stay, and so
    does a row the line threshold already flagged. Every other row loses its model patch.
    """
    marked: list[DiffSnapshot] = []
    for item in snapshots:
        withheld = (
            item.review_patch is None and item.omission_reason is None and not item.summary_only
        )
        if withheld or item.summary_only or item.omission_reason == "empty_file":
            marked.append(item)
        else:
            marked.append(replace(item, summary_only=True, review_patch=None))
    return marked


def _stage(
    trace: RunTrace,
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
    payloads: ContextPayloadStore | None = None,
) -> ReviewContextStage:
    shared = store or Store()
    provider = vcs or Vcs()
    held = None if none else [] if empty else list(snapshots or _snapshots())
    if isinstance(payloads, Payloads) and held is not None:
        payloads.snapshots = held
    return ReviewContextStage(
        checkpoint=_checkpoint,
        runs=Runs(held),
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
        payloads=payloads,
    )


def _block(block_id: str, level: ContextLevel, role: SourceRole, tokens: int) -> ContextBlock:
    source = SourceRef(
        role, UUID(int=5), _HEAD, "src/app.py", None, (), DiffSide.RIGHT, (LineRange(1, 3),), "t"
    )
    return ContextBlock(
        block_id, level, None, source, Representation.EXACT_LINES, "x", tokens, False
    )


# Local gate stub: tests/test_rag_readiness.py owns its copy during open PR #600.
class _StubReviewIndexAccess:
    def __init__(self, readiness: RagReadiness) -> None:
        self._readiness = readiness
        self.opened: list[UUID] = []

    async def open_for_review(self, repository_id: UUID) -> RagReadiness:
        self.opened.append(repository_id)
        return self._readiness


class _FakeReviewIndexUow:
    def __init__(self, review_index: ReviewIndexAccess) -> None:
        self.review_index = review_index
        self.commits = 0
        self.rolled_back = False

    async def __aenter__(self) -> _FakeReviewIndexUow:
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        if exc_type is not None:
            self.rolled_back = True

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rolled_back = True


_REPOSITORY = UUID(int=1)
_INDEXED = "6" * 40
_TARGET = "7" * 40
_USABLE = (RagIndexStatus.READY, RagIndexStatus.STALE)
_IMPORTER = 'from .app import greet\n\n\ndef main():\n    return greet("octo")\n'
_IMPORTER_BLOB = "f" * 40
_PINNED = ("review.system", "v2")


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


def _gate(readiness: RagReadiness) -> tuple[OpenReviewIndex, _StubReviewIndexAccess]:
    access = _StubReviewIndexAccess(readiness)
    return OpenReviewIndex(lambda: _FakeReviewIndexUow(access)), access


class RagRepository(Repository):
    """The head of the changed ``src/app.py`` and of ``src/main.py``, which calls its
    ``greet``."""

    def __init__(self, tree_error: Exception | None = None) -> None:
        self._tree_error = tree_error

    async def tree(self, revision: str) -> RevisionTree:
        if self._tree_error is not None:
            raise self._tree_error
        files = {"src/app.py": "d" * 40, "src/main.py": _IMPORTER_BLOB}
        return RevisionTree(revision, files, complete=True)

    async def read_blob(self, blob_sha: str) -> bytes:
        return (_IMPORTER if blob_sha == _IMPORTER_BLOB else _SOURCE).encode()


def _greet_hints(*paths: str, status: IndexStatus = IndexStatus.READY) -> Graph:
    """The graph: each path uses the changed ``greet``."""
    return Graph({_REPOSITORY: tuple(_uses(path, "src/app.py", "greet") for path in paths)}, status)


def _rag_builder(
    retriever: GraphRetriever, repository: Repository | None = None
) -> Callable[[RunVcsInput], BuildReviewContext]:
    """The context builder of a run with the RAG sources of the run."""

    def build(run: RunVcsInput) -> BuildReviewContext:
        source = repository or RagRepository()
        store = BlobStore()
        return BuildReviewContext(
            source=source,
            parser=Parser(),
            counter=EstimatedTokenCounter(),
            retriever=retriever,
            rag_blobs=CachedBlobSource(
                repository_id=run.repository_id,
                source=source,
                cache=SharedBlobCache(store),
                uow_factory=lambda: BlobCacheUow(store),
            ),
        )

    return build
