"""Run the policy and context steps of a review run.

The stage runs after the diff of the attempt is stored and before any model call. It
enforces the policy: an invalid or unreadable policy fails the run with its code, a
change whose every file is ignored is skipped, and ignored files are withheld from the
prompt of the review. The assembled context itself is recorded in the run trace;
feeding it to the model belongs to the integration with the LLM pipeline (§12).
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Callable
from typing import Any
from uuid import UUID

from app.modules.reviews.application.diff_map import (
    DiffMap,
    VcsProviderName,
    build_diff_map,
    is_supported_path,
)
from app.modules.reviews.application.get_run_diff import DiffSnapshot
from app.modules.reviews.application.policy_snapshot import PolicySnapshot
from app.modules.reviews.application.prepare_policy_snapshot import PreparePolicySnapshot
from app.modules.reviews.application.process_run import RunVcsInput, RunVcsRepository
from app.modules.reviews.application.review_context import (
    BuildReviewContext,
    ContextStatus,
    ReviewContextPayload,
)
from app.modules.reviews.application.review_ignore import (
    IgnoreMatcher,
    IgnorePolicy,
    apply_ignore,
)
from app.modules.reviews.application.run_failures import RunFailure, RunSkipped
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


def _vcs_file(snapshot: DiffSnapshot) -> VcsFile:
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


def context_summary(diff_map: DiffMap, payload: ReviewContextPayload) -> dict[str, Any]:
    """Counts and source ranges of the context; the text of a block is never stored here."""
    tokens: Counter[str] = Counter()
    for block in payload.blocks:
        tokens[block.level.value] += block.tokens
    return {
        "status": payload.status.value,
        "reason": payload.reason,
        "diff": {
            "digest": diff_map.digest,
            "files": len(diff_map.files),
            "diff_old_sha": diff_map.diff_old_sha,
            "file_list_complete": diff_map.file_list_complete,
        },
        "reviewable_lines": payload.reviewable_lines,
        "ignored_files": payload.ignored_files,
        "levels_present": [level.value for level in payload.levels_present],
        "blocks_by_level": dict(Counter(block.level.value for block in payload.blocks)),
        "tokens_by_level": dict(tokens),
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
        ],
    }


class ReviewContextStage:
    """Policy snapshot, diff map, ignore and context of one run, recorded in its trace."""

    def __init__(
        self,
        *,
        runs: RunVcsRepository,
        vcs: VcsProvider,
        merge_base: MergeBaseProvider,
        prepare_policy: PreparePolicySnapshot,
        context_builder: Callable[[RunVcsInput], BuildReviewContext],
        trace: RunTrace | None = None,
        token_budget: int = FAST_CONTEXT_TOKEN_BUDGET,
    ) -> None:
        self._runs = runs
        self._vcs = vcs
        self._merge_base = merge_base
        self._prepare_policy = prepare_policy
        self._context_builder = context_builder
        self._trace = trace
        self._token_budget = token_budget
        self._denied_paths: frozenset[str] = frozenset()

    def excluded(self, path: str) -> bool:
        """Whether the policy of this attempt withholds the path from every model input."""
        return path in self._denied_paths or not is_supported_path(path)

    async def run(self, run_id: UUID) -> ReviewContextPayload | None:
        """Enforce the policy of the run, then build its context.

        The policy part is mandatory: the snapshot, the diff of the attempt and the
        ignore decisions. If it cannot be established the attempt fails, and a change
        whose every file is ignored is skipped, both before any model call. Only then
        comes the optional enrichment: its failure is logged, the exclusions stay in
        force and the review continues with the diff.
        """
        self._denied_paths = frozenset()
        run = await self._runs.get_run_vcs_input(run_id)
        stored = await self._runs.get_run_snapshots(run_id) if run is not None else None
        if run is None or stored is None:
            raise LookupError(f"run {run_id} has no stored diff")

        request = {"policy_base_sha": run.base_sha}
        async with traced_step(self._trace, run_id, "policy.load", request) as step:
            snapshot = await self._prepare_policy.execute(run_id)
            step.response = policy_summary(snapshot)

        pull_request = await self._vcs.get_pull_request(run.locator)
        if pull_request.head_sha != run.head_sha:
            raise RunFailure("diff_fetch_failed", "pull request head changed after the diff")
        try:
            diff_old_sha: str | None = await self._merge_base.get_merge_base(pull_request)
        except MergeBaseUnavailable:
            # The old side stays unknown; it is never replaced by the policy base.
            _LOGGER.warning("Merge base is unavailable run_id=%s", run_id, exc_info=True)
            diff_old_sha = None
        diff_map = build_diff_map(
            pull_request,
            tuple(_vcs_file(item) for item in stored),
            provider=VcsProviderName.GITHUB,
            repository_id=run.repository_id,
            diff_old_sha=diff_old_sha,
        )
        # The snapshot keeps validated masks; the matcher is rebuilt from them.
        matcher = IgnoreMatcher(IgnorePolicy(snapshot.ignore_patterns, (), snapshot.ignore_digest))
        ignore = apply_ignore(matcher, diff_map)
        self._denied_paths = ignore.denied_paths
        if diff_map.files and all(
            ignore.decision_for(file.file_id) is not None for file in diff_map.files
        ):
            await self._record_all_ignored(run_id, snapshot, diff_map)
            raise RunSkipped("all_changes_ignored")

        request = {"head_sha": run.head_sha, "policy_snapshot_id": str(snapshot.snapshot_id)}
        try:
            async with traced_step(self._trace, run_id, "context.build", request) as step:
                payload = await self._context_builder(run).execute(
                    diff_map=diff_map,
                    snapshot=snapshot,
                    matcher=matcher,
                    ignore=ignore,
                    token_budget=self._token_budget,
                )
                step.response = context_summary(diff_map, payload)
        except Exception:
            _LOGGER.warning("Review context build failed run_id=%s", run_id, exc_info=True)
            return None
        if payload.status is ContextStatus.ALL_CHANGES_IGNORED:
            raise RunSkipped("all_changes_ignored")
        return payload

    async def _record_all_ignored(
        self, run_id: UUID, snapshot: PolicySnapshot, diff_map: DiffMap
    ) -> None:
        request = {"head_sha": diff_map.head_sha, "policy_snapshot_id": str(snapshot.snapshot_id)}
        async with traced_step(self._trace, run_id, "context.build", request) as step:
            step.response = {
                "status": ContextStatus.ALL_CHANGES_IGNORED.value,
                "reason": "all_changes_ignored",
                "diff": {"digest": diff_map.digest, "files": len(diff_map.files)},
                "ignored_files": len(diff_map.files),
                "blocks": [],
            }
