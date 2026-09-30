"""Read the immutable database-backed prompt context for one review run."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.repositories.infrastructure.models import RuleVersion
from app.modules.reviews.application.conventions import GeneratedConventions
from app.modules.reviews.application.execute_review import ReviewPromptInput
from app.modules.reviews.application.get_run_diff import (
    DiffSnapshot,
    review_files_from_snapshots,
)
from app.modules.reviews.application.prompt_builder import (
    RepoConventions as PromptConventions,
)
from app.modules.reviews.application.prompt_builder import review_rule_from_stored
from app.modules.reviews.infrastructure.models import CodeChangeDiff, PromptVersion, Run


class SqlAlchemyReviewPromptRepository:
    """Projects the persisted run revision into the pure prompt-builder input."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get_review_prompt_input(
        self, run_id: UUID, conventions: GeneratedConventions
    ) -> ReviewPromptInput | None:
        statement = (
            select(
                PromptVersion.content,
                RuleVersion.rules,
            )
            .select_from(Run)
            .join(PromptVersion, PromptVersion.id == Run.prompt_version_id)
            .join(RuleVersion, RuleVersion.id == Run.rule_version_id)
            .where(Run.id == run_id)
        )
        async with self._session_factory() as session:
            row = (await session.execute(statement)).one_or_none()
            if row is None:
                return None
            system, stored_rules = row
            snapshots = (
                await session.execute(
                    select(CodeChangeDiff)
                    .where(CodeChangeDiff.run_id == run_id)
                    .order_by(CodeChangeDiff.filename.asc())
                )
            ).all()
        changed, omitted = review_files_from_snapshots(
            [
                DiffSnapshot(
                    filename=row.filename,
                    patch=row.patch,
                    blob_sha=row.blob_sha,
                    status=row.status,
                    previous_filename=row.previous_filename,
                    additions=row.additions,
                    deletions=row.deletions,
                    changes=row.changes,
                    omission_reason=row.omission_reason,
                    review_patch=row.review_patch,
                    summary_only=row.summary_only,
                )
                for (row,) in snapshots
            ]
        )
        return ReviewPromptInput(
            system=system,
            rules=tuple(review_rule_from_stored(item) for item in stored_rules),
            agents_md=conventions.agents_md,
            conventions=PromptConventions(
                key_patterns=conventions.conventions.key_patterns,
                recommendations=conventions.conventions.recommendations,
            ),
            changed_files=changed,
            omitted_files=omitted,
        )
