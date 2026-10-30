"""Internal durable coverage of the bounded VCS omission cases (#213)."""

from __future__ import annotations

import json
import logging
import re
from collections import Counter
from dataclasses import asdict, dataclass, replace
from hashlib import sha256
from typing import Any
from uuid import UUID

from app.common.application.git_identity import is_canonical_sha1
from app.modules.reviews.application.coverage_schema import (
    COVERAGE_EXCLUSION_CODES as COVERAGE_EXCLUSION_CODES,
)
from app.modules.reviews.application.coverage_schema import (
    COVERAGE_REASON_CODES as COVERAGE_REASON_CODES,
)
from app.modules.reviews.application.coverage_schema import (
    REASON_TEXT as _REASON_TEXT,
)
from app.modules.reviews.application.coverage_schema import (
    REASON_VARIANT_TEXT,
    TECHNICAL_REASON_CODES,
    read_reason_variants,
    reason_variant,
    valid_coverage_status,
)
from app.modules.reviews.application.coverage_schema import (
    CoverageItem as CoverageItem,
)
from app.modules.reviews.application.coverage_schema import (
    CoverageStatus as CoverageStatus,
)
from app.modules.reviews.application.coverage_schema import (
    read_coverage_counts as read_coverage_counts,
)
from app.modules.reviews.application.coverage_schema import (
    read_coverage_items as read_coverage_items,
)
from app.modules.reviews.application.coverage_schema import (
    reason_text as _reason_text,
)
from app.modules.reviews.application.diff_map import (
    DiffMap,
    PatchState,
    is_supported_path,
)
from app.modules.reviews.application.get_run_diff import (
    DiffSnapshot,
    is_proven_empty_snapshot,
)
from app.modules.reviews.application.review_ignore import IgnoreResult
from app.modules.reviews.application.vcs_diff import VcsFile, is_empty_addition_candidate

_LOGGER = logging.getLogger(__name__)
# The technical skips of a legacy (<= v3) run. It still calls the model for an unsupported
# path or an unresolved identity, so these stay incompleteness there (#130 F5, TL-Q3).
_LEGACY_TECHNICAL_REASON_CODES = frozenset({"too_large", "unsupported_change"})


@dataclass(frozen=True)
class RunCoverage:
    status: CoverageStatus
    reviewed_files: int
    exclusions: tuple[CoverageItem, ...] = ()
    reasons: tuple[CoverageItem, ...] = ()
    head_sha: str = ""
    diff_digest: str = ""
    snapshot_digest: str = ""
    total_files: int = 0
    file_list_complete: bool = False
    stored_reason_counts: tuple[tuple[str, int], ...] = ()
    stored_exclusion_counts: tuple[tuple[str, int], ...] = ()
    # Full durable model-patch capacity, not effective-policy authorization. This
    # is never serialized; only trusted allowed send-scope paths may intersect it.
    durable_reviewable_paths: frozenset[str] | None = None
    stored_reason_variant_counts: tuple[tuple[str, int], ...] = ()

    @property
    def reason_variant_counts(self) -> dict[str, int]:
        return dict(self.stored_reason_variant_counts) or dict(
            Counter(key for item in self.reasons if (key := reason_variant(item)) is not None)
        )

    @property
    def reason_counts(self) -> dict[str, int]:
        return dict(self.stored_reason_counts) or dict(
            Counter(item.reason for item in self.reasons)
        )

    @property
    def exclusion_counts(self) -> dict[str, int]:
        return dict(self.stored_exclusion_counts) or dict(
            Counter(item.reason for item in self.exclusions)
        )

    def summary_problem(self) -> str:
        prefix = (
            "Проверка неполная"
            if self.status is CoverageStatus.PARTIAL
            else "Текстовые изменения не проверены"
        )
        counts: Counter[str] = Counter()
        specialized: Counter[str] = Counter()
        for variant, count in self.reason_variant_counts.items():
            counts[REASON_VARIANT_TEXT[variant]] += count
            specialized[variant.split(":", 1)[0]] += count
        for reason, count in self.reason_counts.items():
            if count > specialized[reason]:
                counts[_REASON_TEXT.get(reason, reason)] += count - specialized[reason]
        causes = "; ".join(f"{text} ({count})" for text, count in sorted(counts.items()))
        return prefix + (f": {causes}" if causes else "") + "."

    def body_explanation(self) -> str:
        """Bound and escape provider paths in deterministic author-facing reasons."""
        lines = []
        for item in self.reasons[:5]:
            text = _reason_text(item)
            path = ""
            if item.path is not None:
                display = item.path[:120]
                delimiter = "`" * (
                    1 + max((len(run) for run in re.findall(r"`+", display)), default=0)
                )
                path = f"{delimiter} {display} {delimiter}"
            lines.append(f"- {path + ': ' if path else ''}{text}")
        remaining = sum(self.reason_counts.values()) - min(len(self.reasons), 5)
        if remaining:
            lines.append(f"- Ещё причин: {remaining}")
        return "\n".join(lines)

    @property
    def all_excluded(self) -> bool:
        return (
            self.file_list_complete
            and self.total_files > 0
            and sum(self.exclusion_counts.values()) == self.total_files
            and not self.reasons
        )

    @property
    def technical_only(self) -> bool:
        """No reviewable text, and at least one technical skip beside the exclusions.

        Coverage none with a reason: a complete inventory of T and E files (CVS §2.2).
        An exclusions-only change has no reason; it is ``all_excluded`` (T19).
        """
        return self.status is CoverageStatus.NONE and bool(self.reasons)

    def to_json(self) -> dict[str, Any]:
        """Bound displayed paths while retaining exact reason counts and identities."""

        def items(values: tuple[CoverageItem, ...]) -> list[dict[str, Any]]:
            return [
                {
                    key: value
                    for key, value in asdict(item).items()
                    if key != "empty_addition_candidate" or value
                }
                | {"path": None if item.reason == "ignored" else item.path}
                for item in sorted(
                    values, key=lambda item: (item.path or "", item.reason, item.change_type or "")
                )[:20]
            ]

        result = {
            "version": 1,
            "head_sha": self.head_sha,
            "diff_digest": self.diff_digest,
            "snapshot_digest": self.snapshot_digest,
            "file_list_complete": self.file_list_complete,
            "total_files": self.total_files,
            "reviewed_files": self.reviewed_files,
            "status": self.status.value,
            "exclusion_counts": dict(sorted(self.exclusion_counts.items())),
            "reason_counts": dict(sorted(self.reason_counts.items())),
            "exclusions": items(self.exclusions),
            "reasons": items(self.reasons),
        }
        if sum(self.reason_counts.values()) > 20 and self.reason_variant_counts:
            result["reason_variant_counts"] = dict(sorted(self.reason_variant_counts.items()))
        return result


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


def _empty_addition_candidate(snapshot: DiffSnapshot) -> bool:
    # DiffMap conversion strips no-hunk metadata, but such raw metadata is not
    # proof of an absent patch. Only the exact stored empty wrapper may be removed.
    empty_wrapper = (
        f"diff --git a/{snapshot.filename} b/{snapshot.filename}\n"
        f"--- a/{snapshot.filename}\n"
        f"+++ b/{snapshot.filename}\n"
    )
    patch = None if snapshot.patch == empty_wrapper else snapshot.patch
    return is_empty_addition_candidate(replace(snapshot_vcs_file(snapshot), patch=patch))


def project_run_coverage(
    snapshots: list[DiffSnapshot],
    diff_map: DiffMap,
    *,
    ignore: IgnoreResult | None = None,
    snapshot_path: bool = False,
) -> RunCoverage:
    """Classify every stored file of the attempt under its effective policy.

    ``snapshot_path``: the run takes the snapshot path, which ends a change of technical
    skips only without a model call, so an unsupported path and an unresolved identity
    count as technical skips (CVS §2.2). A legacy run keeps them incomplete.
    """
    return _project_run_coverage(
        snapshots,
        diff_map,
        ignore=ignore,
        snapshot_digest=snapshot_coverage_digest(snapshots),
        snapshot_path=snapshot_path,
    )


def _project_run_coverage(
    snapshots: list[DiffSnapshot],
    diff_map: DiffMap,
    *,
    ignore: IgnoreResult | None = None,
    snapshot_digest: str,
    snapshot_path: bool = False,
) -> RunCoverage:
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
            _empty_addition_candidate(snapshot),
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
                    _empty_addition_candidate(snapshot),
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


def restore_context_coverage(
    request: object,
    response: object,
    *,
    snapshots: list[DiffSnapshot],
    diff_map: DiffMap,
    run_id: UUID | None = None,
) -> RunCoverage:
    """Restore effective policy only for matching durable facts; unknown names stay private."""
    snapshot_digest = snapshot_coverage_digest(snapshots)
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
    return _unavailable_run_coverage(snapshots, diff_map.head_sha, snapshot_digest=snapshot_digest)


def restore_run_coverage(
    value: object,
    *,
    snapshots: list[DiffSnapshot],
    diff_map: DiffMap,
    run_id: UUID | None = None,
) -> RunCoverage | None:
    """Read saved policy-effective coverage only for the exact durable inputs."""
    result = _restore_run_coverage(
        value,
        snapshots=snapshots,
        diff_map=diff_map,
        snapshot_digest=snapshot_coverage_digest(snapshots),
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
    reasons = read_coverage_counts(value.get("reason_counts"), set(_REASON_TEXT))
    exclusions = read_coverage_counts(value.get("exclusion_counts"), COVERAGE_EXCLUSION_CODES)
    if reasons is None or exclusions is None:
        return None
    reviewed = value["reviewed_files"]
    # Effective policy may withhold a file, but it cannot create a model patch or
    # manufacture independent empty/binary/generated proof. The no-policy projection
    # partitions actual durable capacities before any saved aggregate is accepted.
    available = _project_run_coverage(snapshots, diff_map, snapshot_digest=snapshot_digest)
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
    qualified_paths = {item.filename for item in snapshots if _empty_addition_candidate(item)}
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


def unavailable_run_coverage(
    snapshots: list[DiffSnapshot], head_sha: str, *, metadata_only: bool = False
) -> RunCoverage:
    """Legacy or mismatched context cannot prove inventory or effective-policy coverage."""
    return _unavailable_run_coverage(
        snapshots,
        head_sha,
        metadata_only=metadata_only,
        snapshot_digest=snapshot_coverage_digest(snapshots),
    )


def _unavailable_run_coverage(
    snapshots: list[DiffSnapshot],
    head_sha: str,
    *,
    metadata_only: bool = False,
    snapshot_digest: str,
) -> RunCoverage:
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
