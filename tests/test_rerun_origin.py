"""Rerun authority policy is decided by the use case over raw locked facts."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Self, cast
from uuid import UUID

import pytest

from app.modules.reviews.application.determine_ci_eligibility import (
    CiWaitMode,
    EligibilityCandidate,
)
from app.modules.reviews.application.list_runs import RunListItem
from app.modules.reviews.application.project_github_pull_request import PullRequestState
from app.modules.reviews.application.rerun_run import (
    RerunConflict,
    RerunFacts,
    RerunNotConfigured,
    RerunOriginConflict,
    RerunOriginFacts,
    RerunRun,
)
from app.modules.reviews.application.try_enqueue_webhook_run import (
    CandidateMiss,
    PendingRunMessage,
    RunInsertCandidate,
    RunPublicationKind,
)

RUN = UUID(int=1)
NOW = datetime(2026, 10, 26, tzinfo=UTC)


def facts() -> RerunFacts:
    candidate = RunInsertCandidate(
        ci=EligibilityCandidate(
            UUID(int=2),
            17,
            "team/repo",
            "a" * 40,
            PullRequestState.OPEN,
            True,
            True,
            NOW,
            NOW,
            CiWaitMode.NEVER,
        ),
        origin_provider_installation_id=UUID(int=3),
        repository_id=UUID(int=4),
        workspace_id=UUID(int=5),
        repository_external_id=101,
        pr_number=7,
        base_sha="b" * 40,
        base_ref="main",
        engine="fast",
        rule_version_id=UUID(int=6),
        prompt_version_id=UUID(int=7),
        severity_threshold="warning",
        max_comments=3,
        conventions_prompt_version_id=UUID(int=8),
        verifier_prompt_version_id=UUID(int=9),
        provider="github",
    )
    return RerunFacts(
        UUID(int=2),
        UUID(int=4),
        UUID(int=4),
        RerunOriginFacts(UUID(int=3), "github", UUID(int=5), 700, False),
        candidate,
        RerunOriginFacts(UUID(int=3), "github", UUID(int=5), 700, False),
        UUID(int=4),
        True,
        False,
    )


@dataclass
class FakeRerun:
    value: RerunFacts | None = field(default_factory=facts)
    active: bool = False
    inserted: list[tuple[RunInsertCandidate, UUID, datetime]] = field(default_factory=list)
    commits: int = 0
    published: list[PendingRunMessage] = field(default_factory=list)
    marked: list[UUID] = field(default_factory=list)
    item: RunListItem = field(default_factory=lambda: cast(RunListItem, object()))

    @property
    def runs(self) -> FakeRerun:
        return self

    async def __aenter__(self) -> Self:
        assert not self.active
        self.active = True
        return self

    async def __aexit__(self, *args: object) -> None:
        self.active = False

    async def rollback(self) -> None:
        pass

    async def commit(self) -> None:
        assert self.active
        self.commits += 1

    async def load_rerun_facts(self, run_id: UUID) -> RerunFacts | None:
        assert self.active and run_id == RUN
        return self.value

    async def insert_rerun(
        self, candidate: RunInsertCandidate, code_change_id: UUID, now: datetime
    ) -> PendingRunMessage:
        assert self.active
        self.inserted.append((candidate, code_change_id, now))
        return PendingRunMessage.from_candidate(UUID(int=10), candidate, now)

    async def mark_rerun_published(self, run_id: UUID, now: datetime) -> None:
        assert self.active
        self.marked.append(run_id)

    async def get_run(self, run_id: UUID) -> RunListItem:
        assert not self.active and run_id == UUID(int=10)
        return self.item

    async def publish_confirmed(
        self, message: PendingRunMessage, *, kind: RunPublicationKind = RunPublicationKind.QUEUED
    ) -> None:
        assert not self.active  # Broker calls stay outside the creation transaction.
        self.published.append(message)


def execute(store: FakeRerun) -> RunListItem | None:
    return asyncio.run(RerunRun(lambda: store, store, store, now=lambda: NOW).execute(RUN))


@pytest.mark.parametrize("reinstalled", [False, True])
def test_rerun_usecase_accepts_same_origin_and_retired_same_account_github(
    reinstalled: bool,
) -> None:
    value = facts()
    if reinstalled:
        value = replace(
            value, origin=replace(value.origin, installation_id=UUID(int=11), retired=True)
        )
    store = FakeRerun(value)
    assert execute(store) is store.item
    assert len(store.inserted) == 1
    candidate, code_change, now = store.inserted[0]
    assert candidate is value.candidate and code_change == UUID(int=2) and now == NOW
    assert store.commits == 2 and store.marked == [UUID(int=10)]
    assert len(store.published) == 1


@pytest.mark.parametrize(
    "mutation",
    [
        "account",
        "original_account_unknown",
        "current_account_unknown",
        "workspace",
        "repository",
        "historical_rule_repository",
        "original_provider",
        "current_provider",
        "old_still_live",
        "missing_origin",
        "gitlab_rebind",
        "gitlab_retired",
        "authority_unavailable",
    ],
)
def test_rerun_usecase_refuses_changed_authority_without_persist_commit_or_broker(
    mutation: str,
) -> None:
    value = facts()
    assert isinstance(value.candidate, RunInsertCandidate)
    value = replace(value, origin=replace(value.origin, installation_id=UUID(int=11), retired=True))
    current = value.current_origin
    assert current is not None
    candidate = value.candidate
    assert isinstance(candidate, RunInsertCandidate)
    if mutation == "account":
        value = replace(value, current_origin=replace(current, account_external_id=701))
    elif mutation == "original_account_unknown":
        value = replace(value, origin=replace(value.origin, account_external_id=None))
    elif mutation == "current_account_unknown":
        value = replace(value, current_origin=replace(current, account_external_id=None))
    elif mutation == "workspace":
        value = replace(value, candidate=replace(candidate, workspace_id=UUID(int=50)))
    elif mutation == "repository":
        value = replace(value, candidate=replace(candidate, repository_id=UUID(int=40)))
    elif mutation == "historical_rule_repository":
        value = replace(value, rule_repository_id=UUID(int=41))
    elif mutation == "original_provider":
        value = replace(value, origin=replace(value.origin, provider="gitlab"))
    elif mutation == "current_provider":
        value = replace(value, candidate=replace(candidate, provider="gitlab"))
    elif mutation == "old_still_live":
        value = replace(value, origin=replace(value.origin, retired=False))
    elif mutation == "missing_origin":
        value = replace(value, origin=replace(value.origin, installation_id=None))
    elif mutation == "gitlab_rebind":
        value = replace(
            value,
            origin=replace(value.origin, provider="gitlab"),
            candidate=replace(candidate, provider="gitlab"),
        )
    elif mutation == "gitlab_retired":
        value = replace(
            value,
            origin=replace(value.origin, provider="gitlab"),
            candidate=CandidateMiss.MISSING_INSTALLATION,
        )
    else:
        value = replace(value, candidate=CandidateMiss.MISSING_INSTALLATION)
    assert value.current_origin is not None
    if mutation == "workspace":
        value = replace(value, current_origin=replace(current, workspace_id=UUID(int=50)))
    elif mutation == "repository":
        value = replace(value, current_repository_id=UUID(int=40))
    elif mutation in {"current_provider", "gitlab_rebind"}:
        value = replace(value, current_origin=replace(current, provider="gitlab"))
    store = FakeRerun(value)
    with pytest.raises(RerunOriginConflict):
        execute(store)
    assert store.inserted == [] and store.commits == 0
    assert store.published == [] and store.marked == []


@pytest.mark.parametrize("reason", ["missing", "rules", "prompt", "closed", "active", "paused"])
def test_rerun_usecase_preserves_missing_configuration_and_conflict_precedence(reason: str) -> None:
    value = facts()
    assert isinstance(value.candidate, RunInsertCandidate)
    if reason == "rules":
        value = replace(value, candidate=CandidateMiss.MISSING_RULES)
    elif reason == "prompt":
        value = replace(value, candidate=CandidateMiss.MISSING_PROMPT)
    elif reason == "closed":
        value = replace(value, pull_request_open=False, candidate=CandidateMiss.MISSING_RULES)
    elif reason == "active":
        value = replace(value, active_run=True)
    elif reason == "paused":
        assert isinstance(value.candidate, RunInsertCandidate)
        value = replace(
            value,
            candidate=replace(
                value.candidate, ci=replace(value.candidate.ci, repository_enabled=False)
            ),
        )
    store = FakeRerun(None if reason == "missing" else value)
    if reason == "missing":
        assert execute(store) is None
    else:
        with pytest.raises(RerunNotConfigured if reason in {"rules", "prompt"} else RerunConflict):
            execute(store)
    assert store.inserted == [] and store.commits == 0
    assert store.published == [] and store.marked == []


@pytest.mark.parametrize("missing", [CandidateMiss.MISSING_RULES, CandidateMiss.MISSING_PROMPT])
@pytest.mark.parametrize("changed", ["account", "provider", "workspace", "repository"])
def test_missing_configuration_does_not_mask_changed_authority(
    missing: CandidateMiss, changed: str
) -> None:
    value = facts()
    assert value.current_origin is not None
    value = replace(
        value,
        origin=replace(value.origin, installation_id=UUID(int=11), retired=True),
        candidate=missing,
    )
    current = value.current_origin
    assert current is not None
    if changed == "account":
        value = replace(value, current_origin=replace(current, account_external_id=701))
    elif changed == "provider":
        value = replace(value, current_origin=replace(current, provider="gitlab"))
    elif changed == "workspace":
        value = replace(value, current_origin=replace(current, workspace_id=UUID(int=50)))
    else:
        value = replace(value, current_repository_id=UUID(int=40))
    store = FakeRerun(value)
    with pytest.raises(RerunOriginConflict):
        execute(store)
    assert store.inserted == [] and store.commits == 0 and store.published == []


def test_rerun_usecase_accepts_same_live_gitlab_origin() -> None:
    value = facts()
    assert isinstance(value.candidate, RunInsertCandidate)
    assert value.current_origin is not None
    value = replace(
        value,
        origin=replace(value.origin, provider="gitlab"),
        current_origin=replace(value.current_origin, provider="gitlab"),
        candidate=replace(value.candidate, provider="gitlab"),
    )
    store = FakeRerun(value)
    assert execute(store) is store.item
    assert len(store.inserted) == 1 and store.commits == 2
    assert len(store.published) == 1 and store.marked == [UUID(int=10)]
