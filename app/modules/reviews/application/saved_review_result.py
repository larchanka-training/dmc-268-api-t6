"""A shared internal projection of the finalized postprocess result."""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass, replace
from uuid import UUID

from pydantic import ValidationError

from app.modules.reviews.application.coverage_schema import (
    read_reason_variants,
    valid_coverage_status,
)
from app.modules.reviews.application.findings_post_processor import summary_from_result
from app.modules.reviews.application.get_run_diff import DiffSnapshot
from app.modules.reviews.application.review_output import ReviewSummary
from app.modules.reviews.application.run_coverage import (
    COVERAGE_EXCLUSION_CODES,
    COVERAGE_REASON_CODES,
    CoverageStatus,
    RunCoverage,
    read_coverage_counts,
    read_coverage_items,
    unavailable_run_coverage,
)
from app.modules.reviews.application.verdict import Verdict, coverage_verdict, severity_counts

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class SavedReviewResult:
    verdict: Verdict | None
    coverage: RunCoverage
    summary: dict[str, str]


def read_saved_review_result(
    value: object,
    *,
    head_sha: str,
    severities: Iterable[str],
    run_id: UUID | None = None,
) -> SavedReviewResult | None:
    result = _read_saved_review_result(value, head_sha=head_sha, severities=severities)
    if result is None and isinstance(value, dict) and "coverage" in value:
        _LOGGER.warning("Invalid saved review result run_id=%s", run_id)
    return result


def _read_saved_review_result(
    value: object,
    *,
    head_sha: str,
    severities: Iterable[str],
) -> SavedReviewResult | None:
    if not isinstance(value, dict) or "verdict" not in value:
        return None
    raw = value.get("coverage")
    if not isinstance(raw, dict) or type(raw.get("version")) is not int or raw["version"] != 1:
        return None
    if raw.get("head_sha") != head_sha or not isinstance(raw.get("status"), str):
        return None
    try:
        status = CoverageStatus(raw["status"])
        summary = ReviewSummary.model_validate(value.get("summary"))
    except (ValueError, ValidationError):
        return None
    counts = severity_counts(severities)
    saved_counts = value.get("severity_counts")
    if (
        not isinstance(saved_counts, dict)
        or saved_counts != counts
        or any(type(n) is not int for n in saved_counts.values())
    ):
        return None
    run_verdict = coverage_verdict(
        (name for name, count in counts.items() for _ in range(count)), status
    )
    if value["verdict"] != run_verdict:
        return None
    for key in ("snapshot_digest", "diff_digest"):
        digest = raw.get(key)
        if not isinstance(digest, str) or (
            not re.fullmatch(r"[0-9a-f]{64}", digest)
            and not (key == "diff_digest" and digest == "" and status is CoverageStatus.PARTIAL)
        ):
            return None
    total, reviewed = raw.get("total_files"), raw.get("reviewed_files")
    complete = raw.get("file_list_complete")
    if (
        type(total) is not int
        or not 0 <= total <= 3000
        or type(reviewed) is not int
        or not 0 <= reviewed <= total
        or type(complete) is not bool
    ):
        return None
    reasons = read_coverage_counts(raw.get("reason_counts"), COVERAGE_REASON_CODES)
    exclusions = read_coverage_counts(raw.get("exclusion_counts"), COVERAGE_EXCLUSION_CODES)
    if reasons is None or exclusions is None:
        return None
    if not valid_coverage_status(
        status,
        reviewed=reviewed,
        total=total,
        complete=complete,
        reasons=reasons,
        exclusions=exclusions,
    ):
        return None
    reason_items = read_coverage_items(raw.get("reasons"), reasons)
    exclusion_items = read_coverage_items(raw.get("exclusions"), exclusions)
    if reason_items is None or exclusion_items is None:
        return None
    variants = read_reason_variants(raw.get("reason_variant_counts"), reasons, reason_items)
    if variants is None:
        return None
    coverage = RunCoverage(
        status,
        reviewed,
        exclusion_items,
        reason_items,
        head_sha,
        raw["diff_digest"],
        raw["snapshot_digest"],
        total,
        complete,
        tuple(sorted(reasons.items())),
        tuple(sorted(exclusions.items())),
        stored_reason_variant_counts=tuple(sorted(variants.items())),
    )
    return SavedReviewResult(
        run_verdict,
        coverage,
        {
            "problem": summary.problem,
            "done_well": summary.done_well,
            "effort": summary.effort,
        },
    )


def fallback_review_result(
    head_sha: str,
    severities: Iterable[str],
    snapshots: list[DiffSnapshot],
    *,
    total_files: int | None = None,
) -> SavedReviewResult:
    values = tuple(severities)
    coverage = unavailable_run_coverage(snapshots, head_sha, metadata_only=True)
    if total_files is not None and total_files > len(snapshots):
        counts = coverage.reason_counts
        counts["classification_unavailable"] = (
            counts.get("classification_unavailable", 0) + total_files - len(snapshots)
        )
        coverage = replace(
            coverage, total_files=total_files, stored_reason_counts=tuple(sorted(counts.items()))
        )
    summary = summary_from_result(values, (), coverage)
    return SavedReviewResult(
        coverage_verdict(values, coverage.status),
        coverage,
        {
            "problem": summary.problem,
            "done_well": summary.done_well,
            "effort": summary.effort,
        },
    )
