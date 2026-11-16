"""Internal durable coverage facade (#213, #392)."""

from __future__ import annotations

from uuid import UUID

from app.modules.reviews.application.coverage_finalization import (
    coverage_published_for_summary_only,
    finalize_run_coverage,
)
from app.modules.reviews.application.coverage_model import RunCoverage
from app.modules.reviews.application.coverage_projection import (
    project_run_coverage_with_digest as _project_run_coverage_with_digest,
)
from app.modules.reviews.application.coverage_projection import (
    snapshot_coverage_digest,
    snapshot_vcs_file,
)
from app.modules.reviews.application.coverage_restoration import (
    CoverageContextPins,
    read_context_coverage_pins,
)
from app.modules.reviews.application.coverage_restoration import (
    restore_context_coverage_with_digest as _restore_context_coverage_with_digest,
)
from app.modules.reviews.application.coverage_restoration import (
    restore_run_coverage_with_digest as _restore_run_coverage_with_digest,
)
from app.modules.reviews.application.coverage_restoration import (
    unavailable_run_coverage_with_digest as _unavailable_run_coverage_with_digest,
)
from app.modules.reviews.application.coverage_schema import (
    COVERAGE_EXCLUSION_CODES,
    COVERAGE_REASON_CODES,
    CoverageItem,
    CoverageStatus,
    read_coverage_counts,
    read_coverage_items,
)
from app.modules.reviews.application.diff_map import DiffMap
from app.modules.reviews.application.get_run_diff import DiffSnapshot
from app.modules.reviews.application.review_ignore import IgnoreResult

__all__ = [
    "COVERAGE_EXCLUSION_CODES",
    "COVERAGE_REASON_CODES",
    "CoverageContextPins",
    "CoverageItem",
    "CoverageStatus",
    "RunCoverage",
    "coverage_published_for_summary_only",
    "finalize_run_coverage",
    "project_run_coverage",
    "read_context_coverage_pins",
    "read_coverage_counts",
    "read_coverage_items",
    "restore_context_coverage",
    "restore_run_coverage",
    "snapshot_coverage_digest",
    "snapshot_vcs_file",
    "unavailable_run_coverage",
]


# These four facade entry points are the sole public callers computing snapshot_coverage_digest;
# owner modules accept a required snapshot_digest so production calls and monkeypatch seams
# (e.g. monkeypatch.setattr(run_coverage, "snapshot_coverage_digest", ...)) share one path.
def project_run_coverage(
    snapshots: list[DiffSnapshot],
    diff_map: DiffMap,
    *,
    ignore: IgnoreResult | None = None,
    snapshot_path: bool = False,
) -> RunCoverage:
    """Classify every stored file of the attempt under its effective policy."""
    return _project_run_coverage_with_digest(
        snapshots,
        diff_map,
        snapshot_digest=snapshot_coverage_digest(snapshots),
        ignore=ignore,
        snapshot_path=snapshot_path,
    )


def restore_context_coverage(
    request: object,
    response: object,
    *,
    snapshots: list[DiffSnapshot],
    diff_map: DiffMap,
    run_id: UUID | None = None,
) -> RunCoverage:
    """Restore effective policy only for matching durable facts; unknown names stay private."""
    return _restore_context_coverage_with_digest(
        request,
        response,
        snapshots=snapshots,
        diff_map=diff_map,
        snapshot_digest=snapshot_coverage_digest(snapshots),
        run_id=run_id,
    )


def restore_run_coverage(
    value: object,
    *,
    snapshots: list[DiffSnapshot],
    diff_map: DiffMap,
    run_id: UUID | None = None,
) -> RunCoverage | None:
    """Read saved policy-effective coverage only for the exact durable inputs."""
    return _restore_run_coverage_with_digest(
        value,
        snapshots=snapshots,
        diff_map=diff_map,
        snapshot_digest=snapshot_coverage_digest(snapshots),
        run_id=run_id,
    )


def unavailable_run_coverage(
    snapshots: list[DiffSnapshot], head_sha: str, *, metadata_only: bool = False
) -> RunCoverage:
    """Legacy or mismatched context cannot prove inventory or effective-policy coverage."""
    return _unavailable_run_coverage_with_digest(
        snapshots,
        head_sha,
        snapshot_digest=snapshot_coverage_digest(snapshots),
        metadata_only=metadata_only,
    )
