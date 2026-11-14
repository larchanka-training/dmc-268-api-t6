"""Projecting durable VCS snapshots and DiffMap files into RunCoverage (#213, #392)."""

from __future__ import annotations

import json
from dataclasses import asdict, replace
from hashlib import sha256

from app.modules.reviews.application.coverage_model import RunCoverage
from app.modules.reviews.application.coverage_schema import (
    TECHNICAL_REASON_CODES,
    CoverageItem,
    CoverageStatus,
)
from app.modules.reviews.application.diff_map import DiffMap, PatchState
from app.modules.reviews.application.get_run_diff import (
    DiffSnapshot,
    is_proven_empty_snapshot,
)
from app.modules.reviews.application.review_ignore import IgnoreResult
from app.modules.reviews.application.vcs_diff import VcsFile, is_empty_addition_candidate

# The technical skips of a legacy (<= v3) run. It still calls the model for an unsupported
# path or an unresolved identity, so these stay incompleteness there (#130 F5, TL-Q3).
_LEGACY_TECHNICAL_REASON_CODES = frozenset({"too_large", "unsupported_change"})


def snapshot_coverage_digest(snapshots: list[DiffSnapshot]) -> str:
    """Tie effective-policy classification to the exact durable model/VCS inputs."""
    payload = json.dumps(
        [asdict(item) for item in snapshots],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return sha256(payload.encode("utf-8")).hexdigest()


def snapshot_vcs_file(snapshot: DiffSnapshot) -> VcsFile:
    patch = snapshot.patch
    if patch is not None and patch.startswith("diff --git "):
        hunk_start = patch.find("\n@@ ")
        patch = None if hunk_start < 0 else patch[hunk_start + 1 :]
    return VcsFile(
        snapshot.filename,
        snapshot.status,
        snapshot.blob_sha,
        snapshot.previous_filename,
        snapshot.additions,
        snapshot.deletions,
        snapshot.changes,
        patch,
    )


def empty_addition_candidate(snapshot: DiffSnapshot) -> bool:
    # DiffMap conversion strips no-hunk metadata, but such raw metadata is not
    # proof of an absent patch. Only the exact stored empty wrapper may be removed.
    empty_wrapper = (
        f"diff --git a/{snapshot.filename} b/{snapshot.filename}\n"
        f"--- a/{snapshot.filename}\n"
        f"+++ b/{snapshot.filename}\n"
    )
    patch = None if snapshot.patch == empty_wrapper else snapshot.patch
    return is_empty_addition_candidate(replace(snapshot_vcs_file(snapshot), patch=patch))


def project_run_coverage_with_digest(
    snapshots: list[DiffSnapshot],
    diff_map: DiffMap,
    *,
    snapshot_digest: str,
    ignore: IgnoreResult | None = None,
    snapshot_path: bool = False,
) -> RunCoverage:
    """Classify every stored file under a precomputed ``snapshot_digest``.

    Public facade entry points live exclusively in ``run_coverage.py`` and supply
    ``snapshot_coverage_digest(snapshots)`` so callers share one seam.

    ``snapshot_path``: the run takes the snapshot path, which ends a change of technical
    skips only without a model call, so an unsupported path and an unresolved identity
    count as technical skips (CVS §2.2). A legacy run keeps them incomplete.
    """
    reasons: list[CoverageItem] = []
    exclusions: list[CoverageItem] = []
    reviewed = 0
    reviewable_paths: set[str] = set()
    summary_only = any(snapshot.summary_only for snapshot in snapshots)
    if not diff_map.file_list_complete:
        reasons.append(CoverageItem(None, "diff_inventory_incomplete"))
    for snapshot, file in zip(snapshots, diff_map.files, strict=True):
        path = file.new_path or file.old_path
        item = CoverageItem(
            path,
            "missing_patch",
            file.change_type.value,
            empty_addition_candidate(snapshot),
        )
        if file.omission_reason is not None:
            reasons.append(CoverageItem(None, file.omission_reason.value, file.change_type.value))
        elif ignore is not None and (
            ignore.decision_for(file.file_id) is not None
            or any(name in ignore.denied_paths for name in (file.old_path, file.new_path) if name)
        ):
            exclusions.append(CoverageItem(path, "ignored", file.change_type.value))
        elif is_proven_empty_snapshot(snapshot):
            exclusions.append(CoverageItem(path, "empty_file", file.change_type.value))
        elif (
            snapshot.omission_reason in {"binary", "generated"}
            or file.patch_state is PatchState.BINARY
        ):
            reason = "generated" if snapshot.omission_reason == "generated" else "binary"
            exclusions.append(CoverageItem(path, reason, file.change_type.value))
        elif snapshot.omission_reason == "too_large":
            reasons.append(CoverageItem(path, "too_large", file.change_type.value))
        elif summary_only and snapshot.omission_reason == "missing_patch":
            # The stop flags this row so ``summaryOnly`` is true. The omission stays the
            # coverage reason; the branch below would replace it.
            reasons.append(
                CoverageItem(
                    path,
                    "missing_patch",
                    file.change_type.value,
                    empty_addition_candidate(snapshot),
                )
            )
        elif summary_only:
            reasons.append(CoverageItem(path, "summary_only", file.change_type.value))
        elif _proven_metadata_only(snapshot):
            reasons.append(CoverageItem(path, "unsupported_change", file.change_type.value))
        elif file.patch_state in {
            PatchState.TRUNCATED,
            PatchState.MALFORMED,
            PatchState.UNSUPPORTED,
        }:
            reasons.append(
                CoverageItem(path, f"patch_{file.patch_state.value}", file.change_type.value)
            )
        elif snapshot.review_patch is not None and (
            snapshot.review_patch != snapshot_vcs_file(snapshot).patch
        ):
            reasons.append(CoverageItem(path, "review_input_incomplete", file.change_type.value))
        elif (
            file.anchorable
            and snapshot.review_patch is not None
            and snapshot.omission_reason is None
            and snapshot.changes == snapshot.additions + snapshot.deletions > 0
        ):
            reviewed += 1
            if path is not None:
                reviewable_paths.add(path)
        else:
            reasons.append(item)
    technical = TECHNICAL_REASON_CODES if snapshot_path else _LEGACY_TECHNICAL_REASON_CODES
    incomplete = any(item.reason not in technical for item in reasons)
    status = (
        CoverageStatus.PARTIAL
        if incomplete or (reviewed and reasons)
        else (CoverageStatus.COMPLETE if reviewed else CoverageStatus.NONE)
    )
    return RunCoverage(
        status,
        reviewed,
        tuple(exclusions),
        tuple(reasons),
        diff_map.head_sha,
        diff_map.digest,
        snapshot_digest,
        len(snapshots),
        diff_map.file_list_complete,
        durable_reviewable_paths=frozenset(reviewable_paths),
    )


def _proven_metadata_only(snapshot: DiffSnapshot) -> bool:
    """Recognize exact no-text metadata when a provider actually supplied it.

    A status and zero counters without these facts do not establish a pure move or
    mode-only change. GitHub's ordinary no-patch entries therefore stay incomplete.
    """
    if snapshot.additions != 0 or snapshot.deletions != 0 or snapshot.changes != 0:
        return False
    if snapshot.patch is None:
        return False
    if snapshot.status == "renamed" and snapshot.previous_filename is not None:
        expected = (
            f"diff --git a/{snapshot.previous_filename} b/{snapshot.filename}\n"
            "similarity index 100%\n"
            f"rename from {snapshot.previous_filename}\nrename to {snapshot.filename}"
        )
        return snapshot.patch.rstrip("\n") == expected
    if snapshot.status == "changed":
        prefix = f"diff --git a/{snapshot.filename} b/{snapshot.filename}\n"
        return snapshot.patch.rstrip("\n") in {
            prefix + "old mode 100644\nnew mode 100755",
            prefix + "old mode 100755\nnew mode 100644",
        }
    return False
