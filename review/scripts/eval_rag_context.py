"""Offline context of the #146 A/B arms (graph RAG v1 contribution eval).

Builds the review input of one case for arm A (no RAG) or arm B (RAG) by running the
**production** ``BuildReviewContext`` over in-memory sources: no database, no GitHub, no
model. This is the Task 1 spike: one fixture case, a hand-written frozen graph. The pieces
that Tasks 2-4 replace are named in their docstrings.

Fixture layout (``case_dir``): ``base/`` the repository at the case revision, ``head/`` the
same with the PR applied (Task 2 derives it from ``diff.patch``), ``diff.patch`` a ``git
diff`` of the PR.

Blocked or private seams the spike met (the Task 3 refactor decision) are listed in
``docs/plans/146-todo.md`` under Task 1.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from types import TracebackType
from typing import Literal, Self
from uuid import UUID

from app.modules.code_index.domain.enums import IndexStatus
from app.modules.code_index.domain.rag_index import rag_index
from app.modules.reviews.application.ast_resolution import RevisionTree
from app.modules.reviews.application.ast_symbols import AstFacts, AstFailureReason, ParseRequest
from app.modules.reviews.application.diff_map import DiffMap, VcsProviderName, build_diff_map
from app.modules.reviews.application.get_run_file_lines import (
    BlobCacheEntry,
    BlobCacheKey,
    BlobCacheStatus,
)
from app.modules.reviews.application.graph_retriever import GraphHint, GraphHints, GraphTarget
from app.modules.reviews.application.policy_documents import missing_document
from app.modules.reviews.application.policy_errors import PolicyError, PolicyErrorCode
from app.modules.reviews.application.policy_snapshot import (
    AGENTS_DOCUMENT_PATH,
    PINNED_PROMPT_CHECKSUMS,
    RULES_DOCUMENT_PATH,
    CallManifest,
    ExecutionProfile,
    PolicySnapshot,
    PromptBindings,
)
from app.modules.reviews.application.prepare_policy_snapshot import (
    RunPolicyPins,
    build_policy_snapshot,
)
from app.modules.reviews.application.prompt_budget import (
    token_counter_for,
    verify_provenance_pre_send_guard,
)
from app.modules.reviews.application.prompt_builder import (
    PromptBuilder,
    PullRequestMeta,
    RepoConventions,
    ReviewContext,
    ReviewPrompt,
    parse_unified_diff,
)
from app.modules.reviews.application.rag_context import CachedBlobSource
from app.modules.reviews.application.rag_index import has_retrieved_section
from app.modules.reviews.application.review_context import (
    BuildReviewContext,
    ContextLevel,
    RagPlan,
    ReviewContextPayload,
    snapshot_block,
    snapshot_retrieved,
)
from app.modules.reviews.application.review_context_stage import FAST_CONTEXT_TOKEN_BUDGET
from app.modules.reviews.application.review_ignore import (
    IgnoreMatcher,
    apply_ignore,
    parse_review_ignore,
)
from app.modules.reviews.application.vcs_diff import PullRequestLocator, VcsFile, VcsPullRequest
from app.modules.reviews.infrastructure.tree_sitter_facts import extract_facts

Arm = Literal["A", "B"]

# The pinned review prompt both arms run (Р-6, #82): v5, the only one with a retrieved section.
_PROMPT_KEY = "review.system:v5"
_SPIKE_REVIEW_MODEL = "mistral-small-4"
# L2-L4 are the excerpt levels the snapshot path shows; L1 is ``changed_files``
# (``execute_review._EXCERPT_LEVELS``, private there).
_EXCERPT_LEVELS = frozenset({ContextLevel.L2, ContextLevel.L3, ContextLevel.L4})

_REPOSITORY_ID = UUID(int=146)
# The manifest of a case is reproducible: no random call id.
_CALL_ID = UUID(int=1460)


@dataclass(frozen=True)
class ArmInput:
    """What one arm sends: the payload of the production build, the prompt input mapped
    from it as ``ExecuteReviewRun`` maps it, the rendered v5 prompt and its manifest."""

    payload: ReviewContextPayload
    context: ReviewContext
    prompt: ReviewPrompt
    manifest: CallManifest


def git_blob_oid(raw: bytes) -> str:
    """The git blob object id of ``raw`` (what the provider names a file by)."""
    header = b"blob %d\0" % len(raw)
    return hashlib.sha1(header + raw, usedforsecurity=False).hexdigest()


def _commit_id(case_id: str, side: str) -> str:
    """Synthetic 40-hex revision id, stable per case (Task 2 keeps this derivation)."""
    return hashlib.sha1(f"{case_id}:{side}".encode(), usedforsecurity=False).hexdigest()


def _read_tree(root: Path) -> dict[str, bytes]:
    # A bytecode cache left by a run over the fixture is not a file of the repository.
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"
    }


@dataclass
class SnapshotRevisionSource:
    """``RevisionSource`` over two in-memory trees: ``base`` and ``head`` by revision id."""

    trees: Mapping[str, Mapping[str, bytes]]
    complete: bool = True
    _blobs: dict[str, bytes] = field(init=False, default_factory=dict)

    def __post_init__(self) -> None:
        for files in self.trees.values():
            for raw in files.values():
                self._blobs[git_blob_oid(raw)] = raw

    async def tree(self, revision: str) -> RevisionTree:
        files = self.trees[revision]
        return RevisionTree(
            revision, {path: git_blob_oid(raw) for path, raw in files.items()}, self.complete
        )

    async def read_blob(self, blob_sha: str) -> bytes:
        return self._blobs[blob_sha]


class InProcessParser:
    """``SourceParser`` over ``extract_facts``: tree-sitter in this process, no subprocess."""

    async def parse_many(
        self, requests: tuple[ParseRequest, ...]
    ) -> dict[str, AstFacts | AstFailureReason]:
        return {
            request.key: extract_facts(request.language, request.source) for request in requests
        }

    async def aclose(self) -> None:
        return None


class MemoryBlobCache:
    """``BlobCache`` that keeps nothing: every blob is read from the snapshot source."""

    async def get(self, key: BlobCacheKey) -> BlobCacheEntry:
        return BlobCacheEntry(status=BlobCacheStatus.MISS, content=None)

    async def put(self, key: BlobCacheKey, content: str, *, ttl: timedelta) -> None:
        return None


class MemoryBlobUnitOfWork:
    """The unit of work of ``CachedBlobSource`` without a database."""

    @property
    def blob_cache(self) -> MemoryBlobCache:
        return MemoryBlobCache()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None

    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        return None


class FrozenGraphRetriever:
    """``GraphRetriever`` over a fixed list of reverse edges, hand-written in the spike.

    Same contract as the SQL retriever: a hint is returned only for a target that was asked
    for, in target order. Task 3 replaces the list with the frozen ``index.json``.
    """

    def __init__(self, hints: Sequence[GraphHint], *, indexed_commit_sha: str) -> None:
        self._hints = tuple(hints)
        self._indexed_commit_sha = indexed_commit_sha

    async def read_hints(self, repository_id: UUID, targets: Sequence[GraphTarget]) -> GraphHints:
        index = rag_index(
            rag_enabled=True,
            status=IndexStatus.READY,
            indexed_commit_sha=self._indexed_commit_sha,
            target_commit_sha=self._indexed_commit_sha,
            published_at=None,
            file_count=0,
            symbol_count=0,
            error_code=None,
            last_error_code=None,
        )
        return GraphHints(
            index,
            tuple(
                hint
                for target in targets
                for hint in self._hints
                if (hint.target_path, hint.target_name) == (target.path, target.name)
            ),
        )


_FILE_HEADER = re.compile(r"^diff --git a/(?P<old>\S+) b/(?P<new>\S+)$", re.MULTILINE)


def _vcs_files(diff: str, head: Mapping[str, bytes]) -> tuple[VcsFile, ...]:
    """One ``VcsFile`` per file of ``diff``: hunk-only patch, as the provider lists it.

    The spike handles modified files only; Task 2 adds added/removed/renamed.
    """
    headers = list(_FILE_HEADER.finditer(diff))
    files: list[VcsFile] = []
    for position, header in enumerate(headers):
        end = headers[position + 1].start() if position + 1 < len(headers) else len(diff)
        section = diff[header.end() : end]
        patch = section[section.index("@@") :].rstrip("\n")
        lines = patch.split("\n")
        additions = sum(line.startswith("+") for line in lines)
        deletions = sum(line.startswith("-") for line in lines)
        path = header.group("new")
        files.append(
            VcsFile(
                filename=path,
                status="modified",
                blob_sha=git_blob_oid(head[path]),
                previous_filename=None,
                additions=additions,
                deletions=deletions,
                changes=additions + deletions,
                patch=patch,
            )
        )
    return tuple(files)


def _diff_map(
    files: tuple[VcsFile, ...], *, head_id: str, base_id: str, meta: PullRequestMeta
) -> DiffMap:
    pull_request = VcsPullRequest(
        locator=PullRequestLocator(146, "eval/rag", 1),
        head_sha=head_id,
        base_sha=base_id,
        meta=meta,
    )
    return build_diff_map(
        pull_request,
        files,
        provider=VcsProviderName.GITHUB,
        repository_id=_REPOSITORY_ID,
        diff_old_sha=base_id,
    )


def _policy_snapshot(system: bytes) -> PolicySnapshot:
    """The arm-``none`` snapshot: no repository rules, no AGENTS.md, the pinned v5 prompt.

    ``system`` is the file of the prompt, checked against its pin before it is bound
    (as ``eval_live._prompt_bindings`` does, by the SHA-256 of the bytes).

    The eval's own ``eval_live._policy_snapshot`` is private and reads policy files from
    the case tree; the spike builds the same shape through the public builder.
    """
    checksum = hashlib.sha256(system).hexdigest()
    if checksum != PINNED_PROMPT_CHECKSUMS[_PROMPT_KEY]:
        raise PolicyError(
            PolicyErrorCode.SNAPSHOT_INTEGRITY_ERROR,
            f"{_PROMPT_KEY} has checksum {checksum}, pinned {PINNED_PROMPT_CHECKSUMS[_PROMPT_KEY]}",
        )
    ignore = parse_review_ignore(None)
    bindings = PromptBindings(
        review_system_checksum=checksum,
        conventions_checksum=PINNED_PROMPT_CHECKSUMS["review.conventions:v2"],
        verifier_checksum=PINNED_PROMPT_CHECKSUMS["review.verifier:v2"],
        review_system_version="v5",
        review_system_id=UUID(int=1),
        conventions_id=UUID(int=2),
        verifier_id=UUID(int=3),
    )
    profile = ExecutionProfile(
        "fast",
        _SPIKE_REVIEW_MODEL,
        _SPIKE_REVIEW_MODEL,
        max_input_tokens=60_000,
        primary_provider_route="primary",
        tokenizer_version="tokenizer/v1",
        counter_version="counter/v2",
        temperature="0.0",
        response_schema_version="review-output/v2",
        recovery_profile_version="recovery/v1",
        budget_profile_version="budget/v1",
    )
    return build_policy_snapshot(
        RunPolicyPins(
            UUID(int=2),
            _REPOSITORY_ID,
            "0" * 40,
            UUID(int=4),
            "2" * 64,
            bindings,
            profile,
        ),
        rules=missing_document(RULES_DOCUMENT_PATH),
        agents=missing_document(AGENTS_DOCUMENT_PATH),
        ignore=ignore,
        snapshot_id=UUID(int=3),
    )


async def build_arm_input(case_dir: Path, arm: Arm, hints: Sequence[GraphHint]) -> ArmInput:
    """The input of ``arm`` for the fixture case in ``case_dir``.

    Both arms run the same production build; arm A passes ``rag=None``, arm B a ``RagPlan``
    with the frozen retriever. Raises ``PolicyError`` if the gateway's provenance guard
    would refuse the call.
    """
    if arm not in ("A", "B"):
        raise ValueError(f"unknown arm {arm!r}: expected 'A' or 'B'")
    case_id = case_dir.name
    base_id, head_id = _commit_id(case_id, "base"), _commit_id(case_id, "head")
    base, head = _read_tree(case_dir / "base"), _read_tree(case_dir / "head")
    diff = (case_dir / "diff.patch").read_text(encoding="utf-8")
    changed = parse_unified_diff(diff)
    meta = PullRequestMeta(
        title="Review proposed change",
        description=None,
        author="gold-corpus",
        source_branch="eval",
        target_branch="main",
        labels=(),
        files_changed=len(changed),
        lines_added=sum(line.type == "added" for file in changed for line in file.lines),
        lines_removed=sum(line.type == "removed" for file in changed for line in file.lines),
        is_draft=False,
        is_fork=False,
        # The provenance guard accepts PR-side revisions only against the head id.
        head_sha=head_id,
        commit_messages=(),
        commit_messages_incomplete=False,
    )
    diff_map = _diff_map(_vcs_files(diff, head), head_id=head_id, base_id=base_id, meta=meta)

    system_bytes = (
        Path(__file__).resolve().parents[1] / "prompts" / "review.system.v5.md"
    ).read_bytes()
    snapshot = _policy_snapshot(system_bytes)
    system = system_bytes.decode("utf-8")
    source = SnapshotRevisionSource({base_id: base, head_id: head})
    retriever = FrozenGraphRetriever(hints, indexed_commit_sha=base_id)
    blobs = CachedBlobSource(
        repository_id=_REPOSITORY_ID,
        source=source,
        cache=MemoryBlobCache(),
        uow_factory=MemoryBlobUnitOfWork,
    )
    # The counter the stage pins from the profile (``ReviewContextStage``: ``with_counter``).
    counter = token_counter_for(snapshot.execution_profile.counter_version)
    builder = BuildReviewContext(
        source=source,
        parser=InProcessParser(),
        counter=counter,
        retriever=retriever,
        rag_blobs=blobs,
    )
    matcher = IgnoreMatcher(parse_review_ignore(None))
    payload = await builder.execute(
        diff_map=diff_map,
        snapshot=snapshot,
        matcher=matcher,
        ignore=apply_ignore(matcher, diff_map),
        token_budget=FAST_CONTEXT_TOKEN_BUDGET,
        rag=RagPlan() if arm == "B" else None,
    )

    # The mapping of ``ExecuteReviewRun`` (execute_review.py:282-313), copied: it is inline there.
    bindings = snapshot.prompt_bindings
    section = has_retrieved_section(bindings.review_system_key, bindings.review_system_version)
    context = ReviewContext(
        system=system,
        rules=(),
        agents_md=None,
        conventions=RepoConventions((), ()),
        pr_meta=meta,
        changed_files=changed,
        omitted_files=(),
        policy_snapshot=snapshot,
        context_blocks=tuple(
            snapshot_block(block) for block in payload.blocks if block.level in _EXCERPT_LEVELS
        ),
        retrieved=tuple(snapshot_retrieved(block) for block in payload.retrieved)
        if section
        else (),
        retrieved_section=section,
        excluded_files=payload.ignored_files,
    )
    prompt_builder = PromptBuilder()
    manifest = prompt_builder.build_manifest(context, call_id=_CALL_ID, counter=counter)
    verify_provenance_pre_send_guard(manifest, context=context, snapshot=snapshot)
    return ArmInput(payload, context, prompt_builder.build_prompt(context), manifest)
