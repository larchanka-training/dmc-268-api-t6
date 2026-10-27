"""Bounded persisted coverage schema and canonical author-facing reason vocabulary."""

from __future__ import annotations

from collections import Counter
from collections.abc import Set
from dataclasses import dataclass
from enum import StrEnum

from app.modules.reviews.application.diff_map import ChangeType, is_supported_path

REASON_TEXT = {
    "classification_unavailable": "нет достоверного сохранённого покрытия",
    "diff_inventory_incomplete": "список изменённых файлов неполон",
    "missing_patch": "Отсутствует подтверждённый patch",
    "unsupported_change": "семантика изменения метаданных не анализируется",
    "too_large": "Превышен лимит размера, содержимое не проверено",
    "unsupported_path": "неподдерживаемый путь",
    "identity_unresolved": "не установлена идентичность файла",
    "patch_truncated": "patch неполон",
    "patch_malformed": "patch повреждён",
    "patch_unsupported": "формат patch не поддерживается",
    "summary_only": "краткий обзор без проверки всех строк",
    "review_input_incomplete": "модель получила не весь patch",
    "unknown_path": "не удалось сопоставить замечание с доступным diff",
}
COVERAGE_REASON_CODES = frozenset(REASON_TEXT)
COVERAGE_EXCLUSION_CODES = frozenset({"ignored", "binary", "generated", "empty_file"})
# CVS §2.2 T: a technical skip, a file without text the review could check. A complete
# inventory of T and exclusions only has coverage none. Only the snapshot path classifies
# an unsupported path or an unresolved identity so (#130 F5); see ``project_run_coverage``.
TECHNICAL_REASON_CODES = frozenset(
    {"too_large", "unsupported_change", "unsupported_path", "identity_unresolved"}
)


class CoverageStatus(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    NONE = "none"


@dataclass(frozen=True)
class CoverageItem:
    path: str | None
    reason: str
    change_type: str | None = None
    empty_addition_candidate: bool = False


def read_coverage_counts(value: object, allowed: Set[str]) -> dict[str, int] | None:
    if not isinstance(value, dict):
        return None
    result: dict[str, int] = {}
    for key, count in value.items():
        if (
            not isinstance(key, str)
            or key not in allowed
            or type(count) is not int
            or not 0 < count <= 3001
        ):
            return None
        result[key] = count
    return result


def read_coverage_items(
    value: object, counts: dict[str, int], *, paths: set[str] | None = None
) -> tuple[CoverageItem, ...] | None:
    if not isinstance(value, list) or len(value) != min(sum(counts.values()), 20):
        return None
    result = []
    for raw in value:
        if not isinstance(raw, dict) or set(raw) not in (
            {"path", "reason", "change_type"},
            {"path", "reason", "change_type", "empty_addition_candidate"},
        ):
            return None
        path, reason, change_type = raw["path"], raw["reason"], raw["change_type"]
        qualified = raw.get("empty_addition_candidate", False)
        if type(qualified) is not bool or (
            qualified and (reason != "missing_patch" or change_type != "added" or path is None)
        ):
            return None
        if not isinstance(reason, str) or reason not in counts:
            return None
        if path is not None and (
            not isinstance(path, str)
            or not is_supported_path(path)
            or (paths is not None and path not in paths)
        ):
            return None
        if change_type is not None and (
            not isinstance(change_type, str)
            or change_type not in {item.value for item in ChangeType}
        ):
            return None
        result.append(CoverageItem(path, reason, change_type, qualified))
    if any(
        count > counts[reason] for reason, count in Counter(item.reason for item in result).items()
    ):
        return None
    return tuple(result)


def valid_coverage_status(
    status: CoverageStatus,
    *,
    reviewed: int,
    total: int,
    complete: bool,
    reasons: dict[str, int],
    exclusions: dict[str, int],
) -> bool:
    """Validate the same status/partition invariants at both persistence boundaries."""
    if status is CoverageStatus.COMPLETE and (not complete or reviewed == 0 or reasons):
        return False
    if status is CoverageStatus.NONE and (
        not complete or reviewed or set(reasons) - TECHNICAL_REASON_CODES
    ):
        return False
    if status is CoverageStatus.PARTIAL and not reasons:
        return False
    if reasons.get("diff_inventory_incomplete", 0) > 1 or (
        complete and "diff_inventory_incomplete" in reasons
    ):
        return False
    global_reasons = (
        int("classification_unavailable" in reasons)
        + reasons.get("diff_inventory_incomplete", 0)
        + reasons.get("unknown_path", 0)
    )
    return reviewed + sum(reasons.values()) + sum(exclusions.values()) == total + global_reasons


REASON_VARIANT_TEXT = {
    "missing_patch:empty_addition": "Нет подтверждения обычного пустого файла",
    "missing_patch:renamed": (
        "Отсутствующий patch не подтверждает неизменность содержимого переноса"
    ),
    "missing_patch:mode_changed": (
        "Отсутствующий patch не подтверждает изменение только прав доступа/типа файла"
    ),
    "missing_patch:deleted": "Удалённое содержимое недоступно для подтверждения binary",
    "unsupported_change:renamed": "Семантика переноса файла (включая импорты) не анализируется",
    "unsupported_change:mode_changed": "Изменения прав доступа/типа файла не анализируются",
}


def reason_variant(item: CoverageItem) -> str | None:
    subtype = "empty_addition" if item.empty_addition_candidate else item.change_type
    key = f"{item.reason}:{subtype}"
    return key if key in REASON_VARIANT_TEXT else None


def reason_text(item: CoverageItem) -> str:
    variant = reason_variant(item)
    return REASON_VARIANT_TEXT[variant] if variant else REASON_TEXT.get(item.reason, item.reason)


def read_reason_variants(
    value: object,
    counts: dict[str, int],
    items: tuple[CoverageItem, ...],
) -> dict[str, int] | None:
    """Bound semantic subcounts without storing paths outside the diagnostic window."""
    listed = Counter(key for item in items if (key := reason_variant(item)) is not None)
    if value is None:
        return dict(listed)
    variants = read_coverage_counts(value, frozenset(REASON_VARIANT_TEXT))
    if variants is None or any(variants.get(key, 0) < count for key, count in listed.items()):
        return None
    by_reason: Counter[str] = Counter()
    for key, count in variants.items():
        by_reason[key.split(":", 1)[0]] += count
    visible = Counter(item.reason for item in items)
    visible_specialized: Counter[str] = Counter()
    for key, count in listed.items():
        visible_specialized[key.split(":", 1)[0]] += count
    if any(
        count > counts.get(reason, 0) - visible[reason] + visible_specialized[reason]
        for reason, count in by_reason.items()
    ):
        return None
    return variants
