"""One webhook Run per PR/head, post-commit publish, and durable cancellation."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Iterator
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import Self
from uuid import UUID, uuid4

import psycopg
import pytest
from alembic.config import Config
from sqlalchemy import create_engine, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.schema import CreateSchema, DropSchema

from alembic import command
from app.common.infrastructure.db.enums import CodeChangeState, Engine, RunState, WaitForCi
from app.modules.repositories.infrastructure.models import Repository
from app.modules.reviews.application.determine_ci_eligibility import (
    CiEligibility,
    CiSnapshot,
    CiWaitMode,
    DetermineCiEligibility,
    EligibilityCandidate,
    EligibilityReason,
)
from app.modules.reviews.application.project_github_pull_request import (
    ProjectGitHubPullRequest,
    PullRequestEvent,
    PullRequestLabelEvent,
    PullRequestState,
)
from app.modules.reviews.application.sweep_no_ci import SweepNoCi
from app.modules.reviews.application.trigger_from_delivery import (
    CiTriggerEvent,
    ProjectedPullRequestTarget,
    TriggerFromDelivery,
)
from app.modules.reviews.application.try_enqueue_webhook_run import (
    CandidateMiss,
    DuplicateReason,
    EnqueueResult,
    EnqueueStatus,
    PendingRunMessage,
    RunInsertCandidate,
    RunPublicationKind,
    TryEnqueueWebhookRun,
)
from app.modules.reviews.infrastructure.ci_eligibility_candidates import (
    SqlAlchemyEligibilityCandidateStore,
)
from app.modules.reviews.infrastructure.github_pull_request_projection import (
    SqlAlchemyPullRequestProjectionUnitOfWork,
)
from app.modules.reviews.infrastructure.models import CodeChange, Run
from app.modules.reviews.infrastructure.no_ci_sweep_candidates import SqlAlchemyDueNoCiCandidates
from app.modules.reviews.infrastructure.webhook_run_targets import SqlAlchemyWebhookRunTargets
from app.modules.reviews.infrastructure.webhook_runs import SqlAlchemyWebhookRunUnitOfWork

_PR = UUID("11111111-1111-1111-1111-111111111111")
_RUN = UUID("22222222-2222-2222-2222-222222222222")
_REPO = UUID("33333333-3333-3333-3333-333333333333")
_WS = UUID("44444444-4444-4444-4444-444444444444")
_RULE = UUID("55555555-5555-5555-5555-555555555555")
_PROMPT = UUID("66666666-6666-6666-6666-666666666666")
_HEAD = "a" * 40
_BASE = "b" * 40
_NOW = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)


def _ci_candidate() -> EligibilityCandidate:
    return EligibilityCandidate(
        _PR,
        17,
        "octo/repo",
        _HEAD,
        PullRequestState.OPEN,
        True,
        True,
        _NOW,
        _NOW,
        CiWaitMode.ALWAYS,
    )


def _insert_candidate() -> RunInsertCandidate:
    return RunInsertCandidate(
        ci=_ci_candidate(),
        repository_id=_REPO,
        workspace_id=_WS,
        repository_external_id=101,
        pr_number=7,
        base_sha=_BASE,
        base_ref="main",
        engine="fast",
        rule_version_id=_RULE,
        prompt_version_id=_PROMPT,
    )


@dataclass
class Eligibility:
    calls: int = 0
    decision: CiEligibility = field(
        default_factory=lambda: CiEligibility(
            True, EligibilityReason.ELIGIBLE, _HEAD, _ci_candidate()
        )
    )

    async def execute(self, code_change_id: UUID, expected_head_sha: str) -> CiEligibility:
        self.calls += 1
        return self.decision


@dataclass
class Runs:
    candidate: RunInsertCandidate | CandidateMiss = field(default_factory=_insert_candidate)
    pending: PendingRunMessage | None = None
    duplicate: DuplicateReason = DuplicateReason.ACTIVE_RUN
    published: bool = False
    notifications: list[tuple[UUID, UUID, str]] = field(default_factory=list)

    async def lock_candidate(self, code_change_id: UUID) -> RunInsertCandidate | CandidateMiss:
        return self.candidate

    async def insert_webhook_run(
        self, candidate: RunInsertCandidate, now: datetime
    ) -> PendingRunMessage | DuplicateReason:
        if self.pending is not None:
            return self.duplicate
        self.pending = PendingRunMessage.from_candidate(_RUN, candidate, now)
        return self.pending

    async def mark_published(self, run_id: UUID, now: datetime) -> None:
        assert run_id == _RUN
        self.published = True

    async def notify_run_updated(self, run_id: UUID, workspace_id: UUID, status: str) -> None:
        self.notifications.append((run_id, workspace_id, status))

    async def pending_messages(self, limit: int) -> tuple[PendingRunMessage, ...]:
        return (self.pending,) if self.pending is not None and not self.published else ()


@dataclass
class Uow:
    store: Runs = field(default_factory=Runs)
    active: bool = False
    commits: int = 0

    @property
    def runs(self) -> Runs:
        return self.store

    async def __aenter__(self) -> Self:
        self.active = True
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.active = False

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        pass


@dataclass
class Publisher:
    uow: Uow
    fail: bool = False
    messages: list[PendingRunMessage] = field(default_factory=list)

    async def publish_confirmed(
        self, message: PendingRunMessage, *, kind: RunPublicationKind = RunPublicationKind.QUEUED
    ) -> None:
        assert kind is RunPublicationKind.QUEUED
        assert self.uow.active is False
        self.messages.append(message)
        if self.fail:
            raise RuntimeError("publisher confirm unavailable")


def test_enqueue_pins_run_message_and_publishes_only_after_commit() -> None:
    uow = Uow()
    publisher = Publisher(uow)
    use_case = TryEnqueueWebhookRun(
        eligibility=Eligibility(), uow_factory=lambda: uow, publisher=publisher, now=lambda: _NOW
    )

    result = asyncio.run(use_case.execute(_PR, _HEAD))

    assert result.status == EnqueueStatus.ENQUEUED
    assert result.run_id == _RUN
    assert result.reason is None
    assert uow.commits == 2
    assert uow.store.published is True
    assert uow.store.notifications == [(_RUN, _WS, "queued")]
    assert publisher.messages[0].run_id == _RUN
    assert publisher.messages[0].attempt == 1
    assert publisher.messages[0].head_sha == _HEAD
    assert publisher.messages[0].base_sha == _BASE
    assert publisher.messages[0].rule_version_id == _RULE
    assert publisher.messages[0].prompt_version_id == _PROMPT


def test_publication_pointer_contains_typed_run_data_without_wire_encoding() -> None:
    message = PendingRunMessage.from_candidate(_RUN, _insert_candidate(), _NOW)

    assert message.run_id == _RUN
    assert message.workspace_id == _WS
    assert message.repository_id == _REPO
    assert message.requested_at == _NOW
    assert not hasattr(message, "as_payload")
    assert not hasattr(message, "schema")
    assert not hasattr(message, "message_id")
    assert not hasattr(message, "trigger")


def test_failed_publish_remains_replayable_and_duplicate_does_not_create_second_run() -> None:
    uow = Uow()
    publisher = Publisher(uow, fail=True)
    use_case = TryEnqueueWebhookRun(
        eligibility=Eligibility(), uow_factory=lambda: uow, publisher=publisher, now=lambda: _NOW
    )

    first = asyncio.run(use_case.execute(_PR, _HEAD))
    duplicate = asyncio.run(use_case.execute(_PR, _HEAD))

    assert first.status == EnqueueStatus.PUBLICATION_PENDING
    assert duplicate.status == EnqueueStatus.DUPLICATE
    assert uow.store.published is False
    publisher.fail = False
    replayed = asyncio.run(use_case.replay_pending_publications())
    assert replayed == 1
    assert uow.store.published is True
    assert [message.run_id for message in publisher.messages] == [_RUN, _RUN]


def _enqueue(
    *, eligibility: Eligibility | None = None, store: Runs | None = None
) -> tuple[TryEnqueueWebhookRun, Uow]:
    uow = Uow(store=store if store is not None else Runs())
    use_case = TryEnqueueWebhookRun(
        eligibility=eligibility if eligibility is not None else Eligibility(),
        uow_factory=lambda: uow,
        publisher=Publisher(uow),
        now=lambda: _NOW,
    )
    return use_case, uow


def test_an_ineligible_decision_reports_the_gate_reason() -> None:
    blocked = CiEligibility(False, EligibilityReason.CI_BLOCKED, _HEAD)
    use_case, uow = _enqueue(eligibility=Eligibility(decision=blocked))

    result = asyncio.run(use_case.execute(_PR, _HEAD))

    assert result == EnqueueResult(EnqueueStatus.INELIGIBLE, reason="ci_blocked")
    assert uow.commits == 0


@pytest.mark.parametrize(
    ("miss", "status"),
    [
        (CandidateMiss.MISSING_INSTALLATION, EnqueueStatus.UNCONFIGURED),
        (CandidateMiss.MISSING_RULES, EnqueueStatus.UNCONFIGURED),
        (CandidateMiss.MISSING_PROMPT, EnqueueStatus.UNCONFIGURED),
        (CandidateMiss.PULL_REQUEST_GONE, EnqueueStatus.STALE),
        (CandidateMiss.REPOSITORY_GONE, EnqueueStatus.STALE),
    ],
)
def test_a_missing_candidate_names_what_is_missing(
    miss: CandidateMiss, status: EnqueueStatus
) -> None:
    use_case, uow = _enqueue(store=Runs(candidate=miss))

    result = asyncio.run(use_case.execute(_PR, _HEAD))

    assert result == EnqueueResult(status, reason=miss.value)
    assert uow.commits == 0
    assert uow.store.pending is None


def test_a_candidate_that_moved_since_the_decision_is_stale_with_state_changed() -> None:
    moved = replace(_insert_candidate(), ci=replace(_ci_candidate(), head_sha="c" * 40))
    use_case, uow = _enqueue(store=Runs(candidate=moved))

    result = asyncio.run(use_case.execute(_PR, _HEAD))

    assert result == EnqueueResult(EnqueueStatus.STALE, reason="state_changed")
    assert uow.store.pending is None


@pytest.mark.parametrize("reason", list(DuplicateReason))
def test_a_duplicate_reports_why_no_second_run_was_created(reason: DuplicateReason) -> None:
    pending = PendingRunMessage.from_candidate(_RUN, _insert_candidate(), _NOW)
    use_case, uow = _enqueue(store=Runs(pending=pending, duplicate=reason))

    result = asyncio.run(use_case.execute(_PR, _HEAD))

    assert result == EnqueueResult(EnqueueStatus.DUPLICATE, reason=reason.value)
    assert uow.commits == 0


def test_repeated_ci_delivery_routes_to_one_run() -> None:
    @dataclass
    class Targets:
        async def for_pr(self, event: PullRequestEvent) -> ProjectedPullRequestTarget:
            return ProjectedPullRequestTarget(_PR, _HEAD)

        async def for_ci(self, event: CiTriggerEvent) -> tuple[UUID, ...]:
            assert event == CiTriggerEvent(17, 101, _HEAD)
            return (_PR,)

    uow = Uow()
    publisher = Publisher(uow)
    trigger = TriggerFromDelivery(
        targets=Targets(),
        enqueuer=TryEnqueueWebhookRun(
            eligibility=Eligibility(),
            uow_factory=lambda: uow,
            publisher=publisher,
            now=lambda: _NOW,
        ),
    )

    asyncio.run(trigger.on_ci(CiTriggerEvent(17, 101, _HEAD)))
    asyncio.run(trigger.on_ci(CiTriggerEvent(17, 101, _HEAD)))

    assert uow.commits == 2
    assert [message.run_id for message in publisher.messages] == [_RUN]


def test_delayed_label_enqueues_projected_head_instead_of_webhook_head() -> None:
    current_head = "c" * 40
    observed: list[tuple[UUID, str]] = []

    class Targets:
        async def for_pr(self, event: PullRequestEvent) -> ProjectedPullRequestTarget:
            assert event.head_sha == _HEAD
            return ProjectedPullRequestTarget(_PR, current_head)

        async def for_ci(self, event: CiTriggerEvent) -> tuple[UUID, ...]:
            return ()

    class Enqueuer:
        async def execute(self, code_change_id: UUID, expected_head_sha: str) -> EnqueueResult:
            observed.append((code_change_id, expected_head_sha))
            return EnqueueResult(EnqueueStatus.ENQUEUED)

    event = PullRequestEvent(
        action="labeled",
        installation_external_id=17,
        repository_external_id=101,
        external_id=901,
        number=7,
        title="Review parser",
        description=None,
        author_login="alice",
        web_url="https://github.com/octo/repo/pull/7",
        source_branch="feature",
        target_branch="main",
        base_sha=_BASE,
        head_sha=_HEAD,
        state=PullRequestState.OPEN,
        provider_updated_at=_NOW,
    )

    asyncio.run(
        TriggerFromDelivery(targets=Targets(), enqueuer=Enqueuer()).on_label(
            PullRequestLabelEvent(event, "ai-review")
        )
    )

    assert observed == [(_PR, current_head)]


def _outcome_event(action: str = "labeled") -> PullRequestEvent:
    return PullRequestEvent(
        action=action,
        installation_external_id=17,
        repository_external_id=101,
        external_id=901,
        number=7,
        title="Review parser",
        description=None,
        author_login="alice",
        web_url="https://github.com/octo/repo/pull/7",
        source_branch="feature",
        target_branch="main",
        base_sha=_BASE,
        head_sha=_HEAD,
        state=PullRequestState.OPEN,
        provider_updated_at=_NOW,
    )


@dataclass
class OutcomeTargets:
    pull_request: ProjectedPullRequestTarget | None = ProjectedPullRequestTarget(_PR, _HEAD)
    ci: tuple[UUID, ...] = (_PR,)

    async def for_pr(self, event: PullRequestEvent) -> ProjectedPullRequestTarget | None:
        return self.pull_request

    async def for_ci(self, event: CiTriggerEvent) -> tuple[UUID, ...]:
        return self.ci


@dataclass
class OutcomeEnqueuer:
    results: list[EnqueueResult]
    calls: list[tuple[UUID, str]] = field(default_factory=list)

    async def execute(self, code_change_id: UUID, expected_head_sha: str) -> EnqueueResult:
        self.calls.append((code_change_id, expected_head_sha))
        return self.results.pop(0)


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        (
            EnqueueResult(EnqueueStatus.ENQUEUED, _RUN),
            f"pr={_PR} head=aaaaaaa: enqueued run={_RUN}",
        ),
        (
            EnqueueResult(EnqueueStatus.PUBLICATION_PENDING, _RUN),
            f"pr={_PR} head=aaaaaaa: publication_pending run={_RUN}",
        ),
        (
            EnqueueResult(EnqueueStatus.INELIGIBLE, reason="ci_blocked"),
            f"pr={_PR} head=aaaaaaa: ineligible (ci_blocked)",
        ),
        (
            EnqueueResult(EnqueueStatus.UNCONFIGURED, reason="missing_rules"),
            f"pr={_PR} head=aaaaaaa: unconfigured (missing_rules)",
        ),
        (
            EnqueueResult(EnqueueStatus.STALE, reason="state_changed"),
            f"pr={_PR} head=aaaaaaa: stale (state_changed)",
        ),
        (
            EnqueueResult(EnqueueStatus.DUPLICATE, reason="active_run"),
            f"pr={_PR} head=aaaaaaa: duplicate (active_run)",
        ),
    ],
)
def test_pr_and_label_triggers_report_the_enqueue_outcome(
    result: EnqueueResult, expected: str
) -> None:
    enqueuer = OutcomeEnqueuer([result, result])
    trigger = TriggerFromDelivery(targets=OutcomeTargets(), enqueuer=enqueuer)

    assert asyncio.run(trigger.on_pr(_outcome_event("synchronize"))) == expected
    assert (
        asyncio.run(trigger.on_label(PullRequestLabelEvent(_outcome_event(), "ai-review")))
        == expected
    )
    assert enqueuer.calls == [(_PR, _HEAD), (_PR, _HEAD)]


def test_a_run_whose_publish_failed_is_reported_as_publication_pending_with_its_run() -> None:
    uow = Uow()
    trigger = TriggerFromDelivery(
        targets=OutcomeTargets(),
        enqueuer=TryEnqueueWebhookRun(
            eligibility=Eligibility(),
            uow_factory=lambda: uow,
            publisher=Publisher(uow, fail=True),
            now=lambda: _NOW,
        ),
    )

    outcome = asyncio.run(trigger.on_pr(_outcome_event("synchronize")))

    assert outcome == f"pr={_PR} head=aaaaaaa: publication_pending run={_RUN}"


def test_a_pr_with_no_open_row_is_reported_without_enqueueing() -> None:
    enqueuer = OutcomeEnqueuer([])
    trigger = TriggerFromDelivery(targets=OutcomeTargets(pull_request=None), enqueuer=enqueuer)

    assert asyncio.run(trigger.on_pr(_outcome_event("reopened"))) == "no open pull request"
    assert (
        asyncio.run(trigger.on_label(PullRequestLabelEvent(_outcome_event(), "ai-review")))
        == "no open pull request"
    )
    assert enqueuer.calls == []


@pytest.mark.parametrize(
    ("label", "action"), [("bug", "labeled"), ("ai-review", "unlabeled"), ("AI-review", "labeled")]
)
def test_a_foreign_label_or_action_is_reported_without_enqueueing(label: str, action: str) -> None:
    enqueuer = OutcomeEnqueuer([])
    trigger = TriggerFromDelivery(targets=OutcomeTargets(), enqueuer=enqueuer)

    outcome = asyncio.run(trigger.on_label(PullRequestLabelEvent(_outcome_event(action), label)))

    assert outcome == "not an ai-review labeled action"
    assert enqueuer.calls == []


def test_a_ci_event_reports_every_target_and_the_absence_of_one() -> None:
    other = UUID("77777777-7777-7777-7777-777777777777")
    enqueuer = OutcomeEnqueuer(
        [
            EnqueueResult(EnqueueStatus.ENQUEUED, _RUN),
            EnqueueResult(EnqueueStatus.INELIGIBLE, reason="waiting_for_ci"),
        ]
    )
    trigger = TriggerFromDelivery(targets=OutcomeTargets(ci=(_PR, other)), enqueuer=enqueuer)
    event = CiTriggerEvent(17, 101, _HEAD, "check_suite")

    outcome = asyncio.run(trigger.on_ci(event))

    assert outcome == (
        f"pr={_PR} head=aaaaaaa: enqueued run={_RUN}; "
        f"pr={other} head=aaaaaaa: ineligible (waiting_for_ci)"
    )
    assert enqueuer.calls == [(_PR, _HEAD), (other, _HEAD)]
    nobody = TriggerFromDelivery(targets=OutcomeTargets(ci=()), enqueuer=OutcomeEnqueuer([]))
    assert asyncio.run(nobody.on_ci(event)) == "no open pull request at this head"


@pytest.fixture
def webhook_run_database() -> Iterator[tuple[str, str]]:
    database_url = os.environ.get("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("set TEST_DATABASE_URL to run PostgreSQL integration tests")
    schema = f"test_webhook_run_{uuid4().hex}"
    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            connection.execute(CreateSchema(schema))
            connection.execute(text(f'SET search_path TO "{schema}"'))
            connection.commit()
            config = Config("alembic.ini")
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
            connection.execute(
                text(
                    "INSERT INTO workspaces (id, name, daily_budget_usd) "
                    "VALUES (:id, 'Webhook run test', 0)"
                ),
                {"id": _WS},
            )
            connection.execute(
                text(
                    "INSERT INTO provider_installations "
                    "(id, workspace_id, provider, external_id, metadata) "
                    "VALUES (:id, :workspace_id, 'github', 17, '{}'::jsonb)"
                ),
                {"id": uuid4(), "workspace_id": _WS},
            )
            installation_id = connection.execute(
                text("SELECT id FROM provider_installations WHERE external_id = 17")
            ).scalar_one()
            connection.execute(
                text(
                    "INSERT INTO repositories "
                    "(id, provider_installation_id, external_id, full_name, "
                    "default_branch, web_url, wait_for_ci) "
                    "VALUES (:id, :installation_id, 101, 'octo/repo', 'main', "
                    "'https://github.com/octo/repo', 'always')"
                ),
                {"id": _REPO, "installation_id": installation_id},
            )
            connection.execute(
                text(
                    "INSERT INTO prompt_versions "
                    "(id, key, version, content, checksum, is_active) "
                    "VALUES (:id, 'review.system', 1, 'system', :checksum, true)"
                ),
                {"id": _PROMPT, "checksum": "a" * 64},
            )
            connection.execute(
                text(
                    "INSERT INTO rule_versions "
                    "(id, repository_id, version, rules, checksum, is_active) "
                    "VALUES (:id, :repository_id, 1, '[]'::jsonb, :checksum, true)"
                ),
                {"id": _RULE, "repository_id": _REPO, "checksum": "b" * 64},
            )
            connection.execute(
                text(
                    "INSERT INTO code_changes "
                    "(id, repository_id, external_id, external_number, title, source_branch, "
                    "target_branch, base_sha, head_sha, state, web_url, reviewer_requested, "
                    "reviewer_requested_at, ai_review_labeled, ai_review_labeled_at, "
                    "head_first_seen_at, provider_updated_at) "
                    "VALUES (:id, :repository_id, 901, 7, 'Test PR', 'feature', 'main', "
                    ":base_sha, :head_sha, 'open', 'https://github.com/octo/repo/pull/7', "
                    "true, :now, true, :now, :now, :now)"
                ),
                {
                    "id": _PR,
                    "repository_id": _REPO,
                    "base_sha": _BASE,
                    "head_sha": _HEAD,
                    "now": _NOW,
                },
            )
            connection.commit()
            yield database_url, schema
            connection.rollback()
            connection.execute(text("SET search_path TO public"))
            connection.execute(DropSchema(schema, cascade=True))
            connection.commit()
    finally:
        engine.dispose()


@pytest.mark.integration
def test_postgres_enqueue_race_terminal_duplicate_rollback_and_notify(
    webhook_run_database: tuple[str, str],
) -> None:
    database_url, schema = webhook_run_database

    @dataclass
    class ConfirmedPublisher:
        messages: list[PendingRunMessage] = field(default_factory=list)

        async def publish_confirmed(
            self,
            message: PendingRunMessage,
            *,
            kind: RunPublicationKind = RunPublicationKind.QUEUED,
        ) -> None:
            assert kind is RunPublicationKind.QUEUED
            self.messages.append(message)

    async def exercise() -> UUID:
        engine = create_async_engine(
            database_url, connect_args={"options": f"-csearch_path={schema}"}
        )
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        publisher = ConfirmedPublisher()
        try:
            # An uncommitted insert and pg_notify must both disappear on rollback.
            async with SqlAlchemyWebhookRunUnitOfWork(sessions) as uow:
                candidate = await uow.runs.lock_candidate(_PR)
                assert isinstance(candidate, RunInsertCandidate)
                assert candidate == _insert_candidate()
                message = await uow.runs.insert_webhook_run(candidate, _NOW)
                assert isinstance(message, PendingRunMessage)
                await uow.runs.notify_run_updated(message.run_id, _WS, "queued")
            async with sessions() as session:
                assert await session.scalar(select(Run.id)) is None

            trigger = TryEnqueueWebhookRun(
                eligibility=Eligibility(),
                uow_factory=lambda: SqlAlchemyWebhookRunUnitOfWork(sessions),
                publisher=publisher,
                now=lambda: _NOW,
            )
            results = await asyncio.gather(trigger.execute(_PR, _HEAD), trigger.execute(_PR, _HEAD))
            assert sorted(result.status.value for result in results) == ["duplicate", "enqueued"]
            # The loser waits on the PR row lock and then finds the winner's active Run.
            assert [r.reason for r in results if r.status == EnqueueStatus.DUPLICATE] == [
                "active_run"
            ]
            async with sessions() as session:
                rows = (await session.scalars(select(Run))).all()
                assert len(rows) == 1
                assert rows[0].head_sha == _HEAD
                assert rows[0].base_sha == _BASE
                assert rows[0].base_ref == "main"
                assert rows[0].rule_version_id == _RULE
                assert rows[0].prompt_version_id == _PROMPT
                assert rows[0].message_published_at is not None
                run_id = rows[0].id
                rows[0].state = RunState.SUCCEEDED
                await session.commit()
            assert await trigger.execute(_PR, _HEAD) == EnqueueResult(
                EnqueueStatus.DUPLICATE, reason="head_already_reviewed"
            )
            assert len(publisher.messages) == 1
            assert publisher.messages[0].run_id == run_id
            return run_id
        finally:
            await engine.dispose()

    with psycopg.connect(
        database_url.replace("postgresql+psycopg://", "postgresql://"), autocommit=True
    ) as listener:
        listener.execute("LISTEN run_updated")
        published_run_id = asyncio.run(exercise())
        notifications = list(listener.notifies(timeout=1.0, stop_after=1))
    assert len(notifications) == 1
    assert json.loads(notifications[0].payload) == {
        "run_id": str(published_run_id),
        "workspace_id": str(_WS),
        "status": "queued",
    }


@pytest.mark.integration
def test_postgres_lock_candidate_names_what_is_missing(
    webhook_run_database: tuple[str, str],
) -> None:
    database_url, schema = webhook_run_database

    async def exercise() -> None:
        engine = create_async_engine(
            database_url, connect_args={"options": f"-csearch_path={schema}"}
        )
        sessions = async_sessionmaker(engine, expire_on_commit=False)

        async def lock(code_change_id: UUID = _PR) -> RunInsertCandidate | CandidateMiss:
            async with SqlAlchemyWebhookRunUnitOfWork(sessions) as uow:
                return await uow.runs.lock_candidate(code_change_id)

        async def set_active(table: str, active: bool) -> None:
            async with sessions() as session:
                await session.execute(
                    text(f"UPDATE {table} SET is_active = :active"), {"active": active}
                )
                await session.commit()

        try:
            assert await lock() == _insert_candidate()
            assert await lock(uuid4()) is CandidateMiss.PULL_REQUEST_GONE
            await set_active("rule_versions", False)
            assert await lock() is CandidateMiss.MISSING_RULES
            await set_active("rule_versions", True)
            await set_active("prompt_versions", False)
            assert await lock() is CandidateMiss.MISSING_PROMPT
            await set_active("prompt_versions", True)
            assert await lock() == _insert_candidate()
        finally:
            await engine.dispose()

    asyncio.run(exercise())


@pytest.mark.integration
def test_postgres_pending_replay_excludes_superseded_closed_and_disabled(
    webhook_run_database: tuple[str, str],
) -> None:
    database_url, schema = webhook_run_database

    async def exercise() -> None:
        engine = create_async_engine(
            database_url, connect_args={"options": f"-csearch_path={schema}"}
        )
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        run_id = uuid4()
        try:
            async with sessions() as session:
                session.add(
                    Run(
                        id=run_id,
                        code_change_id=_PR,
                        base_sha=_BASE,
                        base_ref="main",
                        head_sha=_HEAD,
                        state=RunState.QUEUED,
                        trigger="webhook",
                        idempotency_key="c" * 64,
                        engine=Engine.FAST,
                        rule_version_id=_RULE,
                        prompt_version_id=_PROMPT,
                        attempt=0,
                        available_at=_NOW,
                        created_at=_NOW,
                    )
                )
                await session.commit()

            async def pending() -> tuple[PendingRunMessage, ...]:
                async with SqlAlchemyWebhookRunUnitOfWork(sessions) as uow:
                    return await uow.runs.pending_messages(10)

            assert [message.run_id for message in await pending()] == [run_id]
            async with sessions() as session:
                pr = await session.get(CodeChange, _PR)
                assert pr is not None and pr.reviewer_requested is True
                pr.ai_review_labeled = False
                pr.ai_review_labeled_at = None
                await session.commit()
            assert await pending() == ()
            detached = await SqlAlchemyEligibilityCandidateStore(sessions).get(_PR)
            assert detached is not None and detached.ai_review_labeled is False
            async with SqlAlchemyWebhookRunUnitOfWork(sessions) as uow:
                locked = await uow.runs.lock_candidate(_PR)
                assert isinstance(locked, RunInsertCandidate)
                assert locked.ci.ai_review_labeled is False
            async with sessions() as session:
                pr = await session.get(CodeChange, _PR)
                assert pr is not None
                pr.ai_review_labeled = True
                pr.ai_review_labeled_at = _NOW
                await session.commit()
            assert [message.run_id for message in await pending()] == [run_id]
            async with sessions() as session:
                pr = await session.get(CodeChange, _PR)
                assert pr is not None
                pr.head_sha = "c" * 40
                await session.commit()
            assert await pending() == ()
            async with sessions() as session:
                pr = await session.get(CodeChange, _PR)
                assert pr is not None
                pr.head_sha = _HEAD
                pr.state = CodeChangeState.CLOSED
                await session.commit()
            assert await pending() == ()
            async with sessions() as session:
                pr = await session.get(CodeChange, _PR)
                repo = await session.get(Repository, _REPO)
                assert pr is not None and repo is not None
                pr.state = CodeChangeState.OPEN
                repo.enabled = False
                await session.commit()
            assert await pending() == ()
        finally:
            await engine.dispose()

    asyncio.run(exercise())


@pytest.mark.integration
def test_postgres_no_ci_sweep_enqueues_once_and_skips_later_rest_calls(
    webhook_run_database: tuple[str, str],
) -> None:
    database_url, schema = webhook_run_database

    @dataclass
    class GitHubRest:
        calls: list[tuple[int, str, str]] = field(default_factory=list)

        async def get_current_head_ci(
            self, installation_id: int, repository_full_name: str, head_sha: str
        ) -> CiSnapshot:
            self.calls.append((installation_id, repository_full_name, head_sha))
            return CiSnapshot(head_sha, (), "success", 0)

    @dataclass
    class Publisher:
        async def publish_confirmed(
            self,
            message: PendingRunMessage,
            *,
            kind: RunPublicationKind = RunPublicationKind.QUEUED,
        ) -> None:
            assert kind is RunPublicationKind.QUEUED

    async def exercise() -> None:
        engine = create_async_engine(
            database_url, connect_args={"options": f"-csearch_path={schema}"}
        )
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        rest = GitHubRest()
        try:
            async with sessions() as session:
                repository = await session.get(Repository, _REPO)
                assert repository is not None
                repository.wait_for_ci = WaitForCi.AUTO
                await session.commit()
            enqueuer = TryEnqueueWebhookRun(
                eligibility=DetermineCiEligibility(
                    candidates=SqlAlchemyEligibilityCandidateStore(sessions),
                    ci=rest,
                    own_app_id=23,
                    now=lambda: _NOW + timedelta(minutes=2),
                ),
                uow_factory=lambda: SqlAlchemyWebhookRunUnitOfWork(sessions),
                publisher=Publisher(),
                now=lambda: _NOW + timedelta(minutes=2),
            )
            sweep = SweepNoCi(
                candidates=SqlAlchemyDueNoCiCandidates(sessions),
                enqueuer=enqueuer,
                now=lambda: _NOW + timedelta(minutes=2),
            )

            assert await sweep.execute() == 1
            assert rest.calls == [(17, "octo/repo", _HEAD)]
            async with sessions() as session:
                assert len((await session.scalars(select(Run))).all()) == 1
            assert await sweep.execute() == 0
            assert rest.calls == [(17, "octo/repo", _HEAD)]
        finally:
            await engine.dispose()

    asyncio.run(exercise())


@pytest.mark.integration
def test_postgres_ci_event_cache_only_updates_current_head_for_check_suite_and_status(
    webhook_run_database: tuple[str, str],
) -> None:
    database_url, schema = webhook_run_database

    async def exercise() -> None:
        engine = create_async_engine(
            database_url, connect_args={"options": f"-csearch_path={schema}"}
        )
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        targets = SqlAlchemyWebhookRunTargets(sessions)
        try:
            assert await targets.for_ci(CiTriggerEvent(17, 101, "c" * 40, "check_suite")) == ()
            async with sessions() as session:
                pr = await session.get(CodeChange, _PR)
                assert pr is not None and pr.ci_status == {}

            assert await targets.for_ci(CiTriggerEvent(17, 101, _HEAD, "check_suite")) == (_PR,)
            async with sessions() as session:
                pr = await session.get(CodeChange, _PR)
                assert pr is not None and pr.ci_status == {"event": "check_suite"}

            assert await targets.for_ci(CiTriggerEvent(17, 101, _HEAD, "status")) == (_PR,)
            async with sessions() as session:
                pr = await session.get(CodeChange, _PR)
                assert pr is not None and pr.ci_status == {"event": "status"}
        finally:
            await engine.dispose()

    asyncio.run(exercise())


@pytest.mark.integration
def test_postgres_no_ci_sweep_races_current_head_check_suite_to_one_run(
    webhook_run_database: tuple[str, str],
) -> None:
    database_url, schema = webhook_run_database

    @dataclass
    class GitHubRest:
        calls: int = 0

        async def get_current_head_ci(
            self, installation_id: int, repository_full_name: str, head_sha: str
        ) -> CiSnapshot:
            self.calls += 1
            return CiSnapshot(head_sha, (), "success", 0)

    @dataclass
    class Publisher:
        async def publish_confirmed(
            self,
            message: PendingRunMessage,
            *,
            kind: RunPublicationKind = RunPublicationKind.QUEUED,
        ) -> None:
            assert kind is RunPublicationKind.QUEUED

    async def exercise() -> None:
        engine = create_async_engine(
            database_url, connect_args={"options": f"-csearch_path={schema}"}
        )
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        rest = GitHubRest()
        try:
            async with sessions() as session:
                repository = await session.get(Repository, _REPO)
                assert repository is not None
                repository.wait_for_ci = WaitForCi.AUTO
                await session.commit()
            enqueuer = TryEnqueueWebhookRun(
                eligibility=DetermineCiEligibility(
                    candidates=SqlAlchemyEligibilityCandidateStore(sessions),
                    ci=rest,
                    own_app_id=23,
                    now=lambda: _NOW + timedelta(minutes=2),
                ),
                uow_factory=lambda: SqlAlchemyWebhookRunUnitOfWork(sessions),
                publisher=Publisher(),
                now=lambda: _NOW + timedelta(minutes=2),
            )
            sweep = SweepNoCi(
                candidates=SqlAlchemyDueNoCiCandidates(sessions),
                enqueuer=enqueuer,
                now=lambda: _NOW + timedelta(minutes=2),
            )
            trigger = TriggerFromDelivery(
                targets=SqlAlchemyWebhookRunTargets(sessions), enqueuer=enqueuer
            )

            await asyncio.gather(
                sweep.execute(), trigger.on_ci(CiTriggerEvent(17, 101, _HEAD, "check_suite"))
            )
            async with sessions() as session:
                runs = (await session.scalars(select(Run))).all()
                pr = await session.get(CodeChange, _PR)
                assert len(runs) == 1
                assert pr is not None and pr.ci_status == {"event": "check_suite"}
            assert rest.calls >= 1
        finally:
            await engine.dispose()

    asyncio.run(exercise())


@pytest.mark.integration
def test_postgres_no_ci_sweep_excludes_failed_candidate_until_state_changes(
    webhook_run_database: tuple[str, str],
) -> None:
    database_url, schema = webhook_run_database

    @dataclass
    class RestDecision:
        calls: list[tuple[UUID, str]] = field(default_factory=list)

        async def execute(self, code_change_id: UUID, expected_head_sha: str) -> EnqueueResult:
            self.calls.append((code_change_id, expected_head_sha))
            return EnqueueResult(EnqueueStatus.INELIGIBLE)

    async def exercise() -> None:
        engine = create_async_engine(
            database_url, connect_args={"options": f"-csearch_path={schema}"}
        )
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        rest = RestDecision()
        try:
            async with sessions() as session:
                repository = await session.get(Repository, _REPO)
                pr = await session.get(CodeChange, _PR)
                assert repository is not None and pr is not None
                repository.wait_for_ci = WaitForCi.AUTO
                pr.ai_review_labeled = True
                pr.ai_review_labeled_at = _NOW
                pr.head_first_seen_at = _NOW
                pr.ci_status = {}
                await session.commit()
            sweep = SweepNoCi(
                candidates=SqlAlchemyDueNoCiCandidates(sessions),
                enqueuer=rest,
                now=lambda: _NOW + timedelta(minutes=2),
            )
            assert await sweep.execute() == 1
            assert rest.calls == [(_PR, _HEAD)]
            assert await sweep.execute() == 0
            assert rest.calls == [(_PR, _HEAD)]
            async with sessions() as session:
                pr = await session.get(CodeChange, _PR)
                assert pr is not None
                pr.ci_status = {}
                await session.commit()
            assert await sweep.execute() == 1
            assert rest.calls == [(_PR, _HEAD), (_PR, _HEAD)]
        finally:
            await engine.dispose()

    asyncio.run(exercise())


@pytest.mark.integration
def test_postgres_projection_cancels_queued_and_flags_running_run(
    webhook_run_database: tuple[str, str],
) -> None:
    database_url, schema = webhook_run_database

    async def exercise() -> None:
        engine = create_async_engine(
            database_url, connect_args={"options": f"-csearch_path={schema}"}
        )
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        queued_id, running_id = uuid4(), uuid4()
        projector = ProjectGitHubPullRequest(
            uow_factory=lambda: SqlAlchemyPullRequestProjectionUnitOfWork(sessions),
            bot_login="reviewer[bot]",
            now=lambda: _NOW + timedelta(minutes=3),
        )

        def event(action: str, head_sha: str, minute: int) -> PullRequestEvent:
            return PullRequestEvent(
                action=action,
                installation_external_id=17,
                repository_external_id=101,
                external_id=901,
                number=7,
                title="Test PR",
                description=None,
                author_login="octo",
                web_url="https://github.com/octo/repo/pull/7",
                source_branch="feature",
                target_branch="main",
                base_sha=_BASE,
                head_sha=head_sha,
                state=PullRequestState.CLOSED if action == "closed" else PullRequestState.OPEN,
                provider_updated_at=_NOW + timedelta(minutes=minute),
            )

        async def seed_run(run_id: UUID, head_sha: str, state: RunState) -> None:
            async with sessions() as session:
                session.add(
                    Run(
                        id=run_id,
                        code_change_id=_PR,
                        base_sha=_BASE,
                        base_ref="main",
                        head_sha=head_sha,
                        state=state,
                        trigger="webhook",
                        idempotency_key=("d" if state == RunState.QUEUED else "e") * 64,
                        engine=Engine.FAST,
                        rule_version_id=_RULE,
                        prompt_version_id=_PROMPT,
                        attempt=0,
                        available_at=_NOW,
                        created_at=_NOW,
                    )
                )
                await session.commit()

        try:
            await seed_run(queued_id, _HEAD, RunState.QUEUED)
            await projector.execute(event("synchronize", "c" * 40, 1))
            async with sessions() as session:
                queued = await session.get(Run, queued_id)
                assert queued is not None
                assert queued.state == RunState.CANCELLED
                assert queued.error_code == "superseded"
                assert queued.finished_at is not None

            await seed_run(running_id, "c" * 40, RunState.RUNNING)
            await projector.execute(event("closed", "c" * 40, 2))
            async with sessions() as session:
                running = await session.get(Run, running_id)
                assert running is not None
                assert running.state == RunState.RUNNING
                assert running.cancel_requested is True
        finally:
            await engine.dispose()

    asyncio.run(exercise())
