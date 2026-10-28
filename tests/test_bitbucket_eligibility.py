"""Bitbucket review starts only for an active open head with supported CI policy."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from uuid import UUID

from app.modules.integrations.bitbucket.application.eligibility import DetermineBitbucketEligibility
from app.modules.reviews.application.determine_ci_eligibility import (
    CiWaitMode,
    EligibilityCandidate,
    EligibilityReason,
    WebhookRunIdentity,
)
from app.modules.reviews.application.project_github_pull_request import PullRequestState


@dataclass
class Candidates:
    item: EligibilityCandidate | None

    async def get(self, code_change_id: UUID) -> EligibilityCandidate | None:
        return self.item


def test_bitbucket_head_and_wait_policy_gate() -> None:
    async def scenario() -> None:
        identity = WebhookRunIdentity(
            "bitbucket",
            UUID(int=1),
            UUID(int=2),
            UUID(int=3),
            17,
            credential_generation=4,
            repository_uuid=UUID(int=5),
            binding_generation=6,
            webhook_generation=7,
        )
        candidate = EligibilityCandidate(
            UUID(int=8),
            11,
            "team/repo",
            "a" * 40,
            PullRequestState.OPEN,
            True,
            None,
            None,
            None,
            CiWaitMode.NEVER,
            identity,
        )
        store = Candidates(candidate)
        gate = DetermineBitbucketEligibility(candidates=store)
        assert (await gate.execute(candidate.code_change_id, "a" * 40)).eligible
        assert (
            await gate.execute(candidate.code_change_id, "b" * 40)
        ).reason == EligibilityReason.STALE_HEAD
        store.item = EligibilityCandidate(
            candidate.code_change_id,
            11,
            "team/repo",
            "a" * 40,
            PullRequestState.OPEN,
            True,
            None,
            None,
            None,
            CiWaitMode.AUTO,
            identity,
        )
        assert (
            await gate.execute(candidate.code_change_id, "a" * 40)
        ).reason == EligibilityReason.UNSUPPORTED_CI_POLICY

    asyncio.run(scenario())
