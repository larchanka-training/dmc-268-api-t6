"""The bot check-run of one Run: target, report and §7 status table (docs/PIPELINE_SPEC.md §7)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Protocol
from uuid import UUID

from app.modules.reviews.application.run_failures import MAX_ATTEMPTS
from app.modules.reviews.application.verdict import severity_count_phrase

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
    "commit_messages_fetch_failed": "Не удалось получить сообщения коммитов PR из GitHub.",
    "github_forbidden": "У GitHub App нет доступа к репозиторию или PR.",
    "bitbucket_source_unavailable": "Нет доступа к исходному fork-репозиторию Bitbucket.",
    "bitbucket_review_limit_exceeded": "PR Bitbucket превышает лимит файлов для ревью.",
    "bitbucket_response_budget_exceeded": "Исчерпан бюджет ответов Bitbucket для одного прогона.",
    "bitbucket_engine_unsupported": "Выбранный движок ревью не поддерживается Bitbucket.",
    "github_publish_failed": "GitHub не принял ревью.",
    "pr_base_changed": (
        "Целевая ветка PR изменилась после запуска ревью. Перезапустите ревью из UI."
    ),
    "lease_expired": "Обработка прервалась 3 раза подряд.",
    "internal_error": "Внутренняя ошибка сервиса.",
    "superseded": "В PR новый коммит — ревью будет для него.",
    "cancelled_by_user": "Прогон отменён из UI.",
    "pr_closed": "PR закрыт.",
    "rule_not_matched": "Целевая ветка PR не подходит под фильтр веток репозитория.",
    "all_changes_ignored": "Все изменения исключены из AI-ревью.",
    "no_changes": "В PR нет изменённых файлов. Ревью не выполнялось.",
    "policy_document_too_large": (
        "Документ правил превышает 64 КиБ. Ревью не выполнено; уменьшите документ."
    ),
    "policy_document_decode_error": (
        "Документ правил должен быть корректным UTF-8. Исправьте кодировку."
    ),
    "policy_document_control_character": (
        "В документе правил есть недопустимый управляющий символ."
    ),
    "policy_document_type_unsupported": "Документ правил должен быть обычным файлом в Git.",
    "policy_ignore_block_duplicate": "В .review/rules.md допустим один блок review-ignore.",
    "policy_ignore_block_nested": (
        "Блок review-ignore должен быть на верхнем уровне .review/rules.md."
    ),
    "policy_ignore_block_unclosed": "Блок review-ignore в .review/rules.md не закрыт.",
    "policy_ignore_info_invalid": "review-ignore не принимает дополнительные параметры.",
    "policy_ignore_pattern_invalid": "В блоке review-ignore недопустимая маска.",
    "policy_ignore_limit_exceeded": (
        "В блоке review-ignore слишком много масок или слишком длинная маска."
    ),
    "policy_path_invalid": "В изменениях есть недопустимый путь файла.",
    "policy_match_budget_exceeded": (
        "Маски review-ignore слишком дороги в проверке. Упростите политику."
    ),
    "policy_prompt_budget_exceeded": ("Документы правил не помещаются целиком в контекст модели."),
    "policy_access_denied": "Нет доступа к документам правил репозитория.",
    "policy_revision_unavailable": "Закреплённая ревизия правил недоступна.",
    "policy_fetch_transient": "Не удалось прочитать документы правил из GitHub.",
    "policy_rate_limited": "GitHub ограничил частоту запросов при чтении правил.",
    "policy_snapshot_integrity_error": "Снимок правил прогона повреждён или противоречив.",
    "policy_context_violation": "В контекст модели попал запрещённый источник.",
    "budget_paused": "Дневной бюджет исчерпан, ревью на паузе.",
    "verification_context_overflow": "Обязательные данные проверки не помещаются в контекст.",
    "verification_invalid_output": "Проверяющий вернул ответ не по контракту.",
    "verification_unavailable": "Проверяющий временно недоступен.",
    "verification_budget_exceeded": "Бюджета недостаточно для обязательной проверки.",
    "verification_deadline_exceeded": "Недостаточно времени для обязательной проверки.",
}


def failure_reason(error_code: str | None, detail: str | None = None) -> str:
    """The author-facing text of a catalog code and where it happened.

    An unknown code is shown as is. Shared by the check-run and the rules status of a
    repository (#150), so that both say the same thing about one policy error.
    """
    reason = _ERROR_TEXTS.get(error_code or "", error_code or "")
    return f"{reason} Где: {detail}." if detail else reason


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
    # GitHub "View more details" target: the Run page; None leaves the App homepage.
    details_url: str | None = None


@dataclass(frozen=True)
class CheckRunReport:
    """What the check-run shows for the current Run state."""

    target: CheckRunTarget
    state: str
    attempt: int
    error_code: str | None = None
    # Where a policy failure is: document, line and position, limit and actual value.
    error_detail: str | None = None
    verdict: str | None = None
    severity_counts: dict[str, int] = field(default_factory=dict)
    inline_count: int = 0
    body_count: int = 0
    # Findings stored with ``drop_reason = severity_threshold`` (PIPELINE_SPEC §7, §9).
    below_threshold_count: int = 0
    summary_only: bool = False
    run_url: str | None = None
    provider: str = "github"
    coverage_status: str | None = None
    saved_summary: dict[str, str] | None = None


type RunProviderCheckpoint = Callable[[], Awaitable[None]]


class CheckRunGateway(Protocol):
    """Create or update the one check-run with ``external_id = run_id`` (idempotent)."""

    async def upsert(
        self, target: CheckRunTarget, view: CheckRunView, *, checkpoint: RunProviderCheckpoint
    ) -> None: ...


def check_run_view(report: CheckRunReport) -> CheckRunView:
    link = f"\n\n{report.run_url}" if report.run_url else ""
    if report.state in {"queued", "running", "publishing"}:
        return CheckRunView(
            "in_progress",
            None,
            "AI-ревью выполняется",
            f"попытка {max(report.attempt, 1)} из {MAX_ATTEMPTS}",
            details_url=report.run_url,
        )
    if report.state == "succeeded":
        explanation = (
            ""
            if report.saved_summary is None
            else (f"\n{report.saved_summary['problem']}\n{report.saved_summary['done_well']}")
        )
        if report.summary_only:
            return CheckRunView(
                "completed",
                "neutral",
                "AI-ревью: только сводка",
                f"PR слишком большой: только сводка{explanation}{link}",
                details_url=report.run_url,
            )
        counts = severity_count_phrase(report.severity_counts)
        below = (
            f"\nниже порога severity: {report.below_threshold_count}"
            if report.below_threshold_count
            else ""
        )
        title = report.verdict or (
            "нет доступных для анализа изменений"
            if report.coverage_status == "none"
            else "проверка неполная"
        )
        return CheckRunView(
            "completed",
            "neutral",
            f"AI-ревью: {title}",
            f"{counts}\ninline: {report.inline_count}, в теле ревью: {report.body_count}"
            f"{explanation}{below}{link}",
            details_url=report.run_url,
        )
    reason = failure_reason(report.error_code, report.error_detail)
    if report.state == "failed":
        summary = (
            f"{reason}{link}"
            if report.error_code == "llm_payment_required"
            else f"`{report.error_code}`: {reason}{link}"
        )
        return CheckRunView(
            "completed",
            "neutral",
            "AI-ревью не выполнено",
            summary,
            details_url=report.run_url,
        )
    if report.state == "cancelled":
        return CheckRunView(
            "completed",
            "cancelled",
            "AI-ревью отменено",
            reason,
            details_url=report.run_url,
        )
    return CheckRunView(
        "completed",
        "skipped",
        "AI-ревью пропущено",
        reason,
        details_url=report.run_url,
    )
