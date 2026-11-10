"""Send-scope and publication finalization of durable run coverage (#213, #392)."""

from __future__ import annotations

from dataclasses import replace

from app.modules.reviews.application.coverage_model import RunCoverage
from app.modules.reviews.application.coverage_schema import CoverageItem, CoverageStatus


def finalize_run_coverage(
    coverage: RunCoverage,
    *,
    incomplete_paths: tuple[str, ...] = (),
    unknown_path_count: int = 0,
) -> RunCoverage:
    """Apply trusted send-scope and technical-drop evidence before result rendering."""
    reasons = list(coverage.reasons)
    counts = coverage.reason_counts
    reviewed = coverage.reviewed_files
    membership = coverage.durable_reviewable_paths
    if incomplete_paths and membership is None and reviewed:
        # A count-only caller cannot establish which complete patch was sent.
        # Withhold all unproven reviewed allocations without naming any file.
        unproven = reviewed + int("classification_unavailable" not in counts)
        reasons.extend(CoverageItem(None, "classification_unavailable") for _ in range(unproven))
        counts["classification_unavailable"] = (
            counts.get("classification_unavailable", 0) + unproven
        )
        reviewed = 0
    affected = sorted(set(incomplete_paths) & (membership or frozenset()))[:reviewed]
    if affected:
        reasons.extend(CoverageItem(path, "review_input_incomplete") for path in affected)
        counts["review_input_incomplete"] = counts.get("review_input_incomplete", 0) + len(affected)
    if unknown_path_count:
        # Raw candidate names may refer to ignored/private paths. A technical drop
        # is a global diagnostic, never an invented per-file classification.
        reasons.extend(CoverageItem(None, "unknown_path") for _ in range(unknown_path_count))
        counts["unknown_path"] = counts.get("unknown_path", 0) + unknown_path_count
    if not affected and not unknown_path_count and reviewed == coverage.reviewed_files:
        return coverage
    return replace(
        coverage,
        status=CoverageStatus.PARTIAL,
        reviewed_files=reviewed - len(affected),
        durable_reviewable_paths=(None if membership is None else membership.difference(affected)),
        reasons=tuple(reasons),
        stored_reason_counts=tuple(sorted(counts.items())),
    )


def coverage_published_for_summary_only(coverage: RunCoverage) -> RunCoverage:
    """A summary-only stop publishes ``partial``, including a too_large-only inventory.

    ``too_large`` is a technical reason, so a file list of only those rows classifies
    as ``none``. AC4 still publishes ``partial`` for every ``SummaryOnlyReview`` stop.
    """
    if coverage.status is CoverageStatus.PARTIAL or not coverage.reasons:
        return coverage
    return replace(coverage, status=CoverageStatus.PARTIAL)
