"""PostgreSQL state transitions of the worker, publisher and reconciler (T4-T18)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import ColumnElement, and_, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.common.infrastructure.db.enums import CodeChangeState, RunState
from app.common.infrastructure.db.unit_of_work import SqlAlchemyUnitOfWork
from app.modules.analytics.infrastructure.models import UsageEvent
from app.modules.repositories.infrastructure.models import ProviderInstallation, Repository
from app.modules.reviews.application.check_runs import CheckRunReport, CheckRunTarget
from app.modules.reviews.application.handle_review_run import RunGuardSnapshot
from app.modules.reviews.application.publish_run_review import PublishContext
from app.modules.reviews.application.queue_messages import ReviewPublishPointer, StoredRunMessage
from app.modules.reviews.application.run_failures import cancellation_reason
from app.modules.reviews.application.try_enqueue_webhook_run import PendingRunMessage
from app.modules.reviews.application.verdict import (
    HashedFinding,
    findings_hash,
    review_event,
    severity_counts,
    verdict,
)
from app.modules.reviews.infrastructure.models import (
    CodeChange,
    CodeChangeDiff,
    Comment,
    ContextPayload,
    Finding,
    Run,
    RunAction,
)
from app.modules.reviews.infrastructure.run_action_payloads import place_response
from app.modules.reviews.infrastructure.run_notifications import notify_run_state
from app.modules.reviews.infrastructure.run_repository import _to_published_finding_row
from app.modules.workspaces.infrastructure.models import Workspace

_ROW = (Run, CodeChange, Repository, ProviderInstallation)


def _joined() -> Any:
    return (
        select(*_ROW)
        .join(CodeChange, Run.code_change_id == CodeChange.id)
        .join(Repository, CodeChange.repository_id == Repository.id)
        .join(ProviderInstallation, Repository.provider_installation_id == ProviderInstallation.id)
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

    async def _notify(self, run_id: UUID, status: RunState) -> None:
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
        row = (
            await self._session.execute(
                _joined()
                .add_columns(Workspace.daily_budget_usd)
                .join(Workspace, Workspace.id == ProviderInstallation.workspace_id)
                .where(Run.id == run_id)
                .with_for_update(of=Run)
            )
        ).one_or_none()
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
            daily_budget_usd=Decimal(daily_budget),
            spent_today_usd=Decimal(spent or 0),
            engine=run.engine.value,
            prompt_version_id=run.prompt_version_id,
            rule_version_id=run.rule_version_id,
            check_run=CheckRunTarget(
                installation.external_id, repository.full_name, run.head_sha, run.id
            ),
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
    ) -> bool:
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
        await self._notify(run_id, target)
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

    async def cancellation_reason(self, run_id: UUID) -> str | None:
        row = (
            await self._session.execute(
                select(Run.cancel_requested, Run.head_sha, CodeChange.head_sha, CodeChange.state)
                .join(CodeChange, Run.code_change_id == CodeChange.id)
                .where(Run.id == run_id)
            )
        ).one_or_none()
        if row is None:
            return None
        cancel_requested, head_sha, pr_head_sha, pr_state = row
        return cancellation_reason(
            pr_open=pr_state == CodeChangeState.OPEN,
            head_current=head_sha == pr_head_sha,
            cancel_requested=cancel_requested,
        )

    async def run_message(self, run_id: UUID) -> PendingRunMessage | None:
        row = (await self._session.execute(_joined().where(Run.id == run_id))).one_or_none()
        return None if row is None else _message(*row)

    async def check_run_report(self, run_id: UUID) -> CheckRunReport | None:
        row = (await self._session.execute(_joined().where(Run.id == run_id))).one_or_none()
        if row is None:
            return None
        run, _, repository, installation = row
        target = CheckRunTarget(
            installation.external_id, repository.full_name, run.head_sha, run.id
        )
        if run.state != RunState.SUCCEEDED:
            return CheckRunReport(target, run.state.value, run.attempt, run.error_code)
        findings = await self._published_findings(run_id)
        severities = [item.severity.value for item in findings]
        summary_only = await self._session.scalar(
            select(func.coalesce(func.bool_or(CodeChangeDiff.summary_only), False)).where(
                CodeChangeDiff.run_id == run_id
            )
        )
        inline = sum(1 for item in findings if item.inline_comment)
        return CheckRunReport(
            target,
            run.state.value,
            run.attempt,
            verdict=verdict(severities),
            severity_counts=severity_counts(severities),
            inline_count=inline,
            body_count=len(findings) - inline,
            summary_only=bool(summary_only),
        )

    async def _publish_pointer(self, run: Run, repository: Repository) -> ReviewPublishPointer:
        findings = await self._published_findings(run.id)
        run_verdict = verdict(item.severity.value for item in findings)
        return ReviewPublishPointer(
            run_id=run.id,
            head_sha=run.head_sha,
            findings_hash=findings_hash(_hashed(item) for item in findings),
            review_event=review_event(repository.review_event.value, run_verdict),
        )

    async def publish_context(self, run_id: UUID) -> PublishContext | None:
        row = (await self._session.execute(_joined().where(Run.id == run_id))).one_or_none()
        if row is None:
            return None
        run, pr, repository, installation = row
        findings = await self._published_findings(run_id)
        return PublishContext(
            run_id=run.id,
            state=run.state.value,
            cancel_requested=run.cancel_requested,
            head_sha=run.head_sha,
            pr_head_sha=pr.head_sha,
            pr_open=pr.state == CodeChangeState.OPEN,
            installation_id=installation.external_id,
            repository_full_name=repository.full_name,
            pr_number=pr.external_number,
            review_body=run.review_body or "",
            findings=tuple(
                _to_published_finding_row(item) for item in findings if item.inline_comment
            ),
            findings_hash=findings_hash(_hashed(item) for item in findings),
        )

    async def complete_publication(
        self,
        run_id: UUID,
        *,
        review_id: int,
        comment_ids: tuple[int, ...],
        findings_hash: str,
        moved_to_body: bool,
        now: datetime,
    ) -> str | None:
        """T14 under the Run lock; a cancel or push seen during the POST wins (T15).

        The review is already on GitHub either way, so findings and comment ids are
        recorded; only the final state differs.
        """
        row = (
            await self._session.execute(
                select(Run, CodeChange.head_sha, CodeChange.state)
                .join(CodeChange, Run.code_change_id == CodeChange.id)
                .where(Run.id == run_id)
                .with_for_update(of=Run)
            )
        ).one_or_none()
        if row is None or row[0].state != RunState.PUBLISHING:
            return None
        run, pr_head_sha, pr_state = row
        findings = await self._published_findings(run_id)
        inline = [item for item in findings if item.inline_comment]
        if moved_to_body:
            for item in inline:
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
        await self._notify(run_id, final)
        return final.value

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
                _joined()
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
                _joined()
                .where(Run.state == RunState.QUEUED, Run.available_at < available_before)
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
        # The Run row lock serializes the next action index.
        await self._session.execute(select(Run.id).where(Run.id == run_id).with_for_update())
        index = await self._session.scalar(
            select(func.coalesce(func.max(RunAction.index), -1)).where(RunAction.run_id == run_id)
        )
        assert index is not None
        stored, response_ref = await place_response(self._session, run_id, response)
        self._session.add(
            RunAction(
                run_id=run_id,
                index=index + 1,
                tool=tool,
                request=request,
                response=stored,
                response_ref=response_ref,
                started_at=started_at,
                duration_ms=duration_ms,
            )
        )
        await self._session.flush()

    async def save_context_payload(self, run_id: UUID, summary: dict[str, Any]) -> None:
        await self._session.execute(
            insert(ContextPayload)
            .values(run_id=run_id, schema_version=1, summary=summary, s3_ref=None)
            .on_conflict_do_update(
                index_elements=[ContextPayload.run_id], set_={"summary": summary}
            )
        )


class SqlAlchemyRunTraceUnitOfWork(SqlAlchemyUnitOfWork):
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        super().__init__(session_factory)

    @property
    def trace(self) -> SqlAlchemyRunTraceStore:
        return SqlAlchemyRunTraceStore(self.session)
