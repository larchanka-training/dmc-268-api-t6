"""Run the policy and context steps of a review run behind a feature flag.

The stage observes: it stores the policy snapshot and records what the context would be,
and it never changes or fails the review itself. Enforcing its outcomes (failing a run on
an invalid policy, skipping a fully ignored change, feeding the model) belongs to the
integration with the LLM pipeline, CONTEXT_AND_VERIFICATION_SPEC §12.
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Callable
from typing import Any
from uuid import UUID

import httpx

from app.modules.reviews.application.diff_map import DiffMap, VcsProviderName, build_diff_map
from app.modules.reviews.application.get_run_diff import DiffSnapshot
from app.modules.reviews.application.policy_snapshot import PolicySnapshot
from app.modules.reviews.application.prepare_policy_snapshot import PreparePolicySnapshot
from app.modules.reviews.application.process_run import RunVcsInput, RunVcsRepository
from app.modules.reviews.application.review_context import (
    BuildReviewContext,
    ReviewContextPayload,
)
from app.modules.reviews.application.review_ignore import (
    IgnoreMatcher,
    IgnorePolicy,
    apply_ignore,
)
from app.modules.reviews.application.run_trace import RunTrace, traced_step
from app.modules.reviews.application.vcs_diff import MergeBaseProvider, VcsFile, VcsProvider

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
            {"reason": item.reason, "path": item.path, "count": item.count}
            for item in payload.omissions
        ],
        "blocks": [
            {
                "level": block.level.value,
                "role": block.source.role.value,
                "path": block.source.path,
                "ranges": [[item.start_line, item.end_line] for item in block.source.ranges],
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

    async def observe(self, run_id: UUID) -> ReviewContextPayload | None:
        """Run the stage for its record only: a failure is logged and never raised."""
        try:
            return await self.execute(run_id)
        except Exception:
            _LOGGER.warning("Review context stage failed run_id=%s", run_id, exc_info=True)
            return None

    async def execute(self, run_id: UUID) -> ReviewContextPayload:
        run = await self._runs.get_run_vcs_input(run_id)
        stored = await self._runs.get_run_snapshots(run_id) if run is not None else None
        if run is None or stored is None:
            raise LookupError(f"run {run_id} has no stored diff")

        request = {"policy_base_sha": run.base_sha}
        async with traced_step(self._trace, run_id, "policy.load", request) as step:
            snapshot = await self._prepare_policy.execute(run_id)
            step.response = policy_summary(snapshot)

        request = {"head_sha": run.head_sha, "policy_snapshot_id": str(snapshot.snapshot_id)}
        async with traced_step(self._trace, run_id, "context.build", request) as step:
            pull_request = await self._vcs.get_pull_request(run.locator)
            if pull_request.head_sha != run.head_sha:
                raise ValueError("pull request head changed after the diff was stored")
            try:
                diff_old_sha: str | None = await self._merge_base.get_merge_base(pull_request)
            except httpx.HTTPError:
                # The old side stays unknown; it is never replaced by the policy base.
                _LOGGER.warning("Merge base read failed run_id=%s", run_id, exc_info=True)
                diff_old_sha = None
            diff_map = build_diff_map(
                pull_request,
                tuple(_vcs_file(item) for item in stored),
                provider=VcsProviderName.GITHUB,
                repository_id=run.repository_id,
                diff_old_sha=diff_old_sha,
            )
            # The snapshot keeps validated masks; the matcher is rebuilt from them.
            policy = IgnorePolicy(snapshot.ignore_patterns, (), snapshot.ignore_digest)
            matcher = IgnoreMatcher(policy)
            payload = await self._context_builder(run).execute(
                diff_map=diff_map,
                snapshot=snapshot,
                matcher=matcher,
                ignore=apply_ignore(matcher, diff_map),
                token_budget=self._token_budget,
            )
            step.response = context_summary(diff_map, payload)
        return payload
