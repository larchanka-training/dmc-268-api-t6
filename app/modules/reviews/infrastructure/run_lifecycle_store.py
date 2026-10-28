"""PostgreSQL state transitions of the worker, publisher and reconciler (T4-T18)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import ColumnElement, Select, and_, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.common.infrastructure.db.enums import CodeChangeState, Engine, RunState
from app.common.infrastructure.db.unit_of_work import SqlAlchemyUnitOfWork
from app.modules.analytics.infrastructure.models import UsageEvent
from app.modules.integrations.gitlab.infrastructure.request_credentials import (
    active_gitlab_target_metadata,
)
from app.modules.repositories.infrastructure.models import ProviderInstallation, Repository
from app.modules.reviews.application.check_runs import (
    CheckRunReport,
    CheckRunTarget,
    summary_only_reason_of,
)
from app.modules.reviews.application.handle_review_run import RunGuardSnapshot
from app.modules.reviews.application.policy_errors import policy_error_detail
from app.modules.reviews.application.process_run import RunVcsInput
from app.modules.reviews.application.publish_run_review import (
    PublicationIdentity,
    PublishContext,
    publication_matches,
)
from app.modules.reviews.application.queue_messages import ReviewPublishPointer, StoredRunMessage
from app.modules.reviews.application.run_failures import RunCancelled, cancellation_reason
from app.modules.reviews.application.try_enqueue_webhook_run import PendingRunMessage
from app.modules.reviews.application.vcs_diff import PullRequestLocator
from app.modules.reviews.application.verdict import (
    HashedFinding,
    findings_hash,
    review_event,
    severity_counts,
)
from app.modules.reviews.infrastructure.models import (
    CodeChange,
    CodeChangeDiff,
    Comment,
    ContextPayload,
    Finding,
    Run,
)
from app.modules.reviews.infrastructure.publication_findings import published_finding_from_row
from app.modules.reviews.infrastructure.run_action_records import insert_run_action
from app.modules.reviews.infrastructure.run_notifications import notify_run_state
from app.modules.reviews.infrastructure.run_origin import active_origin_condition, origin_active
from app.modules.reviews.infrastructure.run_origin_locks import lock_run_origin_row
from app.modules.reviews.infrastructure.run_repository import load_publication_diff_map
from app.modules.reviews.infrastructure.saved_review_results import load_review_results
from app.modules.reviews.infrastructure.webhook_runs import SqlAlchemyWebhookRunStore
from app.modules.workspaces.infrastructure.models import Workspace

_ROW = (Run, CodeChange, Repository, ProviderInstallation)


def _joined(
    *, provider: bool = False
) -> Select[tuple[Run, CodeChange, Repository, ProviderInstallation]]:
    statement = (
        select(*_ROW)
        .join(CodeChange, Run.code_change_id == CodeChange.id)
        .join(Repository, CodeChange.repository_id == Repository.id)
        .join(
            ProviderInstallation,
            (
                Run.origin_provider_installation_id
                if provider
                else Repository.provider_installation_id
            )
            == ProviderInstallation.id,
        )
    )
    return statement.where(active_origin_condition()) if provider else statement


def _pointer_rows() -> Select[tuple[Run, CodeChange, Repository, ProviderInstallation]]:
    """Supported, active immutable origins; apply authority before a batch limit."""
    gitlab = active_gitlab_target_metadata().subquery()
    return (
        _joined(provider=True)
        .outerjoin(
            gitlab,
            and_(
                gitlab.c.origin_provider_installation_id == Run.origin_provider_installation_id,
                gitlab.c.workspace_id == ProviderInstallation.workspace_id,
                gitlab.c.repository_id == Repository.id,
                gitlab.c.project_id == Repository.external_id,
            ),
        )
        .where(
            or_(
                ProviderInstallation.provider == "github",
                and_(
                    ProviderInstallation.provider == "gitlab",
                    gitlab.c.origin_provider_installation_id.is_not(None),
                ),
            )
        )
    )


def _message(
    run: Run, pr: CodeChange, repository: Repository, installation: ProviderInstallation
) -> StoredRunMessage:
    return StoredRunMessage(
        run_id=run.id,
        workspace_id=installation.workspace_id,
        installation_id=installation.external_id,
        repository_id=repository.id,
        repository_external_id=repository.external_id,
        repository_full_name=repository.full_name,
        pr_number=pr.external_number,
        head_sha=run.head_sha,
        base_sha=run.base_sha,
        base_ref=run.base_ref or pr.target_branch,
        engine=run.engine.value,
        rule_version_id=run.rule_version_id,
        prompt_version_id=run.prompt_version_id,
        attempt=run.attempt + 1,
        requested_at=run.created_at,
        trigger=run.trigger,
        provider=installation.provider,
    )


def _hashed(finding: Finding) -> HashedFinding:
    return HashedFinding(
        path=finding.file_path,
        line_start=finding.line_start,
        line_end=finding.line_end,
        severity=finding.severity.value,
        category=finding.category.value,
        title=finding.title,
        body=finding.body,
        suggestion=finding.suggestion,
        inline=finding.inline_comment,
    )


class SqlAlchemyRunLifecycleStore:
    """Repositories flush only; the use case commits. NOTIFY rides the same transaction."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def _notify(
        self,
        run_id: UUID,
        status: RunState,
        *,
        workspace_id: UUID | None = None,
    ) -> None:
        if workspace_id is not None:
            await SqlAlchemyWebhookRunStore(self._session).notify_run_updated(
                run_id,
                workspace_id,
                status.value,
            )
        else:
            await notify_run_state(self._session, run_id, status)

    async def _published_findings(self, run_id: UUID) -> list[Finding]:
        return list(
            (
                await self._session.scalars(
                    select(Finding)
                    .where(Finding.run_id == run_id, Finding.drop_reason.is_(None))
                    .order_by(Finding.created_at.asc(), Finding.id.asc())
                )
            ).all()
        )

    async def lock_for_claim(self, run_id: UUID) -> RunGuardSnapshot | None:
        row = await lock_run_origin_row(
            self._session,
            run_id,
            _joined()
            .add_columns(Workspace.daily_budget_usd)
            .join(Workspace, Workspace.id == ProviderInstallation.workspace_id)
            .where(Run.id == run_id),
        )
        if row is None:
            return None
        run, pr, repository, installation, daily_budget = row
        now = datetime.now(UTC)
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        spent = await self._session.scalar(
            select(func.coalesce(func.sum(UsageEvent.cost_usd), 0)).where(
                UsageEvent.workspace_id == installation.workspace_id,
                UsageEvent.created_at >= day_start,
            )
        )
        return RunGuardSnapshot(
            run_id=run.id,
            workspace_id=installation.workspace_id,
            state=run.state.value,
            attempt=run.attempt,
            available_at=run.available_at,
            cancel_requested=run.cancel_requested,
            head_sha=run.head_sha,
            pr_head_sha=pr.head_sha,
            pr_open=pr.state == CodeChangeState.OPEN,
            repository_enabled=repository.enabled,
            origin_active=origin_active(run, repository, installation),
            daily_budget_usd=Decimal(daily_budget),
            spent_today_usd=Decimal(spent or 0),
            engine=run.engine.value,
            prompt_version_id=run.prompt_version_id,
            rule_version_id=run.rule_version_id,
        )

    async def claim(
        self, run_id: UUID, *, worker_id: str, now: datetime, lease_until: datetime
    ) -> int:
        attempt = await self._session.scalar(
            update(Run)
            .where(Run.id == run_id, Run.state == RunState.QUEUED)
            .values(
                state=RunState.RUNNING,
                attempt=Run.attempt + 1,
                lease_until=lease_until,
                worker_id=worker_id,
                started_at=func.coalesce(Run.started_at, now),
                error_code=None,
                error_message=None,
            )
            .returning(Run.attempt)
        )
        if attempt is None:
            raise RuntimeError(f"run {run_id} left queued under its row lock")
        await self._notify(run_id, RunState.RUNNING)
        return attempt

    def _owned(self, from_state: str, worker_id: str | None, now: datetime) -> ColumnElement[bool]:
        condition = Run.state == RunState(from_state)
        if worker_id is not None:
            return and_(condition, Run.worker_id == worker_id)
        if from_state == RunState.RUNNING:
            # Without an owner only the reconciler may move a running Run: its lease expired.
            return and_(condition, Run.lease_until < now)
        return condition

    async def finish(
        self,
        run_id: UUID,
        *,
        from_state: str,
        worker_id: str | None,
        state: str,
        error_code: str | None,
        error_message: str | None,
        now: datetime,
        expected_publication: PublishContext | None = None,
    ) -> bool:
        row = await lock_run_origin_row(
            self._session,
            run_id,
            _joined().where(Run.id == run_id).execution_options(populate_existing=True),
        )
        if row is None:
            return False
        publication_workspace_id: UUID | None = None
        if expected_publication is not None:
            if row[0].state != RunState.PUBLISHING:
                return False
            current = await self._publication_context(
                row[0],
                row[1],
                row[2],
                row[3],
                await self._published_findings(run_id),
            )
            if not publication_matches(expected_publication, current):
                return False
            assert current.publication_identity is not None
            publication_workspace_id = current.publication_identity.origin_workspace_id
            reason = cancellation_reason(
                pr_open=row[1].state == CodeChangeState.OPEN,
                head_current=row[0].head_sha == row[1].head_sha,
                cancel_requested=row[0].cancel_requested,
            )
            if reason is not None:
                state, error_code, error_message = "cancelled", reason, None
        if not origin_active(row[0], row[2], row[3]):
            state, error_code, error_message = (
                "cancelled",
                cancellation_reason(
                    pr_open=row[1].state == CodeChangeState.OPEN,
                    head_current=row[0].head_sha == row[1].head_sha,
                    cancel_requested=row[0].cancel_requested,
                )
                or "installation_unavailable"
                if expected_publication is not None
                else "installation_unavailable",
                None,
            )
        target = RunState(state)
        changed = await self._session.scalar(
            update(Run)
            .where(Run.id == run_id, self._owned(from_state, worker_id, now))
            .values(
                state=target,
                error_code=error_code,
                error_message=error_message,
                finished_at=now,
                lease_until=None,
            )
            .returning(Run.id)
        )
        if changed is None:
            return False
        await self._notify(run_id, target, workspace_id=publication_workspace_id)
        return True

    async def extend_lease(self, run_id: UUID, worker_id: str, lease_until: datetime) -> bool:
        changed = await self._session.scalar(
            update(Run)
            .where(
                Run.id == run_id,
                # T8 may commit publishing while the attempt is still returning.
                Run.state.in_([RunState.RUNNING, RunState.PUBLISHING]),
                Run.worker_id == worker_id,
            )
            .values(lease_until=lease_until)
            .returning(Run.id)
        )
        return changed is not None

    async def requeue(self, run_id: UUID, *, worker_id: str | None, available_at: datetime) -> bool:
        # Retirement/rebind must not turn an owned attempt into an orphaned queue row.
        row = await lock_run_origin_row(
            self._session,
            run_id,
            _joined().where(Run.id == run_id).execution_options(populate_existing=True),
        )
        if row is None:
            return False
        if not origin_active(row[0], row[2], row[3]):
            await self.finish(
                run_id,
                from_state="running",
                worker_id=worker_id,
                state="cancelled",
                error_code="installation_unavailable",
                error_message=None,
                now=datetime.now(UTC),
            )
            return False
        changed = await self._session.scalar(
            update(Run)
            .where(Run.id == run_id, self._owned("running", worker_id, available_at))
            .values(state=RunState.QUEUED, available_at=available_at, lease_until=None)
            .returning(Run.id)
        )
        if changed is None:
            return False
        await self._notify(run_id, RunState.QUEUED)
        return True

    async def provider_origin_active(self, run_id: UUID) -> bool:
        return (
            await self._session.scalar(
                _joined(provider=True).with_only_columns(Run.id).where(Run.id == run_id)
            )
            is not None
        )

    async def cancellation_reason(self, run_id: UUID) -> str | None:
        row = (await self._session.execute(_joined().where(Run.id == run_id))).one_or_none()
        if row is None:
            return None
        run, pr, repository, installation = row
        if not origin_active(run, repository, installation):
            return "installation_unavailable"
        return cancellation_reason(
            pr_open=pr.state == CodeChangeState.OPEN,
            head_current=run.head_sha == pr.head_sha,
            cancel_requested=run.cancel_requested,
        )

    async def run_message(self, run_id: UUID) -> PendingRunMessage | None:
        row = (await self._session.execute(_pointer_rows().where(Run.id == run_id))).one_or_none()
        return None if row is None else _message(*row)

    async def check_run_report(self, run_id: UUID) -> CheckRunReport | None:
        row = (
            await self._session.execute(
                _joined(provider=True).where(
                    Run.id == run_id, ProviderInstallation.provider == "github"
                )
            )
        ).one_or_none()
        if row is None:
            return None
        run, _, repository, installation = row
        target = CheckRunTarget(
            installation.external_id, repository.full_name, run.head_sha, run.id
        )
        if run.state != RunState.SUCCEEDED:
            return CheckRunReport(
                target,
                run.state.value,
                run.attempt,
                run.error_code,
                error_detail=policy_error_detail(run.error_message),
                provider=installation.provider,
            )
        findings = await self._published_findings(run_id)
        severities = [item.severity.value for item in findings]
        summary_only = await self._session.scalar(
            select(func.coalesce(func.bool_or(CodeChangeDiff.summary_only), False)).where(
                CodeChangeDiff.run_id == run_id
            )
        )
        context_summary = await self._session.scalar(
            select(ContextPayload.summary).where(ContextPayload.run_id == run_id)
        )
        below_threshold = await self._session.scalar(
            select(func.count())
            .select_from(Finding)
            .where(Finding.run_id == run_id, Finding.drop_reason == "severity_threshold")
        )
        result = (
            await load_review_results(
                self._session,
                {run_id: run.head_sha},
                {run_id: severities},
            )
        )[run_id]
        inline = sum(1 for item in findings if item.inline_comment)
        body_count = len(findings) - inline
        return CheckRunReport(
            target,
            run.state.value,
            run.attempt,
            verdict=None if summary_only else result.verdict,
            coverage_status=result.coverage.status.value,
            saved_summary=result.summary,
            severity_counts=severity_counts(severities),
            inline_count=inline,
            body_count=body_count,
            below_threshold_count=below_threshold or 0,
            summary_only=bool(summary_only),
            summary_reason=summary_only_reason_of(context_summary),
            provider=installation.provider,
        )

    async def _publish_pointer(self, run: Run, repository: Repository) -> ReviewPublishPointer:
        findings = await self._published_findings(run.id)
        result = (
            await load_review_results(
                self._session,
                {run.id: run.head_sha},
                {run.id: [item.severity.value for item in findings]},
            )
        )[run.id]
        return ReviewPublishPointer(
            run_id=run.id,
            head_sha=run.head_sha,
            findings_hash=findings_hash(_hashed(item) for item in findings),
            review_event=review_event(repository.review_event.value, result.verdict),
        )

    async def publish_context(self, run_id: UUID) -> PublishContext | None:
        row = (await self._session.execute(_joined().where(Run.id == run_id))).one_or_none()
        if row is None:
            return None
        run, pr, repository, installation = row
        findings = await self._published_findings(run_id)
        return await self._publication_context(run, pr, repository, installation, findings)

    async def _publication_context(
        self,
        run: Run,
        pr: CodeChange,
        repository: Repository,
        installation: ProviderInstallation,
        findings: list[Finding],
    ) -> PublishContext:
        active = origin_active(run, repository, installation)
        original = installation if installation.id == run.origin_provider_installation_id else None
        if original is None and run.origin_provider_installation_id is not None:
            # Same owned SQL session; the locked callers already hold this ORIGINAL
            # installation before Repository/Run. Never adopt a replacement tenant.
            original = await self._session.get(
                ProviderInstallation,
                run.origin_provider_installation_id,
                populate_existing=True,
            )
        identity = (
            PublicationIdentity(
                original.id,
                original.workspace_id,
                original.external_id,
                original.provider,
                repository.id,
                pr.id,
                run.base_sha,
            )
            if original is not None
            else None
        )
        provider = installation.provider
        diff_map = (
            await load_publication_diff_map(self._session, run.id) if provider == "gitlab" else None
        )
        return PublishContext(
            run_id=run.id,
            state=run.state.value,
            cancel_requested=run.cancel_requested,
            head_sha=run.head_sha,
            pr_head_sha=pr.head_sha,
            pr_open=pr.state == CodeChangeState.OPEN,
            origin_active=origin_active(run, repository, installation),
            installation_id=installation.external_id
            if origin_active(run, repository, installation)
            else None,
            repository_full_name=repository.full_name,
            pr_number=pr.external_number,
            review_body=run.review_body or "",
            findings=tuple(
                published_finding_from_row(item, diff_map, provider=provider)
                for item in findings
                if item.inline_comment
            ),
            findings_hash=findings_hash(_hashed(item) for item in findings),
            provider=installation.provider,
            project_id=repository.external_id,
            workspace_id=installation.workspace_id if active else None,
            publication_identity=identity,
            vcs_input=RunVcsInput(
                pr.id,
                repository.id,
                run.head_sha,
                run.base_sha,
                PullRequestLocator(
                    installation.external_id,
                    repository.full_name,
                    pr.external_number,
                    provider=installation.provider,
                    origin_provider_installation_id=run.origin_provider_installation_id,
                    repository_external_id=repository.external_id,
                ),
            )
            if active
            else None,
        )

    async def complete_publication(
        self,
        run_id: UUID,
        *,
        review_id: int,
        comment_ids: tuple[int, ...],
        findings_hash: str,
        moved_to_body_finding_ids: tuple[UUID, ...] = (),
        now: datetime,
        expected_publication: PublishContext | None = None,
    ) -> str | None:
        """T14 under the Run lock; a cancel or push seen during the POST wins (T15).

        An active origin records the submitted findings and comment ids. A retired
        or replaced origin cancels without altering those historical payloads.
        """
        row = await lock_run_origin_row(
            self._session,
            run_id,
            _joined().where(Run.id == run_id).execution_options(populate_existing=True),
        )
        if row is None or row[0].state != RunState.PUBLISHING:
            return None
        run, pr, repository, installation = row
        findings = await self._published_findings(run_id)
        publication_workspace_id: UUID | None = None
        if expected_publication is not None:
            current = await self._publication_context(run, pr, repository, installation, findings)
            if not publication_matches(expected_publication, current):
                return None
            assert current.publication_identity is not None
            publication_workspace_id = current.publication_identity.origin_workspace_id
        if not origin_active(run, repository, installation):
            run.state = RunState.CANCELLED
            run.error_code = (
                cancellation_reason(
                    pr_open=pr.state == CodeChangeState.OPEN,
                    head_current=run.head_sha == pr.head_sha,
                    cancel_requested=run.cancel_requested,
                )
                or "installation_unavailable"
                if expected_publication is not None
                else "installation_unavailable"
            )
            run.finished_at = now
            run.lease_until = None
            await self._session.flush()
            await self._notify(run_id, RunState.CANCELLED, workspace_id=publication_workspace_id)
            return RunState.CANCELLED.value
        pr_head_sha, pr_state = pr.head_sha, pr.state
        inline = [item for item in findings if item.inline_comment]
        if moved_to_body_finding_ids:
            moved = frozenset(moved_to_body_finding_ids)
            for item in inline:
                if item.id in moved:
                    item.inline_comment = False
        elif len(comment_ids) == len(inline):
            # GitHub returns review comments in submission order.
            for finding, comment_id in zip(inline, comment_ids, strict=True):
                await self._session.execute(
                    insert(Comment)
                    .values(
                        finding_id=finding.id,
                        github_review_id=review_id,
                        github_comment_id=comment_id,
                        findings_hash=findings_hash,
                    )
                    .on_conflict_do_nothing()
                )
        for item in findings:
            item.published = True
        reason = cancellation_reason(
            pr_open=pr_state == CodeChangeState.OPEN,
            head_current=run.head_sha == pr_head_sha,
            cancel_requested=run.cancel_requested,
        )
        final = RunState.SUCCEEDED if reason is None else RunState.CANCELLED
        run.state = final
        run.error_code = reason
        run.finished_at = now
        run.lease_until = None
        await self._session.flush()
        await self._notify(run_id, final, workspace_id=publication_workspace_id)
        return final.value

    @staticmethod
    def _stale(now: datetime, available_before: datetime) -> ColumnElement[bool]:
        return or_(
            and_(Run.state == RunState.QUEUED, Run.available_at < available_before),
            and_(Run.state.in_([RunState.RUNNING, RunState.PUBLISHING]), Run.lease_until < now),
        )

    async def stale_inactive_origins(
        self, now: datetime, available_before: datetime, limit: int
    ) -> tuple[UUID, ...]:
        """Discover retired work without ever projecting a replacement provider pointer."""
        inactive = or_(
            Run.origin_provider_installation_id.is_(None),
            Run.origin_provider_installation_id != Repository.provider_installation_id,
            ProviderInstallation.deleted_at.is_not(None),
            Repository.installation_available.is_(False),
        )
        candidates = await self._session.scalars(
            _joined()
            .with_only_columns(Run.id)
            .where(self._stale(now, available_before), inactive)
            .order_by(Run.id)
            .limit(limit)
        )
        return tuple(candidates.all())

    async def retire_stale_origin(
        self, run_id: UUID, now: datetime, available_before: datetime
    ) -> bool:
        # One candidate per transaction: never acquire another origin after a Run,
        # since OAuth locks every affected installation before Repository/Run rows.
        row = await lock_run_origin_row(
            self._session,
            run_id,
            _joined()
            .where(Run.id == run_id, self._stale(now, available_before))
            .execution_options(populate_existing=True),
        )
        if row is None or origin_active(row[0], row[2], row[3]):
            return False
        return await self.finish(
            run_id,
            from_state=row[0].state.value,
            worker_id=None,
            state="cancelled",
            error_code="installation_unavailable",
            error_message=None,
            now=now,
        )

    async def expired_running(self, now: datetime, limit: int) -> tuple[tuple[UUID, int], ...]:
        rows = await self._session.execute(
            select(Run.id, Run.attempt)
            .where(Run.state == RunState.RUNNING, Run.lease_until < now)
            .order_by(Run.lease_until)
            .limit(limit)
        )
        return tuple((run_id, attempt) for run_id, attempt in rows)

    async def expired_publishing(
        self, now: datetime, limit: int
    ) -> tuple[ReviewPublishPointer, ...]:
        rows = (
            await self._session.execute(
                _pointer_rows()
                .where(Run.state == RunState.PUBLISHING, Run.lease_until < now)
                .order_by(Run.lease_until)
                .limit(limit)
            )
        ).all()
        return tuple(
            [await self._publish_pointer(run, repository) for run, _, repository, _ in rows]
        )

    async def stale_queued(
        self, available_before: datetime, limit: int
    ) -> tuple[PendingRunMessage, ...]:
        rows = (
            await self._session.execute(
                _pointer_rows()
                .where(
                    Run.state == RunState.QUEUED,
                    Run.available_at < available_before,
                    # review.run.deep has no consumer until phase 3 (SD §13).
                    Run.engine == Engine.FAST,
                )
                .order_by(Run.available_at)
                .limit(limit)
            )
        ).all()
        return tuple(_message(*row) for row in rows)


class SqlAlchemyRunLifecycleUnitOfWork(SqlAlchemyUnitOfWork):
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        super().__init__(session_factory)

    @property
    def runs(self) -> SqlAlchemyRunLifecycleStore:
        return SqlAlchemyRunLifecycleStore(self.session)


class SqlAlchemyRunTraceStore:
    """``run_actions`` records and ``context_payloads``; flushes, the use case commits."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add_action(
        self,
        run_id: UUID,
        tool: str,
        request: dict[str, Any],
        response: Any,
        started_at: datetime,
        duration_ms: int,
    ) -> None:
        await insert_run_action(
            self._session,
            run_id=run_id,
            tool=tool,
            request=request,
            response=response,
            started_at=started_at,
            duration_ms=duration_ms,
        )

    async def save_context_payload(
        self, run_id: UUID, summary: dict[str, Any], *, mark_summary_only: bool = False
    ) -> None:
        # Context is durable review input, not an append-only provider audit.
        row = await lock_run_origin_row(
            self._session,
            run_id,
            _joined().where(Run.id == run_id).execution_options(populate_existing=True),
        )
        if row is None or not origin_active(row[0], row[2], row[3]):
            raise RunCancelled("installation_unavailable")
        if row[0].state != RunState.RUNNING:
            raise RunCancelled("run_inactive")
        # Schema 2: the response of the stage's context.build (#168 part 6); schema 1 rows
        # hold the summary of the removed review prompt read. A retry rewrites both.
        await self._session.execute(
            insert(ContextPayload)
            .values(run_id=run_id, schema_version=2, summary=summary, s3_ref=None)
            .on_conflict_do_update(
                index_elements=[ContextPayload.run_id],
                set_={"summary": summary, "schema_version": 2},
            )
        )
        # Same short transaction as the context.build payload. The UI patch stays.
        # Only the stage's summary-only stop sets the flag: a legacy technical-only
        # build can also record status summary_only and must keep the model path.
        if mark_summary_only:
            await self._session.execute(
                update(CodeChangeDiff)
                .where(CodeChangeDiff.run_id == run_id)
                .values(summary_only=True, review_patch=None)
            )


class SqlAlchemyRunTraceUnitOfWork(SqlAlchemyUnitOfWork):
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        super().__init__(session_factory)

    @property
    def trace(self) -> SqlAlchemyRunTraceStore:
        return SqlAlchemyRunTraceStore(self.session)
