"""The bot check-run of one Run: target, report and §7 status table (docs/PIPELINE_SPEC.md §7)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol
from uuid import UUID

from app.modules.reviews.application.run_failures import MAX_ATTEMPTS
from app.modules.reviews.application.verdict import SEVERITIES

# Author-facing texts of the §6 error_code catalog.
_ERROR_TEXTS = {
    "llm_timeout": "Модель не ответила вовремя. Прогон можно перезапустить из UI.",
    "llm_rate_limited": "Провайдер модели ограничил частоту запросов. Перезапустите прогон позже.",
    "llm_payment_required": "AI-ревью временно недоступно.",
    "llm_unavailable": "Провайдер модели недоступен.",
    "llm_invalid_output": "Модель вернула ответ не по контракту.",
    "llm_context_overflow": "PR не помещается в контекст модели.",
    "budget_exceeded": "Превышен лимит стоимости прогона.",
    "deadline_exceeded": "Прогон не уложился в лимит времени.",
    "diff_fetch_failed": "Не удалось получить дифф из GitHub.",
    "github_forbidden": "У GitHub App нет доступа к репозиторию или PR.",
    "github_publish_failed": "GitHub не принял ревью.",
    "lease_expired": "Обработка прервалась 3 раза подряд.",
    "internal_error": "Внутренняя ошибка сервиса.",
    "superseded": "В PR новый коммит — ревью будет для него.",
    "cancelled_by_user": "Прогон отменён из UI.",
    "pr_closed": "PR закрыт.",
    "rule_not_matched": "PR не прошёл правило отбора репозитория.",
    "budget_paused": "Дневной бюджет исчерпан, ревью на паузе.",
}


@dataclass(frozen=True)
class CheckRunTarget:
    installation_id: int
    repository_full_name: str
    head_sha: str
    run_id: UUID


@dataclass(frozen=True)
class CheckRunView:
    status: str
    conclusion: str | None
    title: str
    summary: str


@dataclass(frozen=True)
class CheckRunReport:
    """What the check-run shows for the current Run state."""

    target: CheckRunTarget
    state: str
    attempt: int
    error_code: str | None = None
    verdict: str | None = None
    severity_counts: dict[str, int] = field(default_factory=dict)
    inline_count: int = 0
    body_count: int = 0
    summary_only: bool = False
    run_url: str | None = None


class CheckRunGateway(Protocol):
    """Create or update the one check-run with ``external_id = run_id`` (idempotent)."""

    async def upsert(self, target: CheckRunTarget, view: CheckRunView) -> None: ...


def check_run_view(report: CheckRunReport) -> CheckRunView:
    link = f"\n\n{report.run_url}" if report.run_url else ""
    if report.state in {"queued", "running", "publishing"}:
        return CheckRunView(
            "in_progress",
            None,
            "AI-ревью выполняется",
            f"попытка {max(report.attempt, 1)} из {MAX_ATTEMPTS}",
        )
    if report.state == "succeeded":
        if report.summary_only:
            return CheckRunView(
                "completed",
                "neutral",
                "AI-ревью: только сводка",
                f"PR слишком большой: только сводка{link}",
            )
        counts = ", ".join(f"{name}: {report.severity_counts.get(name, 0)}" for name in SEVERITIES)
        return CheckRunView(
            "completed",
            "neutral",
            f"AI-ревью: {report.verdict or 'clean'}",
            f"{counts}\ninline: {report.inline_count}, в теле ревью: {report.body_count}{link}",
        )
    reason = _ERROR_TEXTS.get(report.error_code or "", report.error_code or "")
    if report.state == "failed":
        summary = (
            f"{reason}{link}"
            if report.error_code == "llm_payment_required"
            else f"`{report.error_code}`: {reason}{link}"
        )
        return CheckRunView("completed", "neutral", "AI-ревью не выполнено", summary)
    if report.state == "cancelled":
        return CheckRunView("completed", "cancelled", "AI-ревью отменено", reason)
    return CheckRunView("completed", "skipped", "AI-ревью пропущено", reason)
