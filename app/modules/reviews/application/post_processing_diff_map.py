"""Rebuild DiffMap/v1 from durable run snapshots for publication bucketing (#171)."""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from app.modules.reviews.application.diff_map import (
    DiffMap,
    VcsProviderName,
    build_diff_map,
    vcs_provider_name,
)
from app.modules.reviews.application.get_run_diff import DiffSnapshot
from app.modules.reviews.application.prompt_builder import PullRequestMeta
from app.modules.reviews.application.review_context_stage import vcs_file_from_snapshot
from app.modules.reviews.application.vcs_diff import PullRequestLocator, VcsPullRequest


def diff_map_from_snapshots(
    *,
    repository_id: UUID,
    code_change_number: int,
    head_sha: str,
    base_sha: str,
    diff_old_sha: str | None,
    snapshots: Sequence[DiffSnapshot],
    provider: VcsProviderName | str = VcsProviderName.GITHUB,
) -> DiffMap:
    """Build the same DiffMap shape as context.build, without live VCS calls."""
    vcs_provider = (
        provider if isinstance(provider, VcsProviderName) else vcs_provider_name(provider)
    )
    pull_request = VcsPullRequest(
        locator=PullRequestLocator(
            installation_external_id=0,
            repository_full_name="",
            number=code_change_number,
        ),
        head_sha=head_sha,
        base_sha=base_sha,
        meta=PullRequestMeta(
            title="",
            description=None,
            author="",
            source_branch="",
            target_branch="",
            labels=(),
            files_changed=len(snapshots),
            lines_added=sum(item.additions for item in snapshots),
            lines_removed=sum(item.deletions for item in snapshots),
            is_draft=False,
            is_fork=False,
        ),
    )
    return build_diff_map(
        pull_request,
        tuple(vcs_file_from_snapshot(item) for item in snapshots),
        provider=vcs_provider,
        repository_id=repository_id,
        diff_old_sha=diff_old_sha,
    )
