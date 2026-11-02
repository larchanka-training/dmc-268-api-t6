"""Shared enqueue identity and provider policy; fake ports only, no SQL or broker."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from types import TracebackType
from typing import Any, Self
from uuid import UUID

import pytest

from app.modules.reviews.application.determine_ci_eligibility import (
    CiEligibility,
    CiProviderCheckpoint,
    CiSnapshot,
    CiWaitMode,
    DetermineCiEligibility,
    EligibilityCandidate,
    EligibilityReason,
    WebhookRunIdentity,
)
from app.modules.reviews.application.project_github_pull_request import PullRequestState
from app.modules.reviews.application.try_enqueue_webhook_run import (
    UNSET,
    DuplicateReason,
    EnqueueResult,
    EnqueueStatus,
    PendingRunMessage,
    RunInsertCandidate,
    RunPublicationKind,
    TryEnqueueWebhookRun,
    UnsetType,
)

PR, RUN, ORIGIN, WS, REPO, RULE, PROMPT = (UUID(int=i) for i in range(1, 8))
NOW = datetime(2026, 10, 19, tzinfo=UTC)
HEAD = "a" * 40


def identity(**changes: Any) -> WebhookRunIdentity:
    values: dict[str, Any] = dict(
        provider="gitlab",
        origin_provider_installation_id=ORIGIN,
        workspace_id=WS,
        repository_id=REPO,
        repository_external_id=17,
        instance_origin="https://gitlab.example.com",
        credential_generation=1,
    )
    values.update(changes)
    return WebhookRunIdentity(**values)


def snapshot(target: WebhookRunIdentity | None = None) -> EligibilityCandidate:
    return EligibilityCandidate(
        PR,
        42,
        "team/project",
        HEAD,
        PullRequestState.OPEN,
        True,
        None,
        None,
        None,
        CiWaitMode.NEVER,
        identity=target,
    )


def insert_candidate(target: WebhookRunIdentity | None = None) -> RunInsertCandidate:
    return RunInsertCandidate(
        snapshot(target),
        ORIGIN,
        REPO,
        WS,
        17,
        7,
        "b" * 40,
        "main",
        "fast",
        RULE,
        PROMPT,
        severity_threshold="all",
        max_comments=10,
        conventions_prompt_version_id=None,
        verifier_prompt_version_id=None,
        provider="gitlab",
    )


@dataclass
class CandidateStore:
    candidate: EligibilityCandidate

    async def get(self, code_change_id: UUID) -> EligibilityCandidate:
        return self.candidate


@dataclass
class CiProvider:
    calls: int = 0

    async def get_current_head_ci(
        self,
        installation_external_id: int,
        repository_full_name: str,
        head_sha: str,
        *,
        checkpoint: CiProviderCheckpoint,
    ) -> CiSnapshot:
        self.calls += 1
        await checkpoint()
        return CiSnapshot(HEAD, (), "success", 1)


@dataclass
class Checker:
    decision: CiEligibility

    async def execute(
        self,
        code_change_id: UUID,
        expected_head_sha: str,
        *,
        expected_installation_external_id: int | None = None,
    ) -> CiEligibility:
        return self.decision


@dataclass
class Runs:
    candidate: RunInsertCandidate
    events: list[str]
    inserted: PendingRunMessage | None = None
    published: bool = False

    async def lock_candidate(self, code_change_id: UUID) -> RunInsertCandidate:
        self.events.append("lock")
        return self.candidate

    async def insert_webhook_run(
        self,
        candidate: RunInsertCandidate,
        now: datetime,
        *,
        triggered_by: str | None | UnsetType = UNSET,
    ) -> PendingRunMessage | DuplicateReason:
        self.events.append("insert")
        if self.inserted is not None:
            return DuplicateReason.HEAD_ALREADY_REVIEWED
        self.inserted = PendingRunMessage.from_candidate(RUN, candidate, now)
        return self.inserted

    async def notify_run_updated(self, run_id: UUID, workspace_id: UUID, status: str) -> None:
        self.events.append("notify")
        assert (run_id, workspace_id, status) == (RUN, WS, "queued")

    async def mark_published(self, run_id: UUID, now: datetime) -> None:
        self.events.append("mark")
        self.published = True

    async def pending_messages(self, limit: int) -> tuple[PendingRunMessage, ...]:
        return (self.inserted,) if self.inserted is not None and not self.published else ()


@dataclass
class Uow:
    runs: Runs
    events: list[str]
    active: bool = False

    async def __aenter__(self) -> Self:
        self.active = True
        self.events.append("enter")
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.active = False
        self.events.append("exit")

    async def commit(self) -> None:
        self.events.append("commit")

    async def rollback(self) -> None:
        self.events.append("rollback")


@dataclass
class Publisher:
    uow: Uow
    events: list[str]
    fail: bool = False
    messages: list[PendingRunMessage] = field(default_factory=list)

    async def publish_confirmed(
        self,
        message: PendingRunMessage,
        *,
        kind: RunPublicationKind = RunPublicationKind.QUEUED,
    ) -> None:
        assert not self.uow.active
        self.events.append("publish")
        self.messages.append(message)
        if self.fail:
            raise RuntimeError("synthetic unavailable broker")
        self.events.append("confirmed")


def enqueue(
    read: EligibilityCandidate,
    locked: RunInsertCandidate,
) -> tuple[TryEnqueueWebhookRun, Uow, Publisher]:
    events: list[str] = []
    uow = Uow(Runs(locked, events), events)
    publisher = Publisher(uow, events)
    use_case = TryEnqueueWebhookRun(
        eligibility=Checker(CiEligibility(True, EligibilityReason.ELIGIBLE, HEAD, read)),
        uow_factory=lambda: uow,
        publisher=publisher,
        now=lambda: NOW,
    )
    return use_case, uow, publisher


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("provider", "unknown"),
        ("provider", ["gitlab"]),
        ("provider", None),
        ("origin_provider_installation_id", str(ORIGIN)),
        ("workspace_id", None),
        ("repository_id", str(REPO)),
        ("repository_external_id", True),
        ("repository_external_id", 0),
        ("repository_external_id", 2**63),
        ("credential_generation", None),
        ("credential_generation", True),
        ("credential_generation", 0),
        ("credential_generation", 2**63),
        ("instance_origin", None),
        ("instance_origin", ["secret-canary"]),
        ("instance_origin", "http://gitlab.example.com"),
        ("instance_origin", "https://user:secret-canary@gitlab.example.com"),
        ("instance_origin", "https://gitlab.example.com/path"),
        ("instance_origin", "https://GitLab.example.com"),
    ],
)
def test_shared_identity_refuses_invalid_input_with_static_error(name: str, value: object) -> None:
    with pytest.raises(ValueError, match="^Invalid webhook run identity$") as caught:
        identity(**{name: value})
    assert caught.value.__cause__ is None
    assert "secret-canary" not in str(caught.value)


def test_github_identity_has_no_gitlab_only_fields() -> None:
    assert (
        identity(provider="github", instance_origin=None, credential_generation=None).provider
        == "github"
    )
    with pytest.raises(ValueError, match="^Invalid webhook run identity$"):
        identity(provider="github")


@pytest.mark.parametrize("mode", list(CiWaitMode))
@pytest.mark.parametrize("labeled", [None, True, False])
def test_github_checker_refuses_gitlab_before_any_ci(
    mode: CiWaitMode, labeled: bool | None
) -> None:
    candidate = replace(snapshot(identity()), wait_for_ci=mode, ai_review_labeled=labeled)
    provider = CiProvider()
    checker = DetermineCiEligibility(
        candidates=CandidateStore(candidate), ci=provider, own_app_id=1
    )
    result = asyncio.run(checker.execute(PR, HEAD))
    assert result.reason.value == "unsupported_provider"
    assert result.eligible is False
    assert provider.calls == 0


@pytest.mark.parametrize("mode", list(CiWaitMode))
@pytest.mark.parametrize("with_identity", [False, True])
@pytest.mark.parametrize("labeled", [False, True])
def test_github_policy_controls_remain_compatible(
    mode: CiWaitMode,
    with_identity: bool,
    labeled: bool,
) -> None:
    target = (
        identity(provider="github", instance_origin=None, credential_generation=None)
        if with_identity
        else None
    )
    candidate = replace(snapshot(target), wait_for_ci=mode, ai_review_labeled=labeled)
    provider = CiProvider()
    checker = DetermineCiEligibility(
        candidates=CandidateStore(candidate), ci=provider, own_app_id=1
    )
    result = asyncio.run(checker.execute(PR, HEAD))
    assert result.eligible is labeled
    assert result.reason.value == ("eligible" if labeled else "label_not_active")
    assert provider.calls == (1 if labeled and mode is not CiWaitMode.NEVER else 0)


def test_unsupported_gitlab_policy_has_distinct_reason_and_never_enters_writer() -> None:
    target = identity()
    use_case, uow, publisher = enqueue(snapshot(target), insert_candidate(target))
    refused = TryEnqueueWebhookRun(
        eligibility=Checker(CiEligibility(False, EligibilityReason.UNSUPPORTED_CI_POLICY, HEAD)),
        uow_factory=lambda: uow,
        publisher=publisher,
    )
    result = asyncio.run(refused.execute(PR, HEAD, expected_identity=target))
    assert result == EnqueueResult(EnqueueStatus.INELIGIBLE, reason="unsupported_ci_policy")
    assert uow.events == []
    assert publisher.messages == []


@pytest.mark.parametrize(
    "changes",
    [
        {"provider": "github", "instance_origin": None, "credential_generation": None},
        {"origin_provider_installation_id": UUID(int=100)},
        {"workspace_id": UUID(int=101)},
        {"repository_id": UUID(int=102)},
        {"repository_external_id": 18},
        {"instance_origin": "https://other.example.com"},
        {"credential_generation": 2},
    ],
)
def test_expected_identity_mismatch_refuses_before_insertion_uow(changes: dict[str, Any]) -> None:
    target = identity()
    use_case, uow, publisher = enqueue(snapshot(target), insert_candidate(target))
    result = asyncio.run(use_case.execute(PR, HEAD, expected_identity=identity(**changes)))
    assert result == EnqueueResult(EnqueueStatus.STALE, reason="state_changed")
    assert uow.events == []
    assert publisher.messages == []


@pytest.mark.parametrize(
    "changes",
    [
        {"provider": "github", "instance_origin": None, "credential_generation": None},
        {"origin_provider_installation_id": UUID(int=100)},
        {"workspace_id": UUID(int=101)},
        {"repository_id": UUID(int=102)},
        {"repository_external_id": 18},
        {"instance_origin": "https://other.example.com"},
        {"credential_generation": 2},
    ],
)
def test_read_to_locked_identity_change_is_stale(changes: dict[str, Any]) -> None:
    target = identity()
    locked = replace(insert_candidate(target), ci=snapshot(identity(**changes)))
    use_case, uow, publisher = enqueue(snapshot(target), locked)
    result = asyncio.run(use_case.execute(PR, HEAD, expected_identity=target))
    assert result == EnqueueResult(EnqueueStatus.STALE, reason="state_changed")
    assert uow.events == ["enter", "lock", "exit"]
    assert publisher.messages == []


@pytest.mark.parametrize(
    "changes",
    [
        {"provider": "github"},
        {"origin_provider_installation_id": UUID(int=100)},
        {"workspace_id": UUID(int=101)},
        {"repository_id": UUID(int=102)},
        {"repository_external_id": 18},
    ],
)
def test_outer_candidate_contradiction_never_inserts(changes: dict[str, Any]) -> None:
    target = identity()
    locked = replace(insert_candidate(target), **changes)
    use_case, uow, publisher = enqueue(snapshot(target), locked)
    result = asyncio.run(use_case.execute(PR, HEAD, expected_identity=target))
    assert result == EnqueueResult(EnqueueStatus.STALE, reason="state_changed")
    assert uow.events == ["enter", "lock", "exit"]
    assert publisher.messages == []


@pytest.mark.parametrize("expected", [False, True])
def test_missing_gitlab_identity_fails_closed(expected: bool) -> None:
    use_case, uow, publisher = enqueue(snapshot(), insert_candidate())
    result = asyncio.run(
        use_case.execute(PR, HEAD, expected_identity=identity() if expected else None)
    )
    assert result == EnqueueResult(EnqueueStatus.STALE, reason="state_changed")
    assert "insert" not in uow.events
    assert publisher.messages == []


def test_gitlab_enqueue_commits_before_publish_and_marks_after_confirmation() -> None:
    target = identity()
    use_case, uow, publisher = enqueue(snapshot(target), insert_candidate(target))
    result = asyncio.run(use_case.execute(PR, HEAD, expected_identity=target))
    assert result == EnqueueResult(EnqueueStatus.ENQUEUED, RUN)
    assert uow.events == [
        "enter",
        "lock",
        "insert",
        "notify",
        "commit",
        "exit",
        "publish",
        "confirmed",
        "enter",
        "mark",
        "commit",
        "exit",
    ]
    assert publisher.messages[0].provider == "gitlab"
    assert publisher.messages[0].workspace_id == WS
    assert uow.runs.published


def test_failed_gitlab_publication_remains_pending_and_replay_marks_only_after_confirm() -> None:
    target = identity()
    use_case, uow, publisher = enqueue(snapshot(target), insert_candidate(target))
    publisher.fail = True
    result = asyncio.run(use_case.execute(PR, HEAD, expected_identity=target))
    assert result == EnqueueResult(EnqueueStatus.PUBLICATION_PENDING, RUN)
    assert not uow.runs.published
    assert "mark" not in uow.events
    publisher.fail = False
    assert asyncio.run(use_case.replay_pending_publications()) == 1
    assert uow.runs.published
    assert [message.provider for message in publisher.messages] == ["gitlab", "gitlab"]
    assert uow.events[-5:] == ["confirmed", "enter", "mark", "commit", "exit"]


@pytest.mark.parametrize("provider", ["unknown", None, ["gitlab"], True])
def test_insert_candidate_refuses_unsupported_provider(provider: Any) -> None:
    with pytest.raises(ValueError, match="^Unsupported run provider$"):
        replace(insert_candidate(identity()), provider=provider)


@pytest.mark.parametrize("with_identity", [False, True])
def test_legacy_and_identified_github_enqueue_remain_compatible(with_identity: bool) -> None:
    target = (
        identity(provider="github", instance_origin=None, credential_generation=None)
        if with_identity
        else None
    )
    read = replace(snapshot(target), ai_review_labeled=True)
    locked = replace(insert_candidate(target), ci=read, provider="github")
    use_case, uow, publisher = enqueue(read, locked)
    result = asyncio.run(
        use_case.execute(PR, HEAD, expected_identity=target, expected_installation_external_id=42)
    )
    assert result == EnqueueResult(EnqueueStatus.ENQUEUED, RUN)
    assert publisher.messages[0].provider == "github"
    assert uow.runs.published


@pytest.mark.parametrize(
    "changes",
    [
        {"origin_provider_installation_id": None},
        {"origin_provider_installation_id": str(ORIGIN)},
        {"workspace_id": None},
        {"repository_id": None},
        {"repository_external_id": 0},
        {"repository_external_id": -1},
        {"repository_external_id": 2**63},
        {"repository_external_id": 17.0},
    ],
)
def test_legacy_github_compatibility_never_allows_malformed_outer_identity(
    changes: dict[str, Any],
) -> None:
    read = replace(snapshot(), ai_review_labeled=True)
    locked = replace(insert_candidate(), ci=read, provider="github", **changes)
    use_case, uow, publisher = enqueue(read, locked)
    result = asyncio.run(use_case.execute(PR, HEAD))
    assert result == EnqueueResult(EnqueueStatus.STALE, reason="state_changed")
    assert "insert" not in uow.events
    assert publisher.messages == []


def test_locked_project_boolean_cannot_equal_the_positive_numeric_identity() -> None:
    target = identity(repository_external_id=1)
    locked = replace(insert_candidate(target), repository_external_id=True)
    use_case, uow, publisher = enqueue(snapshot(target), locked)
    result = asyncio.run(use_case.execute(PR, HEAD, expected_identity=target))
    assert result == EnqueueResult(EnqueueStatus.STALE, reason="state_changed")
    assert "insert" not in uow.events
    assert publisher.messages == []


def test_real_gitlab_checker_accepts_label_free_never_snapshot() -> None:
    from app.modules.integrations.gitlab.application.eligibility import DetermineGitLabEligibility

    checker = DetermineGitLabEligibility(candidates=CandidateStore(snapshot(identity())))
    decision = asyncio.run(checker.execute(PR, HEAD))
    assert decision.eligible is True
    assert decision.reason.value == "eligible"
    assert decision.candidate == snapshot(identity())


@pytest.mark.parametrize(
    ("changes", "head", "installation", "reason"),
    [
        ({"identity": None}, HEAD, None, "unsupported_provider"),
        (
            {
                "identity": identity(
                    provider="github", instance_origin=None, credential_generation=None
                )
            },
            HEAD,
            None,
            "unsupported_provider",
        ),
        ({}, "b" * 40, None, "stale_head"),
        ({}, HEAD, 99, "stale_state"),
        ({"repository_enabled": False}, HEAD, None, "disabled_repository"),
        ({"state": PullRequestState.CLOSED}, HEAD, None, "closed_pr"),
        ({"state": PullRequestState.MERGED}, HEAD, None, "closed_pr"),
        ({"wait_for_ci": CiWaitMode.AUTO}, HEAD, None, "unsupported_ci_policy"),
        ({"wait_for_ci": CiWaitMode.ALWAYS}, HEAD, None, "unsupported_ci_policy"),
    ],
)
def test_real_gitlab_checker_refusals(
    changes: dict[str, Any], head: str, installation: int | None, reason: str
) -> None:
    from app.modules.integrations.gitlab.application.eligibility import DetermineGitLabEligibility

    checker = DetermineGitLabEligibility(
        candidates=CandidateStore(replace(snapshot(identity()), **changes))
    )
    decision = asyncio.run(
        checker.execute(PR, head, expected_installation_external_id=installation)
    )
    assert (decision.eligible, decision.reason.value, decision.candidate) == (False, reason, None)


def test_real_gitlab_checker_missing_is_distinct_from_operational_failure() -> None:
    from app.modules.integrations.gitlab.application.eligibility import DetermineGitLabEligibility
    from app.modules.integrations.gitlab.application.request_credentials import (
        GitLabRequestUnavailable,
    )

    class Missing:
        async def get(self, code_change_id: UUID) -> None:
            return None

    class Unavailable:
        async def get(self, code_change_id: UUID) -> None:
            raise GitLabRequestUnavailable()

    assert (
        asyncio.run(DetermineGitLabEligibility(candidates=Missing()).execute(PR, HEAD)).reason.value
        == "unknown_pr"
    )
    with pytest.raises(GitLabRequestUnavailable, match="^gitlab_request_unavailable$"):
        asyncio.run(DetermineGitLabEligibility(candidates=Unavailable()).execute(PR, HEAD))
