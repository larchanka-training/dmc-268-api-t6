"""Decide whether a current PR head may enter Task 5's enqueue transaction."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Protocol
from uuid import UUID

# A GitHub-supplied status, conclusion or state goes into the outcome log line only when it is
# a plain token; anything else is logged as ``?``.
from app.common.application.log_token import log_token
from app.modules.reviews.application.project_github_pull_request import PullRequestState


class CiWaitMode(StrEnum):
    NEVER = "never"
    ALWAYS = "always"
    AUTO = "auto"


class EligibilityReason(StrEnum):
    ELIGIBLE = "eligible"
    UNKNOWN_PR = "unknown_pr"
    DISABLED_REPOSITORY = "disabled_repository"
    CLOSED_PR = "closed_pr"
    LABEL_NOT_ACTIVE = "label_not_active"
    STALE_HEAD = "stale_head"
    STALE_STATE = "stale_state"
    WAITING_FOR_CI = "waiting_for_ci"
    CI_BLOCKED = "ci_blocked"


@dataclass(frozen=True)
class EligibilityCandidate:
    code_change_id: UUID
    installation_external_id: int
    repository_full_name: str
    head_sha: str
    state: PullRequestState
    repository_enabled: bool
    ai_review_labeled: bool
    ai_review_labeled_at: datetime | None
    head_first_seen_at: datetime | None
    wait_for_ci: CiWaitMode


@dataclass(frozen=True)
class CheckSuite:
    app_id: int
    status: str
    conclusion: str | None
    # A suite whose App created no check run stays ``queued`` for good and never sends
    # ``completed``. Only that state is ignored by the gate (#72); an unknown count means
    # "runs may exist", so the default keeps blocking.
    latest_check_runs_count: int = 1


@dataclass(frozen=True)
class CiSnapshot:
    head_sha: str
    check_suites: tuple[CheckSuite, ...]
    combined_state: str
    combined_total_count: int


@dataclass(frozen=True)
class CiEligibility:
    eligible: bool
    reason: EligibilityReason
    head_sha: str | None
    candidate: EligibilityCandidate | None = field(default=None, compare=False)
    # What blocks (``ci_blocked``) or delays (``waiting_for_ci``) the gate, for the outcome line.
    detail: str | None = None


class EligibilityCandidateStore(Protocol):
    """Each read returns a detached snapshot and closes its DB transaction."""

    async def get(self, code_change_id: UUID) -> EligibilityCandidate | None: ...


class CurrentHeadCiProvider(Protocol):
    async def get_current_head_ci(
        self, installation_external_id: int, repository_full_name: str, head_sha: str
    ) -> CiSnapshot: ...


class DetermineCiEligibility:
    """Read, fetch GitHub outside the DB, then reject an intervening state change."""

    def __init__(
        self,
        *,
        candidates: EligibilityCandidateStore,
        ci: CurrentHeadCiProvider,
        own_app_id: int,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if own_app_id <= 0:
            raise ValueError("GitHub App ID must be positive")
        self._candidates = candidates
        self._ci = ci
        self._own_app_id = own_app_id
        self._now = now

    async def execute(self, code_change_id: UUID, expected_head_sha: str) -> CiEligibility:
        candidate = await self._candidates.get(code_change_id)
        precheck = self._precheck(candidate, expected_head_sha)
        if precheck is not None:
            return precheck
        assert candidate is not None
        if candidate.wait_for_ci == CiWaitMode.NEVER:
            return CiEligibility(True, EligibilityReason.ELIGIBLE, candidate.head_sha, candidate)

        snapshot = await self._ci.get_current_head_ci(
            candidate.installation_external_id,
            candidate.repository_full_name,
            candidate.head_sha,
        )
        current = await self._candidates.get(code_change_id)
        precheck = self._precheck(current, expected_head_sha)
        if precheck is not None:
            return precheck
        if current != candidate or snapshot.head_sha != candidate.head_sha:
            reason = (
                EligibilityReason.STALE_HEAD
                if current is not None and current.head_sha != candidate.head_sha
                else EligibilityReason.STALE_STATE
            )
            return CiEligibility(False, reason, current.head_sha if current is not None else None)

        foreign = tuple(
            suite
            for suite in snapshot.check_suites
            if suite.app_id != self._own_app_id
            and not (suite.status == "queued" and suite.latest_check_runs_count == 0)
        )
        blocking = tuple(
            suite
            for suite in foreign
            if suite.status != "completed"
            or suite.conclusion not in {"success", "neutral", "skipped"}
        )
        if blocking:
            return CiEligibility(
                False,
                EligibilityReason.CI_BLOCKED,
                candidate.head_sha,
                detail=_blocking_suites_detail(blocking),
            )
        if snapshot.combined_total_count > 0 and snapshot.combined_state != "success":
            return CiEligibility(
                False,
                EligibilityReason.CI_BLOCKED,
                candidate.head_sha,
                detail=f"commit status {log_token(snapshot.combined_state) or '?'}",
            )
        if foreign or snapshot.combined_total_count > 0:
            return CiEligibility(True, EligibilityReason.ELIGIBLE, candidate.head_sha, candidate)
        if candidate.wait_for_ci == CiWaitMode.ALWAYS:
            return CiEligibility(
                False, EligibilityReason.WAITING_FOR_CI, candidate.head_sha, detail="no CI yet"
            )
        if candidate.ai_review_labeled_at is None or candidate.head_first_seen_at is None:
            return CiEligibility(
                False,
                EligibilityReason.WAITING_FOR_CI,
                candidate.head_sha,
                detail="no CI yet, label or head time unknown",
            )
        window_start = max(candidate.ai_review_labeled_at, candidate.head_first_seen_at)
        auto_start = window_start + timedelta(minutes=2)
        if self._now() < auto_start:
            return CiEligibility(
                False,
                EligibilityReason.WAITING_FOR_CI,
                candidate.head_sha,
                detail=f"no CI yet, auto start at {auto_start.astimezone(UTC).isoformat()}",
            )
        return CiEligibility(True, EligibilityReason.ELIGIBLE, candidate.head_sha, candidate)

    @staticmethod
    def _precheck(
        candidate: EligibilityCandidate | None, expected_head_sha: str
    ) -> CiEligibility | None:
        if candidate is None:
            return CiEligibility(False, EligibilityReason.UNKNOWN_PR, None)
        if candidate.head_sha != expected_head_sha:
            return CiEligibility(False, EligibilityReason.STALE_HEAD, candidate.head_sha)
        if not candidate.repository_enabled:
            return CiEligibility(False, EligibilityReason.DISABLED_REPOSITORY, candidate.head_sha)
        if candidate.state != PullRequestState.OPEN:
            return CiEligibility(False, EligibilityReason.CLOSED_PR, candidate.head_sha)
        if not candidate.ai_review_labeled:
            return CiEligibility(False, EligibilityReason.LABEL_NOT_ACTIVE, candidate.head_sha)
        return None


def _blocking_suites_detail(blocking: Sequence[CheckSuite]) -> str:
    """Name the first blocking suite in GitHub's order and count the others."""
    first = blocking[0]
    state = log_token(first.status) or "?"
    if first.conclusion is not None:
        state += f"/{log_token(first.conclusion) or '?'}"
    detail = f"check suite app={first.app_id} {state}"
    if len(blocking) > 1:
        detail += f" (+{len(blocking) - 1} more)"
    return detail
