"""The read behind the public badge (#454): it takes no ``AuthScope`` and needs none."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.common.infrastructure.db.enums import RunState
from app.modules.repositories.infrastructure.models import ProviderInstallation, Repository
from app.modules.reviews.application.repository_badge import BadgeRun
from app.modules.reviews.infrastructure.models import CodeChange, Finding, Run
from app.modules.reviews.infrastructure.run_repository import SqlAlchemyRunRepository
from app.modules.reviews.infrastructure.saved_review_results import load_review_results


class SqlAlchemyRepositoryBadgeQueries:
    """The rule of who may see what is here and only here: public GitHub repositories."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def latest_succeeded_run(self, full_name: str) -> BadgeRun | None:
        async with self._session_factory() as session:
            repositories = (
                await session.execute(
                    select(Repository.id, Repository.private)
                    .join(
                        ProviderInstallation,
                        Repository.provider_installation_id == ProviderInstallation.id,
                    )
                    .where(
                        func.lower(Repository.full_name) == full_name.lower(),
                        ProviderInstallation.provider == "github",
                        # Without the App no event says the repository went private.
                        ProviderInstallation.deleted_at.is_(None),
                        Repository.installation_available.is_(True),
                    )
                    .limit(2)
                )
            ).all()
            # Two live rows mean one holds the old name of a renamed repository, and nothing
            # proves which: neither of them answers.
            if len(repositories) != 1 or repositories[0].private is not False:
                return None
            run = (
                await session.execute(
                    select(
                        Run.id,
                        Run.head_sha,
                        SqlAlchemyRunRepository._summary_only_projection().label("summary_only"),
                    )
                    .join(CodeChange, CodeChange.id == Run.code_change_id)
                    .where(
                        CodeChange.repository_id == repositories[0].id,
                        Run.state == RunState.SUCCEEDED,
                    )
                    .order_by(Run.finished_at.desc().nulls_last(), Run.id.desc())
                    .limit(1)
                )
            ).one_or_none()
            if run is None:
                return None
            severities = [
                severity.value
                for severity in await session.scalars(
                    select(Finding.severity).where(
                        Finding.run_id == run.id,
                        Finding.published.is_(True),
                        Finding.drop_reason.is_(None),
                    )
                )
            ]
            results = await load_review_results(
                session, {run.id: run.head_sha}, {run.id: severities}
            )
        return BadgeRun(bool(run.summary_only), tuple(severities), results.get(run.id))
