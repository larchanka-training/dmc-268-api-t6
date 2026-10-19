"""Metadata-only GitLab snapshots using the sole T8 authority predicate."""

from __future__ import annotations

import logging
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.integrations.gitlab.application.request_credentials import (
    BoundGitLabTarget,
    GitLabRequestUnavailable,
    GitLabTargetInactive,
)
from app.modules.integrations.gitlab.infrastructure.models import GitLabConnection
from app.modules.integrations.gitlab.infrastructure.request_credentials import (
    SqlAlchemyGitLabTargetReader,
)
from app.modules.repositories.infrastructure.models import ProviderInstallation, Repository
from app.modules.reviews.application.determine_ci_eligibility import (
    CiWaitMode,
    EligibilityCandidate,
    WebhookRunIdentity,
)
from app.modules.reviews.application.project_github_pull_request import PullRequestState
from app.modules.reviews.infrastructure.models import CodeChange

_LOGGER = logging.getLogger(__name__)


async def read_gitlab_eligibility_target(
    session: AsyncSession, code_change_id: UUID
) -> BoundGitLabTarget | None:
    """Route lookup only. Active/binding authority belongs exclusively to T8."""
    try:
        row = (
            await session.execute(
                select(
                    ProviderInstallation.id,
                    ProviderInstallation.workspace_id,
                    Repository.id,
                    GitLabConnection.instance_origin,
                    Repository.external_id,
                    GitLabConnection.credential_generation,
                )
                .select_from(CodeChange)
                .join(Repository, CodeChange.repository_id == Repository.id)
                .join(
                    ProviderInstallation,
                    Repository.provider_installation_id == ProviderInstallation.id,
                )
                .join(
                    GitLabConnection,
                    GitLabConnection.provider_installation_id == ProviderInstallation.id,
                )
                .where(CodeChange.id == code_change_id, ProviderInstallation.provider == "gitlab")
            )
        ).one_or_none()
    except Exception:
        raise GitLabRequestUnavailable() from None
    if row is None:
        return None
    try:
        return BoundGitLabTarget(*row)
    except ValueError:
        return None


async def read_gitlab_eligibility_candidate(
    session: AsyncSession, code_change_id: UUID, *, expected_target: BoundGitLabTarget | None = None
) -> EligibilityCandidate | None:
    """Borrow the caller's transaction; never obtain, close or commit another lease.

    The inert factory only satisfies the target reader's existing constructor. Only
    its borrowed checkpoint is invoked, preserving one predicate and pool-size-one.
    """
    target = await read_gitlab_eligibility_target(session, code_change_id)
    if target is None or (expected_target is not None and target != expected_target):
        return None
    reader = SqlAlchemyGitLabTargetReader(async_sessionmaker(bind=session.bind))
    try:
        await reader.checkpoint_in_session(session, target)
    except GitLabTargetInactive:
        return None
    try:
        row = (
            await session.execute(
                select(
                    CodeChange.id,
                    ProviderInstallation.external_id,
                    Repository.full_name,
                    CodeChange.head_sha,
                    CodeChange.state,
                    Repository.wait_for_ci,
                )
                .select_from(CodeChange)
                .join(Repository, CodeChange.repository_id == Repository.id)
                .join(
                    ProviderInstallation,
                    Repository.provider_installation_id == ProviderInstallation.id,
                )
                .where(
                    CodeChange.id == code_change_id,
                    Repository.id == target.repository_id,
                    ProviderInstallation.id == target.origin_provider_installation_id,
                )
            )
        ).one_or_none()
    except Exception:
        raise GitLabRequestUnavailable() from None
    if row is None:
        return None
    return EligibilityCandidate(
        code_change_id=row[0],
        installation_external_id=row[1],
        repository_full_name=row[2],
        head_sha=row[3],
        state=PullRequestState(row[4].value),
        repository_enabled=True,
        ai_review_labeled=None,
        ai_review_labeled_at=None,
        head_first_seen_at=None,
        wait_for_ci=CiWaitMode(row[5].value),
        identity=WebhookRunIdentity(
            "gitlab",
            target.origin_provider_installation_id,
            target.workspace_id,
            target.repository_id,
            target.project_id,
            target.instance_origin,
            target.credential_generation,
        ),
    )


class SqlAlchemyGitLabEligibilityCandidateStore:
    """Missing/inactive targets are absent; operational failure remains unavailable."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = session_factory

    async def get(self, code_change_id: UUID) -> EligibilityCandidate | None:
        session = self._sessions()
        # Translate only owned session lifecycle operations. The mapping/body below
        # is outside the generic catch and keeps its original exception taxonomy.
        try:
            await session.__aenter__()
        except Exception:
            raise GitLabRequestUnavailable() from None
        try:
            candidate = await read_gitlab_eligibility_candidate(session, code_change_id)
        except BaseException as body_error:
            try:
                await session.__aexit__(type(body_error), body_error, body_error.__traceback__)
            except Exception:
                # Preserve the primary authority/mapping/cancellation failure after
                # attempting owned cleanup; never replace it with driver diagnostics.
                _LOGGER.warning("GitLab metadata session cleanup failed after a read failure")
            raise
        try:
            await session.__aexit__(None, None, None)
        except Exception:
            raise GitLabRequestUnavailable() from None
        return candidate
