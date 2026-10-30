"""Offline context of the #146 A/B arms (graph RAG v1 contribution eval).

Builds the review input of one case for arm A (no RAG) or arm B (RAG) by running the
**production** ``BuildReviewContext`` over in-memory sources: no database, no GitHub, no
model. ``build_case_input`` returns it as the ``ReviewCase`` the gateway takes.

Case layout (``case_dir``): ``base/`` the repository at the case revision, ``diff.patch``
a ``git diff`` of the PR, ``index.json`` the frozen code index of ``base/``
(``build_rag_snapshot.py``). The head tree, revision ids and provider files come from
``eval_rag_source``; the graph from ``rag_snapshot``.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from types import TracebackType
from typing import Literal, Self
from uuid import UUID

from app.bootstrap.llm_gateway import ReviewCase
from app.modules.reviews.application.execute_review import snapshot_excerpts
from app.modules.reviews.application.get_run_file_lines import (
    BlobCacheEntry,
    BlobCacheKey,
    BlobCacheStatus,
)
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
    RagPlan,
    ReviewContextPayload,
)
from app.modules.reviews.application.review_context_stage import FAST_CONTEXT_TOKEN_BUDGET
from app.modules.reviews.application.review_ignore import (
    IgnoreMatcher,
    apply_ignore,
    parse_review_ignore,
)
from app.modules.reviews.application.vcs_diff import is_generated_path
from app.modules.reviews.infrastructure.subprocess_source_parser import SubprocessSourceParser
from review.scripts.eval_rag_source import (
    REPOSITORY_ID,
    SnapshotRevisionSource,
    load_snapshot,
    snapshot_diff_map,
)
from review.scripts.rag_snapshot import INDEX_FILE, FrozenGraphRetriever, parse_snapshot

Arm = Literal["A", "B"]

# The pinned review prompt both arms run (Р-6, #82): v5, the only one with a retrieved section.
_PROMPT_KEY = "review.system:v5"

# The manifest of a case is reproducible: no random call id.
_CALL_ID = UUID(int=1460)


@dataclass(frozen=True)
class ArmInput:
    """What one arm sends: the payload of the production build, the prompt input mapped
    from it as ``ExecuteReviewRun`` maps it, the rendered v5 prompt and its manifest, and
    the ``ReviewCase`` that makes the gateway send that prompt."""

    payload: ReviewContextPayload
    context: ReviewContext
    prompt: ReviewPrompt
    manifest: CallManifest
    case: ReviewCase


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


def _policy_snapshot(system: bytes, review_model: str) -> PolicySnapshot:
    """The arm-``none`` snapshot: no repository rules, no AGENTS.md, the pinned v5 prompt.

    ``system`` is the file of the prompt, checked against its pin before it is bound
    (as ``eval_live._prompt_bindings`` does, by the SHA-256 of the bytes).

    ``eval_live._policy_snapshot`` is private and reads policy files from the case tree; the
    same shape is built here through the public builder. ``review_model`` is the model the
    run will call: the snapshot pins it in its execution profile.
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
        review_model,
        review_model,
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
            REPOSITORY_ID,
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


def _frozen_graph(case_dir: Path, base_id: str) -> FrozenGraphRetriever:
    """The graph of ``case_dir/index.json``. A missing file is an error, never an empty
    graph, and so is an index built for another base: the commit ids derive from the name
    of the case directory, so a copied or renamed case would read a stale graph."""
    frozen = parse_snapshot((case_dir / INDEX_FILE).read_bytes())
    if frozen.indexed_commit_sha != base_id:
        raise ValueError(
            f"{case_dir / INDEX_FILE} indexes {frozen.indexed_commit_sha}, not the base "
            f"{base_id} of case {case_dir.name}: rebuild it with build_rag_snapshot.py --write"
        )
    return FrozenGraphRetriever(frozen)


async def build_arm_input(case_dir: Path, arm: Arm, *, review_model: str) -> ArmInput:
    """The input of ``arm`` for the case in ``case_dir``.

    Both arms run the same production build; arm A passes ``rag=None``, arm B a ``RagPlan``
    with the frozen retriever. Raises ``PolicyError`` if the gateway's provenance guard
    would refuse the call, and ``ValueError`` for a change production would count as
    withheld (a lock or generated file): ``ReviewCase`` cannot carry that count.
    """
    if arm not in ("A", "B"):
        raise ValueError(f"unknown arm {arm!r}: expected 'A' or 'B'")
    case = load_snapshot(case_dir)
    head_id = case.head_id
    retriever = _frozen_graph(case_dir, case.base_id)
    changed = parse_unified_diff(case.diff)
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
        # The provenance guard accepts PR-side revisions only against the head id...
        head_sha=head_id,
        # ...and the LEFT side of a removed or renamed export against the base id.
        diff_old_sha=case.base_id,
        commit_messages=(),
        commit_messages_incomplete=False,
    )
    diff_map = snapshot_diff_map(case, meta)

    system_bytes = (
        Path(__file__).resolve().parents[1] / "prompts" / "review.system.v5.md"
    ).read_bytes()
    snapshot = _policy_snapshot(system_bytes, review_model)
    source = SnapshotRevisionSource({case.base_id: case.base, head_id: case.head})
    blobs = CachedBlobSource(
        repository_id=REPOSITORY_ID,
        source=source,
        cache=MemoryBlobCache(),
        uow_factory=MemoryBlobUnitOfWork,
    )
    # The counter the stage pins from the profile (``ReviewContextStage``: ``with_counter``).
    counter = token_counter_for(snapshot.execution_profile.counter_version)
    # As the worker builds it; the per-attempt CPU budget is a live-run limit, not a case's.
    parser = SubprocessSourceParser(attempt_cpu_seconds=None)
    builder = BuildReviewContext(
        source=source,
        parser=parser,
        counter=counter,
        builtin_excluded=is_generated_path,
        retriever=retriever,
        rag_blobs=blobs,
    )
    matcher = IgnoreMatcher(parse_review_ignore(None))
    try:
        payload = await builder.execute(
            diff_map=diff_map,
            snapshot=snapshot,
            matcher=matcher,
            ignore=apply_ignore(matcher, diff_map, builtin_excluded=is_generated_path),
            token_budget=FAST_CONTEXT_TOKEN_BUDGET,
            rag=RagPlan() if arm == "B" else None,
        )
    finally:
        await parser.aclose()
    if payload.ignored_files:
        raise ValueError(
            f"case {case.case_id} changes {payload.ignored_files} file(s) the service withholds "
            "(lock or generated files): its prompt would name them and the case cannot"
        )

    bindings = snapshot.prompt_bindings
    section = has_retrieved_section(bindings.review_system_key, bindings.review_system_version)
    system = system_bytes.decode("utf-8")
    excerpts = snapshot_excerpts(payload, retrieved_section=section)
    context = ReviewContext(
        system=system,
        rules=(),
        agents_md=None,
        conventions=RepoConventions((), ()),
        pr_meta=meta,
        changed_files=changed,
        omitted_files=(),
        policy_snapshot=snapshot,
        context_blocks=excerpts.context_blocks,
        retrieved=excerpts.retrieved,
        retrieved_section=section,
        excluded_files=excerpts.excluded_files,
    )
    prompt_builder = PromptBuilder()
    manifest = prompt_builder.build_manifest(context, call_id=_CALL_ID, counter=counter)
    verify_provenance_pre_send_guard(manifest, context=context, snapshot=snapshot)
    review = ReviewCase(
        diff=case.diff,
        system=system,
        pr_meta=meta,
        policy_snapshot=snapshot,
        context_blocks=excerpts.context_blocks,
        retrieved=excerpts.retrieved,
    )
    return ArmInput(payload, context, prompt_builder.build_prompt(context), manifest, review)


async def build_case_input(case_dir: Path, arm: Arm, *, review_model: str) -> ReviewCase:
    """The ``ReviewCase`` of ``arm``: what ``review_case`` sends through the real gateway."""
    return (await build_arm_input(case_dir, arm, review_model=review_model)).case
