"""Decide whether a current PR head may enter Task 5's enqueue transaction."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from functools import partial
from typing import Protocol
from uuid import UUID

from app.common.application.log_token import log_token
from app.modules.integrations.gitlab.application.manage_connection import GitLabOriginPolicy
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
    UNSUPPORTED_PROVIDER = "unsupported_provider"
    UNSUPPORTED_CI_POLICY = "unsupported_ci_policy"


@dataclass(frozen=True)
class WebhookRunIdentity:
    """Detached provider authority; never a credential or a new wire field."""

    provider: str
    origin_provider_installation_id: UUID
    workspace_id: UUID
    repository_id: UUID
    repository_external_id: int
    instance_origin: str | None = None
    credential_generation: int | None = None

    def __post_init__(self) -> None:
        if (
            type(self.provider) is not str
            or self.provider not in {"github", "gitlab"}
            or type(self.origin_provider_installation_id) is not UUID
            or type(self.workspace_id) is not UUID
            or type(self.repository_id) is not UUID
            or type(self.repository_external_id) is not int
            or not 0 < self.repository_external_id <= 9223372036854775807
        ):
            raise ValueError("Invalid webhook run identity")
        if self.provider == "github":
            if self.instance_origin is not None or self.credential_generation is not None:
                raise ValueError("Invalid webhook run identity")
            return
        if (
            type(self.instance_origin) is not str
            or type(self.credential_generation) is not int
            or not 0 < self.credential_generation <= 9223372036854775807
        ):
            raise ValueError("Invalid webhook run identity")
        try:
            # Syntax only; request adapters separately enforce runtime origin trust.
            GitLabOriginPolicy(
                frozenset({self.instance_origin}), frozenset({self.instance_origin})
            ).validate(self.instance_origin)
        except (ValueError, TypeError):
            raise ValueError("Invalid webhook run identity") from None


@dataclass(frozen=True)
class EligibilityCandidate:
    code_change_id: UUID
    installation_external_id: int
    repository_full_name: str
    head_sha: str
    state: PullRequestState
    repository_enabled: bool
    ai_review_labeled: bool | None
    ai_review_labeled_at: datetime | None
    head_first_seen_at: datetime | None
    wait_for_ci: CiWaitMode
    identity: WebhookRunIdentity | None = None


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
    """Shared enqueue decision; provider policy may refuse before any CI request."""

    eligible: bool
    reason: EligibilityReason
    head_sha: str | None
    candidate: EligibilityCandidate | None = field(default=None, compare=False)
    # What blocks (``ci_blocked``) or delays (``waiting_for_ci``) the gate, for the outcome line.
    detail: str | None = None


class EligibilityCandidateStore(Protocol):
    """Each read returns a detached snapshot and closes its DB transaction."""

    async def get(self, code_change_id: UUID) -> EligibilityCandidate | None: ...


type CiProviderCheckpoint = Callable[[], Awaitable[None]]


class _CiCandidateChanged(Exception):
    def __init__(self, decision: CiEligibility) -> None:
        self.decision = decision
        super().__init__(decision.reason.value)


class CurrentHeadCiProvider(Protocol):
    async def get_current_head_ci(
        self,
        installation_external_id: int,
        repository_full_name: str,
        head_sha: str,
        *,
        checkpoint: CiProviderCheckpoint,
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

    async def execute(
        self,
        code_change_id: UUID,
        expected_head_sha: str,
        *,
        expected_installation_external_id: int | None = None,
    ) -> CiEligibility:
        candidate = await self._candidates.get(code_change_id)
        precheck = self._precheck(candidate, expected_head_sha, expected_installation_external_id)
        if precheck is not None:
            return precheck
        assert candidate is not None
        if candidate.wait_for_ci == CiWaitMode.NEVER:
            return CiEligibility(True, EligibilityReason.ELIGIBLE, candidate.head_sha, candidate)

        try:
            snapshot = await self._ci.get_current_head_ci(
                candidate.installation_external_id,
                candidate.repository_full_name,
                candidate.head_sha,
                checkpoint=partial(self._checkpoint, candidate),
            )
        except _CiCandidateChanged as changed:
            return changed.decision
        current = await self._candidates.get(code_change_id)
        precheck = self._precheck(current, expected_head_sha, expected_installation_external_id)
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

    async def _checkpoint(self, candidate: EligibilityCandidate) -> None:
        """Close the detached read before the next request; pin the original source/head."""
        current = await self._candidates.get(candidate.code_change_id)
        decision = self._precheck(current, candidate.head_sha, candidate.installation_external_id)
        if decision is not None:
            raise _CiCandidateChanged(decision)
        if current != candidate:
            raise _CiCandidateChanged(
                CiEligibility(False, EligibilityReason.STALE_STATE, candidate.head_sha)
            )

    @staticmethod
    def _precheck(
        candidate: EligibilityCandidate | None,
        expected_head_sha: str,
        expected_installation_external_id: int | None = None,
    ) -> CiEligibility | None:
        if candidate is None:
            return CiEligibility(False, EligibilityReason.UNKNOWN_PR, None)
        if candidate.identity is not None and candidate.identity.provider != "github":
            return CiEligibility(False, EligibilityReason.UNSUPPORTED_PROVIDER, candidate.head_sha)
        if (
            expected_installation_external_id is not None
            and candidate.installation_external_id != expected_installation_external_id
        ):
            return CiEligibility(False, EligibilityReason.STALE_STATE, candidate.head_sha)
        if candidate.head_sha != expected_head_sha:
            return CiEligibility(False, EligibilityReason.STALE_HEAD, candidate.head_sha)
        if not candidate.repository_enabled:
            return CiEligibility(False, EligibilityReason.DISABLED_REPOSITORY, candidate.head_sha)
        if candidate.state != PullRequestState.OPEN:
            return CiEligibility(False, EligibilityReason.CLOSED_PR, candidate.head_sha)
        if candidate.ai_review_labeled is not True:
            return CiEligibility(False, EligibilityReason.LABEL_NOT_ACTIVE, candidate.head_sha)
        return None


def _blocking_suites_detail(blocking: Sequence[CheckSuite]) -> str:
    """Name the first blocking suite in GitHub's order and count the others.

    A GitHub-supplied status or conclusion goes into the outcome log line only when it is a
    plain token; anything else is logged as ``?`` (so is the commit status above).
    """
    first = blocking[0]
    state = log_token(first.status) or "?"
    if first.conclusion is not None:
        state += f"/{log_token(first.conclusion) or '?'}"
    detail = f"check suite app={first.app_id} {state}"
    if len(blocking) > 1:
        detail += f" (+{len(blocking) - 1} more)"
    return detail
