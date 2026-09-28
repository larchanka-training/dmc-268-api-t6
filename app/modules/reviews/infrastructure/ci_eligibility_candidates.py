"""Detached PR/repository settings snapshots for current-head CI decisions."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.repositories.infrastructure.models import ProviderInstallation, Repository
from app.modules.reviews.application.determine_ci_eligibility import (
    CiWaitMode,
    EligibilityCandidate,
)
from app.modules.reviews.application.project_github_pull_request import PullRequestState
from app.modules.reviews.infrastructure.models import CodeChange


class SqlAlchemyEligibilityCandidateStore:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get(self, code_change_id: UUID) -> EligibilityCandidate | None:
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(CodeChange, Repository, ProviderInstallation.external_id)
                    .join(Repository, CodeChange.repository_id == Repository.id)
                    .join(
                        ProviderInstallation,
                        Repository.provider_installation_id == ProviderInstallation.id,
                    )
                    .where(
                        CodeChange.id == code_change_id,
                        ProviderInstallation.provider == "github",
                    )
                )
            ).one_or_none()
            if row is None:
                return None
            pr, repository, installation_external_id = row
            return EligibilityCandidate(
                code_change_id=pr.id,
                installation_external_id=installation_external_id,
                repository_full_name=repository.full_name,
                head_sha=pr.head_sha,
                state=PullRequestState(pr.state.value),
                repository_enabled=repository.enabled,
                reviewer_requested=pr.reviewer_requested,
                reviewer_requested_at=pr.reviewer_requested_at,
                head_first_seen_at=pr.head_first_seen_at,
                wait_for_ci=CiWaitMode(repository.wait_for_ci.value),
            )
