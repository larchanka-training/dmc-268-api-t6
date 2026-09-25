"""SQLAlchemy read projection for review runs."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import ColumnElement, and_, delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.common.infrastructure.db.enums import RunState
from app.modules.analytics.infrastructure.models import UsageEvent
from app.modules.repositories.infrastructure.models import Repository, RuleVersion
from app.modules.reviews.application.cancel_run import CancelRequestResult
from app.modules.reviews.application.findings_post_processor import (
    ProcessedFinding,
    ProcessedReviewOutput,
)
from app.modules.reviews.application.get_run_actions import RunAction as RunActionProjection
from app.modules.reviews.application.get_run_actions import RunActionResponse
from app.modules.reviews.application.get_run_comments import PublishedComment
from app.modules.reviews.application.get_run_diff import DiffSnapshot
from app.modules.reviews.application.get_run_file_lines import BlobCacheKey
from app.modules.reviews.application.list_runs import RunCursor, RunListItem
from app.modules.reviews.application.process_run import RunConventionsInput, RunDiffInput
from app.modules.reviews.application.prompt_builder import parse_unified_diff
from app.modules.reviews.application.review_output import (
    FindingPostProcessingInput,
    PublishedFinding,
    ReviewOutput,
    ReviewPublication,
)
from app.modules.reviews.infrastructure.models import (
    CodeChange,
    CodeChangeDiff,
    Finding,
    Run,
    RunAction,
)


class SqlAlchemyRunRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

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

        async with self._session_factory() as session:
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
        )
        async with self._session_factory() as session:
            row = (await session.execute(statement)).one_or_none()
        return self._to_run_list_item(*row) if row is not None else None

    async def get_published_comments(self, run_id: UUID) -> list[PublishedComment] | None:
        statement = (
            select(Run.id, Finding)
            .outerjoin(
                Finding,
                and_(Finding.run_id == Run.id, Finding.published.is_(True)),
            )
            .where(Run.id == run_id)
            .order_by(Finding.created_at.asc(), Finding.id.asc())
        )
        async with self._session_factory() as session:
            rows = (await session.execute(statement)).all()
        if not rows:
            return None
        return [self._to_published_comment(finding) for _, finding in rows if finding is not None]

    async def get_run_actions(self, run_id: UUID) -> list[RunActionProjection] | None:
        statement = (
            select(Run.id, RunAction)
            .outerjoin(RunAction, RunAction.run_id == Run.id)
            .where(Run.id == run_id)
            .order_by(RunAction.index.asc())
        )
        async with self._session_factory() as session:
            rows = (await session.execute(statement)).all()
        if not rows:
            return None
        return [self._to_run_action(action) for _, action in rows if action is not None]

    async def get_run_action_response(self, run_id: UUID, index: int) -> RunActionResponse | None:
        statement = (
            select(RunAction.response)
            .join(Run, RunAction.run_id == Run.id)
            .where(Run.id == run_id, RunAction.index == index)
        )
        async with self._session_factory() as session:
            row = (await session.execute(statement)).one_or_none()
        if row is None:
            return None
        return RunActionResponse(response=row[0])

    async def get_run_diff(self, run_id: UUID) -> list[DiffSnapshot] | None:
        statement = (
            select(Run.id, CodeChangeDiff.filename, CodeChangeDiff.patch)
            .outerjoin(
                CodeChangeDiff,
                and_(
                    CodeChangeDiff.code_change_id == Run.code_change_id,
                    CodeChangeDiff.head_sha == Run.head_sha,
                ),
            )
            .where(Run.id == run_id)
            .order_by(CodeChangeDiff.filename.asc())
        )
        async with self._session_factory() as session:
            rows = (await session.execute(statement)).all()
        if not rows:
            return None
        return [
            DiffSnapshot(filename=filename, patch=patch)
            for _, filename, patch in rows
            if filename is not None
        ]

    async def replace_diff_snapshots(
        self, code_change_id: UUID, head_sha: str, snapshots: list[DiffSnapshot]
    ) -> None:
        async with self._session_factory.begin() as session:
            await session.execute(
                delete(CodeChangeDiff).where(
                    CodeChangeDiff.code_change_id == code_change_id,
                    CodeChangeDiff.head_sha == head_sha,
                )
            )
            session.add_all(
                [
                    CodeChangeDiff(
                        code_change_id=code_change_id,
                        head_sha=head_sha,
                        filename=snapshot.filename,
                        patch=snapshot.patch,
                    )
                    for snapshot in snapshots
                ]
            )

    async def get_run_diff_input(self, run_id: UUID) -> RunDiffInput | None:
        statement = select(Run.code_change_id, Run.head_sha).where(Run.id == run_id)
        async with self._session_factory() as session:
            row = (await session.execute(statement)).one_or_none()
        if row is None:
            return None
        return RunDiffInput(code_change_id=row[0], head_sha=row[1])

    async def get_run_conventions_input(self, run_id: UUID) -> RunConventionsInput | None:
        statement = (
            select(CodeChange.repository_id, Run.prompt_version_id)
            .join(CodeChange, CodeChange.id == Run.code_change_id)
            .where(Run.id == run_id)
        )
        async with self._session_factory() as session:
            row = (await session.execute(statement)).one_or_none()
        if row is None:
            return None
        return RunConventionsInput(repository_id=row[0], prompt_version_id=row[1])

    async def get_run_file_key(self, run_id: UUID, path: str) -> BlobCacheKey | None:
        statement = (
            select(Run.code_change_id, Run.head_sha)
            .join(
                CodeChangeDiff,
                and_(
                    CodeChangeDiff.code_change_id == Run.code_change_id,
                    CodeChangeDiff.head_sha == Run.head_sha,
                    CodeChangeDiff.filename == path,
                ),
            )
            .where(Run.id == run_id)
        )
        async with self._session_factory() as session:
            row = (await session.execute(statement)).one_or_none()
        if row is None:
            return None
        return BlobCacheKey(code_change_id=row[0], head_sha=row[1], path=path)

    async def request_cancel(self, run_id: UUID) -> CancelRequestResult:
        """Persist one cancellation decision while holding the run row lock.

        Queued work cannot have started and is cancelled immediately.  Running
        and publishing work remains in its current state until its worker sees
        ``cancel_requested`` at a checkpoint.  Terminal rows are intentionally
        left untouched, making retries idempotent.
        """
        async with self._session_factory.begin() as session:
            run = await session.scalar(select(Run).where(Run.id == run_id).with_for_update())
            if run is None:
                return CancelRequestResult(found=False, changed=False)
            if run.state is RunState.QUEUED:
                run.state = RunState.CANCELLED
                return CancelRequestResult(found=True, changed=True)
            elif run.state in {RunState.RUNNING, RunState.PUBLISHING}:
                if run.cancel_requested:
                    return CancelRequestResult(found=True, changed=False)
                run.cancel_requested = True
                return CancelRequestResult(found=True, changed=True)
        return CancelRequestResult(found=True, changed=False)

    @staticmethod
    def _summary_only_projection() -> ColumnElement[bool]:
        snapshot_count = (
            select(func.count())
            .select_from(CodeChangeDiff)
            .where(
                CodeChangeDiff.code_change_id == Run.code_change_id,
                CodeChangeDiff.head_sha == Run.head_sha,
            )
            .scalar_subquery()
        )
        textual_snapshot_count = (
            select(func.count())
            .select_from(CodeChangeDiff)
            .where(
                CodeChangeDiff.code_change_id == Run.code_change_id,
                CodeChangeDiff.head_sha == Run.head_sha,
                CodeChangeDiff.patch.is_not(None),
            )
            .scalar_subquery()
        )
        return and_(snapshot_count > 0, textual_snapshot_count == 0).label("summary_only")

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


class SqlAlchemyReviewOutputRepository:
    """Write adapter bound to a caller-owned SQLAlchemy unit of work."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_post_processing_input(self, run_id: UUID) -> FindingPostProcessingInput | None:
        """Snapshot only the durable diff and immutable rule version for post-processing."""
        row = (
            await self._session.execute(
                select(Run.code_change_id, Run.head_sha, RuleVersion.rules, Repository.max_comments)
                .join(RuleVersion, RuleVersion.id == Run.rule_version_id)
                .join(CodeChange, CodeChange.id == Run.code_change_id)
                .join(Repository, Repository.id == CodeChange.repository_id)
                .where(Run.id == run_id)
            )
        ).one_or_none()
        if row is None:
            return None
        code_change_id, head_sha, rules, max_comments = row
        snapshots = (
            await self._session.execute(
                select(CodeChangeDiff.filename, CodeChangeDiff.patch).where(
                    CodeChangeDiff.code_change_id == code_change_id,
                    CodeChangeDiff.head_sha == head_sha,
                )
            )
        ).all()
        hunk_lines = {filename: _new_hunk_lines(filename, patch) for filename, patch in snapshots}
        rule_names = frozenset(rule["name"] for rule in rules if isinstance(rule.get("name"), str))
        return FindingPostProcessingInput(
            hunk_lines=hunk_lines,
            rule_names=rule_names,
            repository_max_inline=max_comments,
        )

    async def store_review_output(
        self,
        run_id: UUID,
        raw_output: dict[str, object],
        parsed: ReviewOutput,
        processed: ProcessedReviewOutput,
    ) -> ReviewPublication | None:
        """Flush one validated answer; the use case commits it before networking."""
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
            index = await self._session.scalar(
                select(func.coalesce(func.max(RunAction.index), -1)).where(
                    RunAction.run_id == run_id
                )
            )
            assert index is not None
            self._session.add(
                RunAction(
                    run_id=run_id,
                    index=index + 1,
                    tool="llm.review_output",
                    request={},
                    response=raw_output,
                    response_ref=None,
                    started_at=datetime.now(UTC),
                    duration_ms=0,
                )
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
    )


def _new_hunk_lines(filename: str, patch: str | None) -> frozenset[int]:
    """Return only new-version anchors in one immutable stored diff snapshot."""
    if patch is None:
        return frozenset()
    diff = patch
    if not patch.startswith("diff --git "):
        diff = f"diff --git a/{filename} b/{filename}\n{patch}"
    changed_files = parse_unified_diff(diff)
    return frozenset(
        line.number
        for changed_file in changed_files
        for line in changed_file.lines
        if line.type in {"added", "context"}
    )
