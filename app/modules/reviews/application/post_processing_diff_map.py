"""Rebuild DiffMap/v1 from durable run snapshots for publication bucketing (#171)."""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from app.modules.reviews.application.diff_map import (
    DiffMap,
    VcsProviderName,
    build_diff_map_from_facts,
    vcs_provider_name,
)
from app.modules.reviews.application.get_run_diff import DiffSnapshot
from app.modules.reviews.application.run_coverage import snapshot_vcs_file


def diff_map_from_snapshots(
    *,
    repository_id: UUID,
    code_change_number: int,
    head_sha: str,
    base_sha: str,
    diff_old_sha: str | None,
    snapshots: Sequence[DiffSnapshot],
    provider: VcsProviderName | str = VcsProviderName.GITHUB,
    file_list_complete: bool = True,
) -> DiffMap:
    """Build the same DiffMap shape as context.build, without live VCS calls."""
    vcs_provider = (
        provider if isinstance(provider, VcsProviderName) else vcs_provider_name(provider)
    )
    return build_diff_map_from_facts(
        files=tuple(snapshot_vcs_file(item) for item in snapshots),
        provider=vcs_provider,
        repository_id=repository_id,
        code_change_number=code_change_number,
        policy_base_sha=base_sha,
        head_sha=head_sha,
        diff_old_sha=diff_old_sha,
        file_list_complete=file_list_complete,
    )
