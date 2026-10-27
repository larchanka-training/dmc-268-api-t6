"""SQLAlchemy read projection for review runs."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import ColumnElement, String, and_, case, cast, func, or_, select, true, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import aliased

from app.common.infrastructure.db.enums import RunState
from app.common.infrastructure.db.unit_of_work import SqlAlchemyUnitOfWork
from app.modules.analytics.infrastructure.models import UsageEvent
from app.modules.auth.application.scope import AuthScope
from app.modules.repositories.infrastructure.models import (
    ProviderInstallation,
    Repository,
    RuleVersion,
)
from app.modules.reviews.application.cancel_run import CancelRequestResult
from app.modules.reviews.application.conventions import ActiveConventionsPrompt
from app.modules.reviews.application.diff_map import DiffMap, vcs_provider_name
from app.modules.reviews.application.findings_post_processor import (
    ProcessedFinding,
    ProcessedReviewOutput,
)
from app.modules.reviews.application.get_run import FindingView, RunReview
from app.modules.reviews.application.get_run_actions import RunAction as RunActionProjection
from app.modules.reviews.application.get_run_actions import RunActionResponse
from app.modules.reviews.application.get_run_comments import PublishedComment
from app.modules.reviews.application.get_run_diff import DiffSnapshot
from app.modules.reviews.application.get_run_file_lines import BlobCacheKey
from app.modules.reviews.application.list_runs import RunCursor, RunListItem
from app.modules.reviews.application.post_processing_diff_map import diff_map_from_snapshots
from app.modules.reviews.application.process_run import (
    RunConventionsInput,
    RunDiffInput,
    RunVcsInput,
)
from app.modules.reviews.application.prompt_builder import review_rule_from_stored
from app.modules.reviews.application.review_output import (
    FindingPostProcessingInput,
    PublishedFinding,
    ReviewOutput,
    ReviewPublication,
)
from app.modules.reviews.application.review_path import ReviewBinding
from app.modules.reviews.application.run_coverage import (
    read_context_coverage_pins,
    restore_context_coverage,
)
from app.modules.reviews.application.run_events import RunChange
from app.modules.reviews.application.run_failures import RunCancelled
from app.modules.reviews.application.store_review_output import PublishingGuard
from app.modules.reviews.application.vcs_diff import PullRequestLocator
from app.modules.reviews.infrastructure.models import (
    CodeChange,
    CodeChangeDiff,
    Finding,
    PromptVersion,
    Run,
    RunAction,
    RunActionResponseBody,
)
from app.modules.reviews.infrastructure.run_action_records import insert_run_action
from app.modules.reviews.infrastructure.run_notifications import notify_run_state
from app.modules.reviews.infrastructure.run_origin import active_origin_condition, origin_active
from app.modules.reviews.infrastructure.run_origin_locks import lock_run_origin_row
from app.modules.reviews.infrastructure.saved_review_results import load_review_results
from app.modules.reviews.infrastructure.severity_threshold import severity_threshold_value
from app.modules.workspaces.infrastructure.repository_access import repository_access_predicate


def authorized_run(scope: AuthScope | None, *, allow_unscoped: bool = False) -> ColumnElement[bool]:
    """A Run is visible when the claim and grants give access to its repository."""
    if scope is None:
        if not allow_unscoped:
            raise ValueError("Run authorization requires an AuthScope unless allow_unscoped=True")
        return true()
    return (
        select(1)
        .select_from(CodeChange)
        .join(Repository, Repository.id == CodeChange.repository_id)
        .where(CodeChange.id == Run.code_change_id, repository_access_predicate(scope))
        .correlate(Run)
        .exists()
    )


class SqlAlchemyRunRepository:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        scope: AuthScope | None = None,
        *,
        allow_unscoped: bool = False,
        session: AsyncSession | None = None,
    ) -> None:
        if scope is None and not allow_unscoped:
            raise ValueError(
                "SqlAlchemyRunRepository requires an AuthScope unless allow_unscoped=True"
            )
        self._session_factory = session_factory
        self._session = session
        self._scope = scope
        self._allow_unscoped = allow_unscoped

    @asynccontextmanager
    async def _session_scope(self) -> AsyncIterator[AsyncSession]:
        if self._session is not None:
            yield self._session
        else:
            assert self._session_factory is not None
            async with self._session_factory() as session:
                yield session

    def _authorized_run(self) -> ColumnElement[bool]:
        return authorized_run(self._scope, allow_unscoped=self._allow_unscoped)

    async def run_updated_at(self, run_id: UUID) -> datetime | None:
        """One scoped query: `updated_at` of a visible Run (its SSE event id), else `None`."""
        statement = select(Run.updated_at).where(Run.id == run_id, self._authorized_run())
        async with self._session_scope() as session:
            updated_at: datetime | None = await session.scalar(statement)
        return updated_at

    async def runs_updated_after(self, after: datetime, limit: int) -> list[RunChange]:
        """Visible Runs changed after `after`, oldest first; the newest `limit` on overflow."""
        statement = (
            select(Run.id, Run.state, Run.updated_at)
            .where(Run.updated_at > after, self._authorized_run())
            .order_by(Run.updated_at.desc(), Run.id.desc())
            .limit(limit)
        )
        async with self._session_scope() as session:
            rows = (await session.execute(statement)).all()
        return [
            RunChange(run_id, state.value, updated_at)
            for run_id, state, updated_at in reversed(rows)
        ]

    async def list_runs(
        self,
        *,
        status: str | None,
        repository: str | None,
        cursor: RunCursor | None,
        limit: int,
    ) -> list[RunListItem]:
        latest_model = (
            select(UsageEvent.model)
            .where(UsageEvent.run_id == Run.id)
            .order_by(UsageEvent.created_at.desc(), UsageEvent.id.desc())
            .limit(1)
            .scalar_subquery()
        )
        action_count = (
            select(func.count())
            .select_from(RunAction)
            .where(RunAction.run_id == Run.id)
            .scalar_subquery()
        )
        summary_only = self._summary_only_projection()
        statement = (
            select(Run, CodeChange, Repository.full_name, latest_model, action_count, summary_only)
            .join(CodeChange, Run.code_change_id == CodeChange.id)
            .join(Repository, CodeChange.repository_id == Repository.id)
            .where(self._authorized_run())
            .order_by(Run.created_at.desc(), Run.id.desc())
            .limit(limit)
        )
        if status is not None:
            statement = statement.where(Run.state == status)
        if repository is not None:
            statement = statement.where(Repository.full_name == repository)
        if cursor is not None:
            statement = statement.where(
                or_(
                    Run.created_at < cursor.created_at,
                    and_(Run.created_at == cursor.created_at, Run.id < cursor.id),
                )
            )

        async with self._session_scope() as session:
            rows = (await session.execute(statement)).all()
        return [self._to_run_list_item(*row) for row in rows]

    async def get_run(self, run_id: UUID) -> RunListItem | None:
        latest_model = (
            select(UsageEvent.model)
            .where(UsageEvent.run_id == Run.id)
            .order_by(UsageEvent.created_at.desc(), UsageEvent.id.desc())
            .limit(1)
            .scalar_subquery()
        )
        action_count = (
            select(func.count())
            .select_from(RunAction)
            .where(RunAction.run_id == Run.id)
            .scalar_subquery()
        )
        summary_only = self._summary_only_projection()
        statement = (
            select(Run, CodeChange, Repository.full_name, latest_model, action_count, summary_only)
            .join(CodeChange, Run.code_change_id == CodeChange.id)
            .join(Repository, CodeChange.repository_id == Repository.id)
            .where(Run.id == run_id)
            .where(self._authorized_run())
        )
        async with self._session_scope() as session:
            row = (await session.execute(statement)).one_or_none()
        return self._to_run_list_item(*row) if row is not None else None

    async def get_run_review(self, run_id: UUID) -> RunReview | None:
        async with self._session_scope() as session:
            row = (
                await session.execute(
                    select(CodeChange, Run.head_sha, Run.state)
                    .join(Run, Run.code_change_id == CodeChange.id)
                    .where(Run.id == run_id, self._authorized_run())
                )
            ).one_or_none()
            if row is None:
                return None
            pr, head_sha, state = row
            findings = (
                await session.scalars(
                    select(Finding)
                    .where(
                        Finding.run_id == run_id,
                        Finding.published.is_(True),
                        Finding.drop_reason.is_(None),
                    )
                    .order_by(Finding.created_at.asc(), Finding.id.asc())
                )
            ).all()
            result = None
            if state is RunState.SUCCEEDED:
                result = (
                    await load_review_results(
                        session,
                        {run_id: head_sha},
                        {run_id: [item.severity.value for item in findings]},
                    )
                )[run_id]
            usage = (
                await session.execute(
                    select(
                        func.count(UsageEvent.id),
                        func.coalesce(func.sum(UsageEvent.tokens_in), 0),
                        func.coalesce(func.sum(UsageEvent.tokens_out), 0),
                        func.coalesce(func.sum(UsageEvent.cost_usd), 0),
                    ).where(UsageEvent.run_id == run_id)
                )
            ).one()
        return RunReview(
            author=pr.author_login,
            head_ref=pr.source_branch,
            base_ref=pr.target_branch,
            findings=[
                FindingView(
                    comment=self._to_published_comment(item),
                    side=item.side.value,
                    suggestion=item.suggestion,
                    confidence=float(item.confidence),
                )
                for item in findings
            ],
            summary=result.summary if result is not None else None,
            saved_result=result,
            usage_calls=int(usage[0]),
            tokens_in=int(usage[1]),
            tokens_out=int(usage[2]),
            cost_usd=Decimal(usage[3]),
        )

    async def get_published_comments(self, run_id: UUID) -> list[PublishedComment] | None:
        statement = (
            select(Run.id, Finding)
            .outerjoin(
                Finding,
                and_(Finding.run_id == Run.id, Finding.published.is_(True)),
            )
            .where(Run.id == run_id)
            .where(self._authorized_run())
            .order_by(Finding.created_at.asc(), Finding.id.asc())
        )
        async with self._session_scope() as session:
            rows = (await session.execute(statement)).all()
        if not rows:
            return None
        return [self._to_published_comment(finding) for _, finding in rows if finding is not None]

    async def get_run_actions(self, run_id: UUID) -> list[RunActionProjection] | None:
        statement = (
            select(Run.id, RunAction)
            .outerjoin(RunAction, RunAction.run_id == Run.id)
            .where(Run.id == run_id)
            .where(self._authorized_run())
            .order_by(RunAction.index.asc())
        )
        async with self._session_scope() as session:
            rows = (await session.execute(statement)).all()
        if not rows:
            return None
        return [self._to_run_action(action) for _, action in rows if action is not None]

    async def get_run_action_response(self, run_id: UUID, index: int) -> RunActionResponse | None:
        statement = (
            select(RunAction.response, RunActionResponseBody.body)
            .join(Run, RunAction.run_id == Run.id)
            .outerjoin(
                RunActionResponseBody,
                and_(
                    RunActionResponseBody.run_id == RunAction.run_id,
                    cast(RunActionResponseBody.id, String) == RunAction.response_ref,
                ),
            )
            .where(Run.id == run_id, RunAction.index == index, self._authorized_run())
        )
        async with self._session_scope() as session:
            row = (await session.execute(statement)).one_or_none()
        if row is None:
            return None
        inline, stored = row
        return RunActionResponse(response=stored if stored is not None else inline)

    async def get_run_diff(self, run_id: UUID) -> list[DiffSnapshot] | None:
        statement = (
            select(Run.id, CodeChangeDiff)
            .outerjoin(
                CodeChangeDiff,
                CodeChangeDiff.run_id == Run.id,
            )
            .where(Run.id == run_id)
            .where(self._authorized_run())
            .order_by(CodeChangeDiff.filename.asc())
        )
        async with self._session_scope() as session:
            rows = (await session.execute(statement)).all()
        if not rows:
            return None
        return [self._to_diff_snapshot(snapshot) for _, snapshot in rows if snapshot is not None]

    async def get_run_snapshots(self, run_id: UUID) -> list[DiffSnapshot] | None:
        statement = (
            select(Run.diff_snapshotted_at, CodeChangeDiff)
            .outerjoin(CodeChangeDiff, CodeChangeDiff.run_id == Run.id)
            .where(Run.id == run_id)
            .where(self._authorized_run())
            .order_by(CodeChangeDiff.filename.asc())
        )
        async with self._session_scope() as session:
            rows = (await session.execute(statement)).all()
        if not rows or rows[0][0] is None:
            return None
        return [self._to_diff_snapshot(snapshot) for _, snapshot in rows if snapshot is not None]

    async def store_diff_snapshots(
        self,
        run_id: UUID,
        code_change_id: UUID,
        head_sha: str,
        snapshots: list[DiffSnapshot],
    ) -> list[DiffSnapshot]:
        if self._session is None:
            raise RuntimeError(
                "store_diff_snapshots() requires an active session within a Unit of Work"
            )
        session = self._session
        # Rebind takes source installation → Repository → Run. Protect the final
        # snapshot write with the same order after detached provider requests.
        row = await lock_run_origin_row(
            session,
            run_id,
            select(Run, Repository, ProviderInstallation)
            .join(CodeChange, CodeChange.id == Run.code_change_id)
            .join(Repository, Repository.id == CodeChange.repository_id)
            .outerjoin(
                ProviderInstallation,
                ProviderInstallation.id == Run.origin_provider_installation_id,
            )
            .where(Run.id == run_id)
            .execution_options(populate_existing=True),
        )
        if row is None or row[0].code_change_id != code_change_id or row[0].head_sha != head_sha:
            raise ValueError("run revision changed before diff snapshot storage")
        run, repository, installation = row
        if installation is None or not origin_active(run, repository, installation):
            raise RunCancelled("installation_unavailable")
        if run.state != RunState.RUNNING:
            raise RunCancelled("run_inactive")
        if run.diff_snapshotted_at is not None:
            rows = (
                await session.scalars(
                    select(CodeChangeDiff)
                    .where(CodeChangeDiff.run_id == run_id)
                    .order_by(CodeChangeDiff.filename.asc())
                )
            ).all()
            return [self._to_diff_snapshot(row) for row in rows]
        session.add_all(
            [
                CodeChangeDiff(
                    run_id=run_id,
                    code_change_id=code_change_id,
                    head_sha=head_sha,
                    filename=snapshot.filename,
                    patch=snapshot.patch,
                    review_patch=snapshot.review_patch,
                    blob_sha=snapshot.blob_sha,
                    status=snapshot.status,
                    previous_filename=snapshot.previous_filename,
                    additions=snapshot.additions,
                    deletions=snapshot.deletions,
                    changes=snapshot.changes,
                    omission_reason=snapshot.omission_reason,
                    summary_only=snapshot.summary_only,
                )
                for snapshot in snapshots
            ]
        )
        run.diff_snapshotted_at = datetime.now(UTC)
        await session.flush()
        return snapshots

    async def get_run_diff_input(self, run_id: UUID) -> RunDiffInput | None:
        statement = select(Run.code_change_id, Run.head_sha).where(Run.id == run_id)
        async with self._session_scope() as session:
            row = (await session.execute(statement)).one_or_none()
        if row is None:
            return None
        return RunDiffInput(code_change_id=row[0], head_sha=row[1])

    async def get_run_vcs_input(self, run_id: UUID) -> RunVcsInput | None:
        statement = (
            select(
                Run.code_change_id,
                CodeChange.repository_id,
                Run.head_sha,
                Run.base_sha,
                ProviderInstallation.external_id,
                Repository.full_name,
                CodeChange.external_number,
                ProviderInstallation.provider,
                Run.origin_provider_installation_id,
                Repository.external_id,
            )
            .select_from(Run)
            .join(CodeChange, CodeChange.id == Run.code_change_id)
            .join(Repository, Repository.id == CodeChange.repository_id)
            .join(
                ProviderInstallation, ProviderInstallation.id == Run.origin_provider_installation_id
            )
            .where(Run.id == run_id, active_origin_condition())
        )
        async with self._session_scope() as session:
            row = (await session.execute(statement)).one_or_none()
        if row is None:
            return None
        return RunVcsInput(
            row[0],
            row[1],
            row[2],
            row[3],
            PullRequestLocator(
                row[4],
                row[5],
                row[6],
                provider=row[7],
                origin_provider_installation_id=row[8],
                repository_external_id=row[9],
            ),
        )

    async def get_run_conventions_input(self, run_id: UUID) -> RunConventionsInput | None:
        # The review and conventions prompts the run pinned, beside the active conventions row.
        review_prompt = aliased(PromptVersion)
        pinned_conventions = aliased(PromptVersion)
        statement = (
            select(
                CodeChange.repository_id,
                PromptVersion.id,
                PromptVersion.content,
                RuleVersion.rules,
                Run.prompt_version_id,
                review_prompt.key,
                review_prompt.version,
                ProviderInstallation.provider,
                pinned_conventions.id,
                pinned_conventions.content,
            )
            .select_from(Run)
            .join(CodeChange, CodeChange.id == Run.code_change_id)
            .join(RuleVersion, RuleVersion.id == Run.rule_version_id)
            .join(review_prompt, review_prompt.id == Run.prompt_version_id)
            .outerjoin(
                pinned_conventions, pinned_conventions.id == Run.conventions_prompt_version_id
            )
            .outerjoin(
                ProviderInstallation, ProviderInstallation.id == Run.origin_provider_installation_id
            )
            .outerjoin(
                PromptVersion,
                and_(
                    PromptVersion.key == "review.conventions",
                    PromptVersion.is_active.is_(True),
                ),
            )
            .where(Run.id == run_id)
        )
        async with self._session_scope() as session:
            row = (await session.execute(statement)).one_or_none()
        if row is None:
            return None
        if row[1] is None or row[2] is None:
            # Also for a snapshot-path (v4) run, which generates with its pinned row: this one
            # read serves both paths before the policy load, and ``conventions_prompt`` is the
            # required legacy field. The seed keeps one active row per key, so only a broken
            # catalog fails here, with ``internal_error`` before any provider call.
            raise LookupError("active review.conventions prompt is missing")
        return RunConventionsInput(
            repository_id=row[0],
            conventions_prompt=ActiveConventionsPrompt(id=row[1], content=row[2]),
            rules=tuple(review_rule_from_stored(item) for item in row[3]),
            review_binding=ReviewBinding(row[4], row[5], f"v{row[6]}", row[7]),
            pinned_conventions_prompt=(
                None if row[8] is None else ActiveConventionsPrompt(id=row[8], content=row[9])
            ),
        )

    async def get_run_file_key(self, run_id: UUID, path: str) -> BlobCacheKey | None:
        statement = (
            select(CodeChange.repository_id, CodeChangeDiff.blob_sha)
            .select_from(Run)
            .join(CodeChange, CodeChange.id == Run.code_change_id)
            .join(
                CodeChangeDiff,
                and_(
                    CodeChangeDiff.run_id == Run.id,
                    CodeChangeDiff.filename == path,
                ),
            )
            .where(Run.id == run_id, CodeChangeDiff.blob_sha.is_not(None), self._authorized_run())
        )
        async with self._session_scope() as session:
            row = (await session.execute(statement)).one_or_none()
        if row is None:
            return None
        return BlobCacheKey(repository_id=row[0], blob_sha=row[1])

    async def request_cancel(self, run_id: UUID) -> CancelRequestResult:
        """Persist one cancellation decision while holding the run row lock.

        Queued work cannot have started and is cancelled immediately (T6); an
        attempted Run also gets a durable close signal for its check-run.  Running
        and publishing work remains in its current state until its worker sees
        ``cancel_requested`` at a checkpoint.  Terminal rows are intentionally
        left untouched, making retries idempotent.
        """
        if self._session is None:
            raise RuntimeError("request_cancel() requires an active session within a Unit of Work")
        session = self._session
        run = await session.scalar(
            select(Run).where(Run.id == run_id, self._authorized_run()).with_for_update()
        )
        if run is None:
            return CancelRequestResult(found=False, changed=False)
        if run.state is RunState.QUEUED:
            now = datetime.now(UTC)
            run.state = RunState.CANCELLED
            run.error_code = "cancelled_by_user"
            run.finished_at = now
            if run.attempt >= 1:
                run.cancellation_signal_requested_at = now
            await session.flush()
            await notify_run_state(session, run_id, RunState.CANCELLED)
            return CancelRequestResult(found=True, changed=True, signal_requested=run.attempt >= 1)
        elif run.state in {RunState.RUNNING, RunState.PUBLISHING}:
            if run.cancel_requested:
                return CancelRequestResult(found=True, changed=False)
            run.cancel_requested = True
            await session.flush()
            return CancelRequestResult(found=True, changed=True)
        return CancelRequestResult(found=True, changed=False)

    @staticmethod
    def _summary_only_projection() -> ColumnElement[bool]:
        summary_only = (
            select(func.bool_or(CodeChangeDiff.summary_only))
            .select_from(CodeChangeDiff)
            .where(CodeChangeDiff.run_id == Run.id)
            .scalar_subquery()
        )
        return func.coalesce(summary_only, False).label("summary_only")

    @staticmethod
    def _to_diff_snapshot(row: CodeChangeDiff) -> DiffSnapshot:
        return DiffSnapshot(
            filename=row.filename,
            patch=row.patch,
            blob_sha=row.blob_sha,
            status=row.status,  # type: ignore[arg-type]
            previous_filename=row.previous_filename,
            additions=row.additions,
            deletions=row.deletions,
            changes=row.changes,
            omission_reason=row.omission_reason,
            review_patch=row.review_patch,
            summary_only=row.summary_only,
        )

    @staticmethod
    def _to_run_list_item(
        run: Run,
        code_change: CodeChange,
        repository_name: str,
        model: str | None,
        action_count: int,
        summary_only: bool = False,
    ) -> RunListItem:
        return RunListItem(
            id=run.id,
            status=run.state.value,
            engine=run.engine.value,
            attempt=run.attempt,
            cancel_requested=run.cancel_requested,
            started_at=run.started_at,
            finished_at=run.finished_at,
            error_code=run.error_code,
            model=model,
            action_count=action_count,
            repo=repository_name,
            number=code_change.external_number,
            title=code_change.title,
            url=code_change.web_url,
            head_sha=run.head_sha,
            created_at=run.created_at,
            trigger=run.trigger,
            summary_only=summary_only,
        )

    @staticmethod
    def _to_published_comment(finding: Finding) -> PublishedComment:
        return PublishedComment(
            id=finding.id,
            file=finding.file_path,
            old_line=finding.line_start if finding.side.value == "LEFT" else None,
            new_line=finding.line_start if finding.side.value == "RIGHT" else None,
            end_line=finding.line_end,
            severity=finding.severity.value,
            category=finding.category.value,
            title=finding.title,
            body=finding.body,
            rule_name=finding.rule_name,
            created_at=finding.created_at,
        )

    @staticmethod
    def _to_run_action(action: RunAction) -> RunActionProjection:
        return RunActionProjection(
            id=action.id,
            run_id=action.run_id,
            index=action.index,
            tool=action.tool,
            request=action.request,
            response=action.response,
            response_ref=action.response_ref,
            started_at=action.started_at,
            duration_ms=action.duration_ms,
        )


def diff_snapshot_from_code_change_diff(row: CodeChangeDiff) -> DiffSnapshot:
    """Map a durable diff row to the snapshot shape used by publication DiffMap rebuild."""
    return SqlAlchemyRunRepository._to_diff_snapshot(row)


async def load_publication_diff_map(
    session: AsyncSession,
    run_id: UUID,
    *,
    snapshots: list[DiffSnapshot] | None = None,
    diff_old_sha: str | None = None,
    file_list_complete: bool = True,
) -> DiffMap | None:
    """Rebuild DiffMap/v1 from durable snapshots for publish bucketing and GitLab coords."""
    row = (
        await session.execute(
            select(
                Run.head_sha,
                Run.base_sha,
                CodeChange.repository_id,
                CodeChange.external_number,
                ProviderInstallation.provider,
            )
            .join(CodeChange, CodeChange.id == Run.code_change_id)
            .join(Repository, Repository.id == CodeChange.repository_id)
            .join(
                ProviderInstallation,
                ProviderInstallation.id == Repository.provider_installation_id,
            )
            .where(Run.id == run_id)
        )
    ).one_or_none()
    if row is None:
        return None
    head_sha, base_sha, repository_id, external_number, provider = row
    if snapshots is None:
        snapshot_rows = (
            (
                await session.execute(
                    select(CodeChangeDiff)
                    .where(CodeChangeDiff.run_id == run_id)
                    .order_by(CodeChangeDiff.filename.asc())
                )
            )
            .scalars()
            .all()
        )
        snapshots = [diff_snapshot_from_code_change_diff(item) for item in snapshot_rows]
    return diff_map_from_snapshots(
        repository_id=repository_id,
        code_change_number=external_number,
        head_sha=head_sha,
        base_sha=base_sha,
        diff_old_sha=diff_old_sha,
        snapshots=snapshots,
        provider=vcs_provider_name(provider),
        file_list_complete=file_list_complete,
    )


class SqlAlchemyReviewOutputRepository:
    """Write adapter bound to a caller-owned SQLAlchemy unit of work."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_post_processing_input(self, run_id: UUID) -> FindingPostProcessingInput | None:
        """Snapshot the durable diff and immutable rule version for post-processing.

        New runs use their creation-time pair; historical NULL pins read Repository live.
        """
        row = (
            await self._session.execute(
                select(
                    RuleVersion.rules,
                    Run.max_comments,
                    Run.severity_threshold,
                    Repository.max_comments,
                    Repository.severity_threshold,
                    Run.head_sha,
                )
                .select_from(Run)
                .join(RuleVersion, RuleVersion.id == Run.rule_version_id)
                .join(CodeChange, CodeChange.id == Run.code_change_id)
                .join(Repository, Repository.id == CodeChange.repository_id)
                .where(Run.id == run_id)
            )
        ).one_or_none()
        if row is None:
            return None
        rules, max_comments, severity_threshold, live_cap, live_threshold, head_sha = row
        if max_comments is None and severity_threshold is None:
            max_comments, severity_threshold = live_cap, live_threshold
        durable = [
            diff_snapshot_from_code_change_diff(item)
            for item in (
                await self._session.execute(
                    select(CodeChangeDiff)
                    .where(CodeChangeDiff.run_id == run_id)
                    .order_by(CodeChangeDiff.filename.asc())
                )
            )
            .scalars()
            .all()
        ]
        # A referenced JSON null deserializes as None, like an absent body. Keep
        # that same precedence while testing key presence inside PostgreSQL, so
        # prompt-only actions never send their potentially large bodies here.
        response = case(
            (func.jsonb_typeof(RunActionResponseBody.body) == "null", RunAction.response),
            else_=func.coalesce(RunActionResponseBody.body, RunAction.response),
        )
        context_row = (
            await self._session.execute(
                select(RunAction.request, RunAction.response, RunActionResponseBody.body)
                .outerjoin(
                    RunActionResponseBody,
                    and_(
                        RunActionResponseBody.run_id == RunAction.run_id,
                        cast(RunActionResponseBody.id, String) == RunAction.response_ref,
                    ),
                )
                .where(
                    RunAction.run_id == run_id,
                    RunAction.tool == "context.build",
                    func.jsonb_typeof(response) == "object",
                    response.op("?")("coverage"),
                )
                .order_by(RunAction.index.desc())
                .limit(1)
            )
        ).one_or_none()
        request: object = None
        response_value: object = None
        if context_row is not None:
            request, inline, body = context_row
            response_value = body if body is not None else inline
        pins = read_context_coverage_pins(request, response_value, head_sha=head_sha)
        diff_map = await load_publication_diff_map(
            self._session,
            run_id,
            snapshots=durable,
            diff_old_sha=pins.diff_old_sha if pins is not None else None,
            file_list_complete=pins.file_list_complete if pins is not None else True,
        )
        if diff_map is None:
            return None
        checked_paths = frozenset(
            item.filename for item in durable if item.review_patch is not None
        )
        coverage = restore_context_coverage(
            request, response_value, snapshots=durable, diff_map=diff_map, run_id=run_id
        )
        rule_names = frozenset(rule["name"] for rule in rules if isinstance(rule.get("name"), str))
        # This legacy adapter has no verified manifest: a snapshot row alone cannot
        # authorize v2 attribution. The verified publication path supplies its own allowlist.
        return FindingPostProcessingInput(
            diff_map=diff_map,
            checked_paths=checked_paths,
            rule_names=rule_names,
            repository_max_inline=max_comments,
            severity_threshold=severity_threshold_value(severity_threshold),
            allowed_v2_rules=None,
            coverage=coverage,
            summary_only=any(item.summary_only for item in durable),
        )

    async def store_review_output(
        self,
        run_id: UUID,
        model_output: dict[str, object],
        parsed: ReviewOutput,
        processed: ProcessedReviewOutput,
    ) -> ReviewPublication | None:
        """Flush one validated answer; the use case commits it before networking.

        ``model_output`` is the gateway's normalized answer (PIPELINE_SPEC §9); the raw
        provider answer stays only in the ``llm.call`` trace.
        """
        run = await self._session.scalar(select(Run).where(Run.id == run_id).with_for_update())
        if run is None or run.state in {RunState.SUCCEEDED, RunState.CANCELLED}:
            return None

        existing = await self._session.scalar(
            select(RunAction).where(
                RunAction.run_id == run_id,
                RunAction.tool == "llm.review_output",
            )
        )
        if existing is None:
            await insert_run_action(
                self._session,
                run_id=run_id,
                tool="llm.review_output",
                request={},
                response=model_output,
                started_at=datetime.now(UTC),
                duration_ms=0,
            )
            findings = tuple(_to_published_finding(item.finding) for item in processed.inline)
            records = (*processed.inline, *processed.body_only, *processed.dropped)
            self._session.add_all([_to_finding(run_id, item) for item in records])
            run.review_body = processed.review_body
        else:
            rows = (
                await self._session.scalars(
                    select(Finding)
                    .where(Finding.run_id == run_id, Finding.inline_comment.is_(True))
                    .order_by(Finding.created_at.asc(), Finding.id.asc())
                )
            ).all()
            findings = tuple(_to_published_finding_row(item) for item in rows)

        assert run.review_body is not None
        run.state = RunState.PUBLISHING
        await self._session.flush()
        return ReviewPublication(
            head_sha=run.head_sha,
            findings=findings,
            review_body=run.review_body,
            idempotency_key=run.idempotency_key,
        )

    async def mark_review_published(self, run_id: UUID) -> None:
        """Flush completion after the provider returned; never commits itself."""
        run = await self._session.scalar(select(Run).where(Run.id == run_id).with_for_update())
        if run is None or run.state is RunState.SUCCEEDED:
            return
        await self._session.execute(
            update(Finding)
            .where(Finding.run_id == run_id, Finding.drop_reason.is_(None))
            .values(published=True)
        )
        run.state = RunState.SUCCEEDED
        run.finished_at = datetime.now(UTC)
        await self._session.flush()

    async def lock_for_publishing(self, run_id: UUID) -> PublishingGuard | None:
        """Lock the Run and read the T8 guard; the caller stores findings in the same UOW."""
        row = (
            await self._session.execute(
                select(Run, CodeChange.head_sha, Repository.review_event)
                .join(CodeChange, CodeChange.id == Run.code_change_id)
                .join(Repository, Repository.id == CodeChange.repository_id)
                .where(Run.id == run_id)
                .with_for_update(of=Run)
            )
        ).one_or_none()
        if row is None:
            return None
        run, pr_head_sha, repository_review_event = row
        return PublishingGuard(
            state=run.state.value,
            worker_id=run.worker_id,
            cancel_requested=run.cancel_requested,
            head_sha=run.head_sha,
            head_current=run.head_sha == pr_head_sha,
            repository_review_event=repository_review_event.value,
        )

    async def enter_publishing(
        self,
        run_id: UUID,
        *,
        lease_until: datetime,
        postprocess_request: dict[str, Any],
        postprocess_response: dict[str, Any],
        started_at: datetime,
        duration_ms: int,
        verify_request: dict[str, Any] | None = None,
        verify_response: dict[str, Any] | None = None,
    ) -> None:
        """Record ``review.postprocess``, the publishing lease and NOTIFY (T8); flush only.

        A verifier trace is recorded first as ``llm.verify``, once per run like
        ``llm.review_output`` (CVS §7.4); a run without a verifier pass writes none.
        """
        if verify_request is not None:
            existing = await self._session.scalar(
                select(RunAction.id).where(
                    RunAction.run_id == run_id,
                    RunAction.tool == "llm.verify",
                )
            )
            if existing is None:
                await insert_run_action(
                    self._session,
                    run_id=run_id,
                    tool="llm.verify",
                    request=verify_request,
                    response=verify_response,
                    started_at=started_at,
                    duration_ms=0,
                )
        await insert_run_action(
            self._session,
            run_id=run_id,
            tool="review.postprocess",
            request=postprocess_request,
            response=postprocess_response,
            started_at=started_at,
            duration_ms=duration_ms,
        )
        await self._session.execute(
            update(Run).where(Run.id == run_id).values(lease_until=lease_until)
        )
        await self._session.flush()
        await notify_run_state(self._session, run_id, RunState.PUBLISHING)


def _to_published_finding(item: object) -> PublishedFinding:
    """Translate a parsed Pydantic finding without weakening its contract."""
    from app.modules.reviews.application.review_output import ReviewFinding

    assert isinstance(item, ReviewFinding)
    return PublishedFinding(
        path=item.path,
        line=item.line,
        start_line=item.start_line,
        severity=item.severity,
        category=item.category,
        title=item.title,
        body=item.body,
        suggestion=item.suggestion,
        confidence=item.confidence,
        rule_name=item.rule_name,
    )


def _to_finding(run_id: UUID, item: ProcessedFinding) -> Finding:
    """Map new-version review anchors to the existing findings table."""
    from app.common.infrastructure.db.enums import FindingCategory, FindingSeverity, FindingSide

    return Finding(
        run_id=run_id,
        file_path=item.finding.path,
        line_start=item.finding.start_line or item.finding.line,
        line_end=item.finding.line if item.finding.start_line is not None else None,
        side=FindingSide.RIGHT,
        severity=FindingSeverity(item.finding.severity),
        confidence=Decimal(str(item.finding.confidence)),
        category=FindingCategory(item.finding.category),
        suggestion=item.finding.suggestion,
        title=item.finding.title,
        body=item.finding.body,
        rule_name=item.finding.rule_name,
        published=False,
        inline_comment=item.bucket == "inline",
        drop_reason=item.drop_reason,
    )


def _to_published_finding_row(item: Finding) -> PublishedFinding:
    """Rebuild a retry-safe provider payload from the durable finding row."""
    return PublishedFinding(
        path=item.file_path,
        line=item.line_end or item.line_start,
        start_line=item.line_start if item.line_end is not None else None,
        severity=item.severity.value,
        category=item.category.value,
        title=item.title,
        body=item.body,
        suggestion=item.suggestion,
        confidence=float(item.confidence),
        rule_name=item.rule_name,
        finding_id=item.id,
    )


class SqlAlchemyCancelRunUnitOfWork(SqlAlchemyUnitOfWork):
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        scope: AuthScope | None = None,
        *,
        allow_unscoped: bool = False,
    ) -> None:
        if scope is None and not allow_unscoped:
            raise ValueError(
                "SqlAlchemyCancelRunUnitOfWork requires an AuthScope unless allow_unscoped=True"
            )
        super().__init__(session_factory)
        self._scope = scope
        self._allow_unscoped = allow_unscoped

    @property
    def repository(self) -> SqlAlchemyRunRepository:
        return SqlAlchemyRunRepository(
            session=self.session, scope=self._scope, allow_unscoped=self._allow_unscoped
        )
