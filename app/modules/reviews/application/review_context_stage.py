"""Run the policy and context steps of a review run.

The stage runs after the diff of the attempt is stored and before any model call. It
enforces the policy: an invalid or unreadable policy fails the run with its code, a
change whose every file is ignored is skipped, and ignored files are withheld from the
prompt of the review. The assembled context itself is recorded in the run trace;
feeding it to the model belongs to the integration with the LLM pipeline (§12).
"""

from __future__ import annotations

import json
import logging
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any
from uuid import UUID

from app.modules.code_index.domain.enums import RagIndexStatus
from app.modules.code_index.domain.rag_index import RagIndex
from app.modules.reviews.application.check_runs import RunProviderCheckpoint
from app.modules.reviews.application.conventions import conventions_cache_key
from app.modules.reviews.application.diff_map import (
    DiffMap,
    VcsProviderName,
    build_diff_map,
    is_supported_path,
)
from app.modules.reviews.application.get_run_diff import DiffSnapshot
from app.modules.reviews.application.policy_errors import PolicyError, PolicyErrorCode
from app.modules.reviews.application.policy_snapshot import PolicySnapshot, PromptBindings
from app.modules.reviews.application.prepare_policy_snapshot import PreparePolicySnapshot
from app.modules.reviews.application.process_run import RunVcsInput, RunVcsRepository
from app.modules.reviews.application.prompt_budget import (
    PolicyBudgetExceededError,
    token_counter_for,
    validate_execution_profile_versions,
    verify_policy_budget,
)
from app.modules.reviews.application.rag_index import (
    OpenReviewIndex,
    RagReadiness,
    has_retrieved_section,
)
from app.modules.reviews.application.review_context import (
    BuildReviewContext,
    ContextBlock,
    ContextStatus,
    RagPlan,
    RagStats,
    RetrievedBlock,
    ReviewContextPayload,
)
from app.modules.reviews.application.review_ignore import (
    IgnoreMatcher,
    IgnorePolicy,
    apply_ignore,
)
from app.modules.reviews.application.review_path import ReviewBinding, snapshot_for_review
from app.modules.reviews.application.run_failures import (
    RequiredSourceUnavailable,
    RunCancelled,
    RunFailure,
    RunSkipped,
)
from app.modules.reviews.application.run_trace import RunTrace, traced_step
from app.modules.reviews.application.vcs_diff import (
    MergeBaseProvider,
    MergeBaseUnavailable,
    VcsFile,
    VcsProvider,
)

_LOGGER = logging.getLogger(__name__)
FAST_CONTEXT_TOKEN_BUDGET = 60_000
_MAX_TRACED_BLOCKS = 200
# ``context.rag`` of a build that had a RAG plan and failed: the slot did not complete.
_RAG_BUILD_FAILED = "context_build_failed"
# CONTRACT: the provider the stage builds the diff map with is the provider the review path
# is chosen for (TL-13). The stage's input carries no provider yet; #220 replaces both uses
# with the provider of the run.
_STAGE_PROVIDER = VcsProviderName.GITHUB


class EstimatedTokenCounter:
    """Four characters per token, rounded up: an estimate until the model counter is wired."""

    def count(self, text: str) -> int:
        return -(-len(text) // 4)


def _provider_patch(patch: str | None) -> str | None:
    """The hunks as the provider sent them.

    A stored snapshot keeps the patch for the UI under a ``diff --git`` header; the diff
    map reads the hunks only. A stored patch without a hunk has no lines to map.
    """
    if patch is None or not patch.startswith("diff --git "):
        return patch
    hunk_start = patch.find("\n@@ ")
    return None if hunk_start < 0 else patch[hunk_start + 1 :]


# Stored omissions that are exclusions of the service, not incompleteness of the diff.
_SERVICE_EXCLUSIONS = frozenset({"binary", "generated"})


def _service_excluded_names(stored: list[DiffSnapshot]) -> frozenset[str]:
    """Names of stored files that the service excluded, as the deny-set holds them.

    The source of a copy stays its own file: only the copy is withheld.
    """
    names: set[str] = set()
    for item in stored:
        if item.omission_reason not in _SERVICE_EXCLUSIONS:
            continue
        names.add(item.filename)
        if item.previous_filename is not None and item.status != "copied":
            names.add(item.previous_filename)
    return frozenset(name for name in names if is_supported_path(name))


def vcs_file_from_snapshot(snapshot: DiffSnapshot) -> VcsFile:
    return VcsFile(
        filename=snapshot.filename,
        status=snapshot.status,
        blob_sha=snapshot.blob_sha,
        previous_filename=snapshot.previous_filename,
        additions=snapshot.additions,
        deletions=snapshot.deletions,
        changes=snapshot.changes,
        patch=_provider_patch(snapshot.patch),
    )


def policy_summary(snapshot: PolicySnapshot) -> dict[str, Any]:
    """What was loaded, without any document content."""
    return {
        "policy_snapshot_id": str(snapshot.snapshot_id),
        "policy_base_sha": snapshot.policy_base_sha,
        "policy_digest": snapshot.policy_digest,
        "documents": [
            {
                "path": document.path,
                "presence": document.presence.value,
                "bytes": document.byte_count,
                "lines": document.line_count,
            }
            for document in (snapshot.rules, snapshot.agents)
        ],
        "ignore_patterns": len(snapshot.ignore_patterns),
        "ignore_digest": snapshot.ignore_digest,
    }


def context_summary(
    diff_map: DiffMap, payload: ReviewContextPayload, *, pr_base_sha: str | None = None
) -> dict[str, Any]:
    """Counts and source ranges of the context; the text of a block is never stored here.

    ``pr_base_sha`` is the live base of a PR that left its pinned base; it is recorded
    here only, never in the diff map or its digest.
    """
    # Retrieved blocks of the RAG slot count with the blocks of the diff files.
    counted: tuple[ContextBlock | RetrievedBlock, ...] = (*payload.blocks, *payload.retrieved)
    tokens: Counter[str] = Counter()
    tokens_by_role: Counter[str] = Counter()
    for block in counted:
        tokens[block.level.value] += block.tokens
        tokens_by_role[block.source.role.value] += block.tokens
    diff: dict[str, Any] = {
        "digest": diff_map.digest,
        "files": len(diff_map.files),
        "diff_old_sha": diff_map.diff_old_sha,
        "file_list_complete": diff_map.file_list_complete,
    }
    if pr_base_sha is not None:
        diff["pr_base_sha"] = pr_base_sha
    return {
        "status": payload.status.value,
        "reason": payload.reason,
        "diff": diff,
        "reviewable_lines": payload.reviewable_lines,
        "ignored_files": payload.ignored_files,
        "levels_present": [level.value for level in payload.levels_present],
        "blocks_by_level": dict(Counter(block.level.value for block in counted)),
        "tokens_by_level": dict(tokens),
        "tokens_by_role": dict(tokens_by_role),
        "tokens_used": payload.tokens_used,
        "imports": dict(Counter(ref.resolution.value for ref in payload.imports)),
        "omissions": [
            {
                "reason": item.reason,
                "path": item.path,
                "count": item.count,
                "diagnostic_path": item.diagnostic_path,
            }
            for item in payload.omissions
        ],
        "blocks": [
            {
                "level": block.level.value,
                "role": block.source.role.value,
                "path": block.source.path,
                "side": None if block.source.side is None else block.source.side.value,
                "ranges": [[item.start_line, item.end_line] for item in block.source.ranges],
                "related": [
                    {
                        "side": None if ref.side is None else ref.side.value,
                        "path": ref.path,
                        "revision": ref.revision,
                        "ranges": [[item.start_line, item.end_line] for item in ref.ranges],
                    }
                    for ref in block.related_sources
                ],
                "representation": block.representation.value,
                "tokens": block.tokens,
                "truncated": block.truncated,
                "reason": block.source.reason,
            }
            for block in payload.blocks[:_MAX_TRACED_BLOCKS]
        ]
        # Retrieved blocks, bounded by their slot, each with the revision of its side.
        + [_traced_retrieved(block) for block in payload.retrieved],
    }


def _traced_retrieved(block: RetrievedBlock) -> dict[str, Any]:
    source = block.source
    return {
        "level": block.level.value,
        "role": source.role.value,
        "path": source.path,
        "side": None if source.side is None else source.side.value,
        "revision": source.revision,
        "ranges": [[item.start_line, item.end_line] for item in source.ranges],
        "representation": block.representation.value,
        "tokens": block.tokens,
        "reason": source.reason,
    }


@dataclass(frozen=True)
class _RagStep:
    """What the RAG gate found, kept until ``context.build`` is recorded: the gate runs
    before the build, its step follows the build's in the trace.

    With a ``plan`` the build fills the RAG slot and its stats give the response;
    ``response`` is then what a caught build failure records: the gate let RAG in, the
    slot did not complete, nothing is counted.
    """

    request: dict[str, Any]
    response: dict[str, Any]
    started_at: datetime
    duration_ms: int
    plan: RagPlan | None = None


def _not_ready(index: RagIndex) -> dict[str, Any] | None:
    """The ``not_ready`` response of an index that cannot serve the review; None when it
    can or RAG is off. ``disabled`` is never a reason (AC6)."""
    reason = index.not_ready_reason
    if reason is None:
        return None
    response: dict[str, Any] = {"outcome": "not_ready", "reason": reason.value}
    if reason is RagIndexStatus.FAILED:
        response["error_code"] = index.last_error_code
    return response


def _rag_readiness(
    readiness: RagReadiness, prompt: PromptBindings
) -> tuple[dict[str, Any], dict[str, Any], RagPlan | None] | None:
    """Request, response and plan of ``context.rag``; None when RAG is not in effect.

    Readiness is read only through ``RagIndex`` (AC6). A usable index gets a plan only
    when the pinned review prompt shows retrieved blocks (M6).
    """
    index = readiness.index
    if not readiness.enabled or index is None:
        return None
    response: dict[str, Any] | None
    plan: RagPlan | None = None
    if not index.usable:
        response = _not_ready(index)
        if response is None:
            return None
    elif index.indexed_commit_sha is not None and has_retrieved_section(
        prompt.review_system_key, prompt.review_system_version
    ):
        plan = RagPlan()
        response = {"outcome": "used", "reason": _RAG_BUILD_FAILED}
    else:
        response = {"outcome": "not_ready", "reason": "prompt_version"}
    request = {
        "index_status": index.status.value,
        "indexed_commit_sha": index.indexed_commit_sha,
        "default_head_sha": readiness.default_head_sha,
        "stale": index.status is RagIndexStatus.STALE,
    }
    return request, response, plan


def _rag_used(stats: RagStats) -> dict[str, Any]:
    """The response of a build that had a RAG plan: counters only, no path.

    A failed slot gives its error. An index the graph read found no longer usable gives
    its fresh status, not ``used`` with nothing found; an index turned off after the gate
    reads as empty there. A return before the slot keeps its reason and zero counters (M8).
    """
    if stats.error_type is not None:
        return {"error": {"type": stats.error_type, "message": stats.error_message}}
    if stats.index is not None and not stats.index.usable:
        return _not_ready(stats.index) or {
            "outcome": "not_ready",
            "reason": RagIndexStatus.EMPTY.value,
        }
    response: dict[str, Any] = {"outcome": "used"}
    if stats.reason is not None:
        response["reason"] = stats.reason
    response["candidates"] = {
        "found": stats.found,
        "confirmed": stats.confirmed,
        "dropped": dict(stats.dropped),
    }
    response["tokens"] = stats.tokens
    return response


def _verify_policy_budget(snapshot: PolicySnapshot) -> None:
    """Fail closed when the rendered policy documents exceed the pinned profile's budget."""
    profile = snapshot.execution_profile
    try:
        verify_policy_budget(
            snapshot,
            token_counter_for(profile.counter_version),
            max_policy_tokens=profile.max_policy_tokens,
        )
    except PolicyBudgetExceededError as exc:
        raise PolicyError(
            PolicyErrorCode.PROMPT_BUDGET_EXCEEDED,
            "The rendered policy documents exceed the token budget of the pinned profile",
            limit=exc.limit,
            actual=exc.actual,
        ) from exc


class ReviewContextStage:
    """Policy snapshot, diff map, ignore and context of one run, recorded in its trace."""

    def __init__(
        self,
        *,
        runs: RunVcsRepository,
        checkpoint: RunProviderCheckpoint,
        vcs: VcsProvider,
        merge_base: MergeBaseProvider,
        prepare_policy: PreparePolicySnapshot,
        context_builder: Callable[[RunVcsInput], BuildReviewContext],
        trace: RunTrace | None = None,
        token_budget: int = FAST_CONTEXT_TOKEN_BUDGET,
        builtin_excluded: Callable[[str], bool] = lambda path: False,
        rag: OpenReviewIndex | None = None,
    ) -> None:
        self._runs = runs
        self._checkpoint = checkpoint
        self._vcs = vcs
        self._merge_base = merge_base
        self._prepare_policy = prepare_policy
        self._context_builder = context_builder
        self._trace = trace
        self._token_budget = token_budget
        # Built-in exclusions of the service (generated files): part of the effective
        # exclusion next to the masks of the repository.
        self._builtin_excluded = builtin_excluded
        # None: no index access and no ``context.rag`` step (eval, replay).
        self._rag = rag
        self._denied_paths: frozenset[str] = frozenset()
        self._snapshot: PolicySnapshot | None = None
        # The snapshot the review is given; None on the legacy path.
        self._snapshot_for_llm: PolicySnapshot | None = None
        # The context built for this attempt; None before the build or after a failed one.
        self._payload: ReviewContextPayload | None = None
        self._matcher: IgnoreMatcher | None = None
        self._diff_old_sha: str | None = None
        self._head_sha: str | None = None
        # The live PR base of the attempt when it differs from the pinned one, else None.
        self._pr_base_sha: str | None = None

    @property
    def snapshot_for_llm(self) -> PolicySnapshot | None:
        """The attempt's snapshot when the run takes the snapshot path, else ``None``."""
        return self._snapshot_for_llm

    @property
    def payload(self) -> ReviewContextPayload | None:
        """The context the attempt's build produced; ``None`` when it failed or did not run."""
        return self._payload

    @property
    def diff_old_sha(self) -> str | None:
        """The old-side diff revision (merge base) established for the current attempt."""
        return self._diff_old_sha

    @property
    def head_sha(self) -> str | None:
        """The head revision of the attempt's diff map: its stored diff and excerpts."""
        return self._head_sha

    @property
    def policy_key(self) -> str | None:
        """The key of the conventions cache entry this attempt may reuse.

        On the legacy path it is the attempt's ignore key: the masks and built-in
        exclusions are in ``ignore_digest``, and a rename or copy also withholds names no
        mask matches, so an entry made while such a file was still allowed is not reused
        once it is tainted. On the snapshot path it is the ``repo-conventions-v2`` digest
        over that ignore key, the policy documents, the service defaults, the pinned
        conventions prompt and the counter (R5, TL-7), so no entry of a legacy run is reused.
        """
        ignore_key = self._ignore_key()
        if self._snapshot_for_llm is not None:
            return conventions_cache_key(self._snapshot_for_llm, ignore_key)
        return ignore_key

    def _ignore_key(self) -> str | None:
        if self._snapshot is None or self._matcher is None:
            return None
        tainted = sorted(
            path
            for path in self._denied_paths
            if is_supported_path(path)
            and not self._builtin_excluded(path)
            and self._matcher.match(path) is None
        )
        if not tainted:
            return self._snapshot.ignore_digest
        payload = json.dumps([self._snapshot.ignore_digest, tainted], separators=(",", ":"))
        return sha256(payload.encode("utf-8")).hexdigest()

    def excluded(self, path: str) -> bool:
        """Whether the policy of this attempt withholds the path from every model input.

        The effective exclusion: a path outside the path contract, any name of an
        ignored changed file, a built-in exclusion of the service, and every repository
        path that matches a mask of the policy.
        """
        if not is_supported_path(path) or path in self._denied_paths:
            return True
        if self._builtin_excluded(path):
            return True
        return self._matcher is not None and self._matcher.match(path) is not None

    def excludes_change(self, path: str, previous_path: str | None) -> bool:
        """Whether a changed file is withheld; a rename or copy is checked by both names."""
        return any(self.excluded(item) for item in (path, previous_path) if item)

    async def begin(self, run_id: UUID) -> None:
        """Establish the policy of the attempt before its diff is stored or counted."""
        await self._checkpoint()
        run = await self._runs.get_run_vcs_input(run_id)
        if run is None:
            raise RunCancelled("installation_unavailable")
        request = {"policy_base_sha": run.base_sha}
        async with traced_step(self._trace, run_id, "policy.load", request) as step:
            self._snapshot = await self._prepare_policy.execute(run_id)
            validate_execution_profile_versions(self._snapshot.execution_profile)
            # The pinned review prompt picks the path. A snapshot-path run that cannot be
            # served fails here, before the conventions call.
            # CONTRACT: the snapshot's prompt bindings are the run's pins (checked on every
            # read of a stored snapshot), so they name the review prompt the run pinned.
            bindings = self._snapshot.prompt_bindings
            snapshot_for_llm = snapshot_for_review(
                ReviewBinding(
                    bindings.review_system_id,
                    bindings.review_system_key,
                    bindings.review_system_version,
                    _STAGE_PROVIDER.value,
                ),
                self._snapshot,
            )
            if snapshot_for_llm is not None:
                # R7 (TL-8): policy documents over their budget end the attempt here,
                # before the conventions and the review model; legacy runs are not checked.
                _verify_policy_budget(snapshot_for_llm)
            self._snapshot_for_llm = snapshot_for_llm
            step.response = policy_summary(self._snapshot)
        # The snapshot keeps validated masks; the matcher is rebuilt from them.
        self._matcher = IgnoreMatcher(
            IgnorePolicy(self._snapshot.ignore_patterns, (), self._snapshot.ignore_digest)
        )

    async def run(self, run_id: UUID) -> ReviewContextPayload | None:
        """Enforce the policy of the run, then build its context.

        The policy part is mandatory: the snapshot, the diff of the attempt and the
        ignore decisions. If it cannot be established the attempt fails, and a change
        whose every file is ignored is skipped, both before any model call. Only then
        comes the optional enrichment: its failure is logged, the exclusions stay in
        force and the review continues with the diff.
        """
        self._denied_paths = frozenset()
        self._diff_old_sha = None
        self._head_sha = None
        self._pr_base_sha = None
        self._payload = None
        if self._snapshot is None or self._matcher is None:
            await self.begin(run_id)
        snapshot, matcher = self._snapshot, self._matcher
        assert snapshot is not None and matcher is not None
        await self._checkpoint()
        run = await self._runs.get_run_vcs_input(run_id)
        stored = await self._runs.get_run_snapshots(run_id) if run is not None else None
        if run is None:
            raise RunCancelled("installation_unavailable")
        if stored is None:
            raise LookupError(f"run {run_id} has no stored diff")

        pull_request = await self._vcs.get_pull_request(run.locator, checkpoint=self._checkpoint)
        if pull_request.head_sha != run.head_sha:
            raise RunFailure("diff_fetch_failed", "pull request head changed after the diff")
        if pull_request.base_sha != run.base_sha:
            # A retarget after the diff was stored: the stored patches, the merge base and
            # the policy all stay on the pinned base; the live one is only recorded.
            _LOGGER.warning(
                "Pull request base differs from the pinned run base "
                "run_id=%s pinned_base_sha=%s pr_base_sha=%s",
                run_id,
                run.base_sha,
                pull_request.base_sha,
            )
            self._pr_base_sha = pull_request.base_sha
            pull_request = replace(pull_request, base_sha=run.base_sha)
        if not stored and pull_request.meta.files_changed == 0:
            # The file list and the counter of the provider agree: nothing was changed.
            await self._record_nothing_to_review(
                run_id, snapshot, run.head_sha, ContextStatus.NO_CHANGES, digest=None, files=0
            )
            raise RunSkipped("no_changes")
        try:
            diff_old_sha: str | None = await self._merge_base.get_merge_base(
                pull_request, checkpoint=self._checkpoint
            )
        except MergeBaseUnavailable:
            # The old side stays unknown; it is never replaced by the policy base.
            _LOGGER.warning("Merge base is unavailable run_id=%s", run_id, exc_info=True)
            diff_old_sha = None
        try:
            provider_name = VcsProviderName(run.locator.provider)
        except ValueError:
            raise RunFailure("diff_fetch_failed", "Unsupported review source provider") from None
        diff_map = build_diff_map(
            pull_request,
            tuple(vcs_file_from_snapshot(item) for item in stored),
            provider=provider_name,
            repository_id=run.repository_id,
            diff_old_sha=diff_old_sha,
        )
        self._diff_old_sha = diff_map.diff_old_sha
        self._head_sha = diff_map.head_sha
        ignore = apply_ignore(matcher, diff_map, builtin_excluded=self._builtin_excluded)
        # A file the service stored as binary or generated is an exclusion for every
        # consumer, also when an allowed file imports it: its names join the deny-set.
        ignore = replace(ignore, denied_paths=ignore.denied_paths | _service_excluded_names(stored))
        self._denied_paths = ignore.denied_paths
        # The effective exclusion: a mask of the repository, a built-in exclusion of any
        # name of the file, or a binary file. Decided here, so a skip never depends on the
        # optional build. An incomplete file list does not prove that all is excluded.
        service_excluded = {
            item.filename for item in stored if item.omission_reason in _SERVICE_EXCLUSIONS
        }
        if (
            diff_map.files
            and diff_map.file_list_complete
            and all(
                ignore.decision_for(file.file_id) is not None
                or (file.provider_new_path or file.provider_old_path) in service_excluded
                or (
                    file.omission_reason is None
                    and (file.new_path or file.old_path) in ignore.denied_paths
                )
                for file in diff_map.files
            )
        ):
            await self._record_nothing_to_review(
                run_id,
                snapshot,
                diff_map.head_sha,
                ContextStatus.ALL_CHANGES_IGNORED,
                digest=diff_map.digest,
                files=len(diff_map.files),
            )
            raise RunSkipped("all_changes_ignored")

        rag = await self._open_rag(run_id, run.repository_id, snapshot.prompt_bindings)
        request = {"head_sha": run.head_sha, "policy_snapshot_id": str(snapshot.snapshot_id)}
        try:
            async with traced_step(self._trace, run_id, "context.build", request) as step:
                builder = self._context_builder(run)
                if self._snapshot_for_llm is not None:
                    # R19 (DD7): the snapshot path counts with the counter its profile pins,
                    # as the policy budget and the gateway do; a legacy run keeps the
                    # composition's counter (four characters per token), so its numbers stay.
                    profile = self._snapshot_for_llm.execution_profile
                    builder = builder.with_counter(token_counter_for(profile.counter_version))
                payload = await builder.execute(
                    diff_map=diff_map,
                    snapshot=snapshot,
                    matcher=matcher,
                    ignore=ignore,
                    token_budget=self._token_budget,
                    rag=None if rag is None else rag.plan,
                )
                step.response = context_summary(diff_map, payload, pr_base_sha=self._pr_base_sha)
        except (PolicyError, RunCancelled, RequiredSourceUnavailable):
            # The policy is also evaluated while the context is built (paths behind an
            # import). Its failure is never an optional fallback: it stops the run.
            raise
        except Exception as exc:
            if isinstance(exc, RunFailure) and exc.error_code == "deadline_exceeded":
                raise
            _LOGGER.warning("Review context build failed run_id=%s", run_id, exc_info=True)
            await self._record_rag(run_id, rag)
            return None
        await self._record_rag(run_id, rag, payload.rag)
        if payload.status is ContextStatus.ALL_CHANGES_IGNORED:
            raise RunSkipped("all_changes_ignored")
        self._payload = payload
        return payload

    async def _record_nothing_to_review(
        self,
        run_id: UUID,
        snapshot: PolicySnapshot,
        head_sha: str,
        status: ContextStatus,
        *,
        digest: str | None,
        files: int,
    ) -> None:
        diff: dict[str, Any] = {"digest": digest, "files": files}
        if self._pr_base_sha is not None:
            diff["pr_base_sha"] = self._pr_base_sha
        request = {"head_sha": head_sha, "policy_snapshot_id": str(snapshot.snapshot_id)}
        async with traced_step(self._trace, run_id, "context.build", request) as step:
            step.response = {
                "status": status.value,
                "reason": status.value,
                "diff": diff,
                "ignored_files": files,
                "blocks": [],
            }

    async def _open_rag(
        self, run_id: UUID, repository_id: UUID, prompt: PromptBindings
    ) -> _RagStep | None:
        """Open the RAG index of the repository; a build it requests is never waited for.

        Optional like the build: a policy error, a cancel and the deadline stop the
        attempt, any other failure is kept as ``{error}`` and the review goes on without RAG.
        """
        if self._rag is None:
            return None
        started_at = datetime.now(UTC)
        started = time.monotonic()
        request: dict[str, Any]
        response: dict[str, Any]
        plan: RagPlan | None = None
        try:
            readiness = await self._rag.execute(repository_id)
        except (PolicyError, RunCancelled):
            raise
        except Exception as exc:
            if isinstance(exc, RunFailure) and exc.error_code == "deadline_exceeded":
                raise
            _LOGGER.warning("RAG readiness gate failed run_id=%s", run_id, exc_info=True)
            request = dict.fromkeys(
                ("index_status", "indexed_commit_sha", "default_head_sha", "stale")
            )
            response = {"error": {"type": type(exc).__name__, "message": str(exc)[:1000]}}
        else:
            found = _rag_readiness(readiness, prompt)
            if found is None:
                return None
            request, response, plan = found
        duration_ms = int((time.monotonic() - started) * 1000)
        return _RagStep(request, response, started_at, duration_ms, plan)

    async def _record_rag(
        self, run_id: UUID, step: _RagStep | None, stats: RagStats | None = None
    ) -> None:
        """Record ``context.rag`` once per attempt, right after the stage's ``context.build``,
        also when the build failed and was caught."""
        if step is None or self._trace is None:
            return
        response = step.response
        if step.plan is not None and stats is not None:
            response = _rag_used(stats)
        await self._trace.record(
            run_id, "context.rag", step.request, response, step.started_at, step.duration_ms
        )
