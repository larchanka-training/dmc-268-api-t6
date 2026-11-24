"""Restoring saved coverage payloads against durable snapshots (#213, #392)."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from uuid import UUID

from app.common.application.git_identity import is_canonical_sha1
from app.modules.reviews.application.coverage_model import RunCoverage
from app.modules.reviews.application.coverage_projection import (
    empty_addition_candidate,
    project_run_coverage_with_digest,
)
from app.modules.reviews.application.coverage_schema import (
    COVERAGE_EXCLUSION_CODES,
    REASON_TEXT,
    CoverageItem,
    CoverageStatus,
    read_coverage_counts,
    read_coverage_items,
    read_reason_variants,
    valid_coverage_status,
)
from app.modules.reviews.application.diff_map import DiffMap, is_supported_path
from app.modules.reviews.application.get_run_diff import (
    DiffSnapshot,
    is_proven_empty_snapshot,
)

# Preserve the historical facade logger name so log filters keep matching after the split.
_LOGGER = logging.getLogger("app.modules.reviews.application.run_coverage")


@dataclass(frozen=True)
class CoverageContextPins:
    """Validated context identities needed to rebuild the canonical publication map."""

    diff_old_sha: str | None
    file_list_complete: bool


def read_context_coverage_pins(
    request: object, response: object, *, head_sha: str
) -> CoverageContextPins | None:
    """Decode persisted context bindings without trusting live refs or model output."""
    if not isinstance(request, dict) or request.get("head_sha") != head_sha:
        return None
    if not isinstance(response, dict):
        return None
    coverage, diff = response.get("coverage"), response.get("diff")
    if not isinstance(coverage, dict) or not isinstance(diff, dict):
        return None
    old_sha = diff.get("diff_old_sha")
    digest = coverage.get("diff_digest")
    if (
        type(coverage.get("version")) is not int
        or coverage["version"] != 1
        or coverage.get("head_sha") != head_sha
        or type(coverage.get("file_list_complete")) is not bool
        or not isinstance(digest, str)
        or re.fullmatch(r"[0-9a-f]{64}", digest) is None
        or diff.get("digest") != digest
        or (old_sha is not None and not is_canonical_sha1(old_sha))
    ):
        return None
    return CoverageContextPins(old_sha, coverage["file_list_complete"])


def restore_context_coverage_with_digest(
    request: object,
    response: object,
    *,
    snapshots: list[DiffSnapshot],
    diff_map: DiffMap,
    snapshot_digest: str,
    run_id: UUID | None = None,
) -> RunCoverage:
    """Restore effective policy for matching durable facts; unknown names stay private."""
    pins = read_context_coverage_pins(request, response, head_sha=diff_map.head_sha)
    if (
        pins is not None
        and isinstance(response, dict)
        and (
            pins.diff_old_sha == diff_map.diff_old_sha
            and pins.file_list_complete == diff_map.file_list_complete
        )
    ):
        restored = _restore_run_coverage(
            response.get("coverage"),
            snapshots=snapshots,
            diff_map=diff_map,
            snapshot_digest=snapshot_digest,
        )
        if restored is not None:
            return restored
    if request is not None or response is not None:
        _LOGGER.warning("Invalid saved context coverage run_id=%s", run_id)
    return unavailable_run_coverage_with_digest(
        snapshots, diff_map.head_sha, snapshot_digest=snapshot_digest
    )


def restore_run_coverage_with_digest(
    value: object,
    *,
    snapshots: list[DiffSnapshot],
    diff_map: DiffMap,
    snapshot_digest: str,
    run_id: UUID | None = None,
) -> RunCoverage | None:
    """Read saved policy-effective coverage only for the exact durable inputs."""
    result = _restore_run_coverage(
        value,
        snapshots=snapshots,
        diff_map=diff_map,
        snapshot_digest=snapshot_digest,
    )
    if result is None and value is not None:
        _LOGGER.warning("Invalid saved run coverage run_id=%s", run_id)
    return result


def _restore_run_coverage(
    value: object,
    *,
    snapshots: list[DiffSnapshot],
    diff_map: DiffMap,
    snapshot_digest: str,
) -> RunCoverage | None:
    if not isinstance(value, dict):
        return None
    if (
        type(value.get("version")) is not int
        or value["version"] != 1
        or value.get("head_sha") != diff_map.head_sha
        or value.get("diff_digest") != diff_map.digest
        or value.get("snapshot_digest") != snapshot_digest
        or type(value.get("file_list_complete")) is not bool
        or value["file_list_complete"] != diff_map.file_list_complete
        or type(value.get("total_files")) is not int
        or value["total_files"] != len(snapshots)
        or type(value.get("reviewed_files")) is not int
        or not 0 <= value["reviewed_files"] <= len(snapshots)
    ):
        return None
    raw_status = value.get("status")
    if not isinstance(raw_status, str):
        return None
    try:
        status = CoverageStatus(raw_status)
    except (ValueError, TypeError):
        return None
    reasons = read_coverage_counts(value.get("reason_counts"), set(REASON_TEXT))
    exclusions = read_coverage_counts(value.get("exclusion_counts"), COVERAGE_EXCLUSION_CODES)
    if reasons is None or exclusions is None:
        return None
    reviewed = value["reviewed_files"]
    # Effective policy may withhold a file, but it cannot create a model patch or
    # manufacture independent empty/binary/generated proof. The no-policy projection
    # partitions actual durable capacities before any saved aggregate is accepted.
    available = project_run_coverage_with_digest(
        snapshots,
        diff_map,
        snapshot_digest=snapshot_digest,
    )
    if reviewed > available.reviewed_files:
        return None
    if any(
        count > available.exclusion_counts.get(reason, 0)
        for reason, count in exclusions.items()
        if reason != "ignored"
    ):
        return None
    if any(count > available.reason_counts.get(reason, 0) for reason, count in reasons.items()):
        return None
    if any(
        reasons.get(reason, 0) != available.reason_counts.get(reason, 0)
        for reason in ("unsupported_path", "identity_unresolved")
    ):
        return None
    if not valid_coverage_status(
        status,
        reviewed=reviewed,
        total=len(snapshots),
        complete=diff_map.file_list_complete,
        reasons=reasons,
        exclusions=exclusions,
    ):
        return None
    if (not diff_map.file_list_complete) != (reasons.get("diff_inventory_incomplete") == 1):
        return None
    if status is CoverageStatus.COMPLETE and any(
        file.omission_reason is not None for file in diff_map.files
    ):
        return None
    paths = {item.filename for item in snapshots} | {
        item.previous_filename for item in snapshots if item.previous_filename
    }
    reason_items = read_coverage_items(value.get("reasons"), reasons, paths=paths)
    exclusion_items = read_coverage_items(value.get("exclusions"), exclusions, paths=paths)
    if reason_items is None or exclusion_items is None:
        return None
    qualified_paths = {item.filename for item in snapshots if empty_addition_candidate(item)}
    if any(
        item.empty_addition_candidate and item.path not in qualified_paths for item in reason_items
    ):
        return None
    variants = read_reason_variants(value.get("reason_variant_counts"), reasons, reason_items)
    if variants is None or any(
        count > available.reason_variant_counts.get(variant, 0)
        for variant, count in variants.items()
    ):
        return None
    return RunCoverage(
        status,
        reviewed,
        exclusion_items,
        reason_items,
        diff_map.head_sha,
        diff_map.digest,
        snapshot_digest,
        len(snapshots),
        diff_map.file_list_complete,
        tuple(sorted(reasons.items())),
        tuple(sorted(exclusions.items())),
        durable_reviewable_paths=available.durable_reviewable_paths,
        stored_reason_variant_counts=tuple(sorted(variants.items())),
    )


def unavailable_run_coverage_with_digest(
    snapshots: list[DiffSnapshot],
    head_sha: str,
    *,
    snapshot_digest: str,
    metadata_only: bool = False,
) -> RunCoverage:
    """Legacy or mismatched context cannot prove inventory or effective-policy coverage."""
    reasons = [CoverageItem(None, "classification_unavailable")]
    exclusions = []
    for snapshot in snapshots:
        path = snapshot.filename if is_supported_path(snapshot.filename) else None
        change_type = {"changed": "mode_changed", "removed": "deleted"}.get(
            snapshot.status, snapshot.status
        )
        reason = snapshot.omission_reason
        if path is None or (
            snapshot.previous_filename is not None
            and not is_supported_path(snapshot.previous_filename)
        ):
            reason = "unsupported_path"
            path = None
        elif snapshot.status in {"renamed", "copied"} and snapshot.previous_filename is None:
            reason = "identity_unresolved"
            path = None
        elif is_proven_empty_snapshot(snapshot) or snapshot.omission_reason in {
            "binary",
            "generated",
        }:
            exclusions.append(
                CoverageItem(None, snapshot.omission_reason or "empty_file", change_type)
            )
            continue
        elif reason not in {"too_large", "missing_patch"}:
            reason = (
                "summary_only"
                if snapshot.summary_only
                else (
                    "classification_unavailable"
                    if metadata_only or snapshot.review_patch is not None
                    else "missing_patch"
                )
            )
        reasons.append(CoverageItem(None, reason, change_type))
    return RunCoverage(
        CoverageStatus.PARTIAL,
        0,
        exclusions=tuple(exclusions),
        reasons=tuple(reasons),
        head_sha=head_sha,
        snapshot_digest=snapshot_digest,
        total_files=len(snapshots),
    )
