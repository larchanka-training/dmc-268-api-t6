"""Durable run coverage aggregate and presentation/serialization methods (#213, #392)."""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import asdict, dataclass
from typing import Any

from app.modules.reviews.application.coverage_schema import (
    REASON_TEXT,
    REASON_VARIANT_TEXT,
    CoverageItem,
    CoverageStatus,
    reason_text,
    reason_variant,
)


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
                counts[REASON_TEXT.get(reason, reason)] += count - specialized[reason]
        causes = "; ".join(f"{text} ({count})" for text, count in sorted(counts.items()))
        return prefix + (f": {causes}" if causes else "") + "."

    def body_explanation(self) -> str:
        """Bound and escape provider paths in deterministic author-facing reasons."""
        lines = []
        for item in self.reasons[:5]:
            text = reason_text(item)
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

        def shown_path(item: CoverageItem) -> str | None:
            return None if item.reason == "ignored" else item.path

        def items(values: tuple[CoverageItem, ...]) -> list[dict[str, Any]]:
            order = sorted(
                values, key=lambda item: (item.path or "", item.reason, item.change_type or "")
            )
            # The review-body checklist names files from this window (CVS §8.2): each reason
            # with a name keeps its first name, then names go before the items without one.
            first_named: dict[str, int] = {}
            for index, item in enumerate(order):
                if shown_path(item) is not None:
                    first_named.setdefault(item.reason, index)
            ranked = sorted(
                range(len(order)),
                key=lambda index: (
                    first_named.get(order[index].reason) != index,
                    shown_path(order[index]) is None,
                    index,
                ),
            )
            return [
                {
                    key: value
                    for key, value in asdict(order[index]).items()
                    if key != "empty_addition_candidate" or value
                }
                | {"path": shown_path(order[index])}
                for index in sorted(ranked[:20])
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
