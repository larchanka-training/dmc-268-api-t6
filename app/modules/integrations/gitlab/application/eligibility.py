"""Pure GitLab enqueue policy; metadata authority is the candidate port's responsibility."""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from app.modules.reviews.application.determine_ci_eligibility import (
    CiEligibility,
    CiWaitMode,
    EligibilityCandidate,
    EligibilityReason,
)
from app.modules.reviews.application.project_github_pull_request import PullRequestState


class GitLabEligibilityCandidateStore(Protocol):
    """Return detached authorized metadata; inactive/missing targets return None.

    Operational failures propagate and must never masquerade as permanent absence.
    """

    async def get(self, code_change_id: UUID) -> EligibilityCandidate | None: ...


class DetermineGitLabEligibility:
    def __init__(self, *, candidates: GitLabEligibilityCandidateStore) -> None:
        self._candidates = candidates

    async def execute(
        self,
        code_change_id: UUID,
        expected_head_sha: str,
        *,
        expected_installation_external_id: int | None = None,
    ) -> CiEligibility:
        candidate = await self._candidates.get(code_change_id)
        if candidate is None:
            return CiEligibility(False, EligibilityReason.UNKNOWN_PR, None)
        reason: EligibilityReason | None = None
        if candidate.identity is None or candidate.identity.provider != "gitlab":
            reason = EligibilityReason.UNSUPPORTED_PROVIDER
        elif (
            expected_installation_external_id is not None
            and candidate.installation_external_id != expected_installation_external_id
        ):
            reason = EligibilityReason.STALE_STATE
        elif candidate.head_sha != expected_head_sha:
            reason = EligibilityReason.STALE_HEAD
        elif not candidate.repository_enabled:
            reason = EligibilityReason.DISABLED_REPOSITORY
        elif candidate.state != PullRequestState.OPEN:
            reason = EligibilityReason.CLOSED_PR
        elif candidate.wait_for_ci != CiWaitMode.NEVER:
            reason = EligibilityReason.UNSUPPORTED_CI_POLICY
        if reason is not None:
            return CiEligibility(False, reason, candidate.head_sha)
        return CiEligibility(True, EligibilityReason.ELIGIBLE, candidate.head_sha, candidate)
