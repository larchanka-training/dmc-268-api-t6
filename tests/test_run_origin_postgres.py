"""Immutable original Run installation across migrations and fresh OAuth rebind."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.modules.workspaces.infrastructure.github_repository_rebinding as rebinding
from alembic import command
from app.bootstrap.db_metadata import metadata
from app.common.infrastructure.db.enums import RunState
from app.modules.auth.application.scope import AuthScope
from app.modules.reviews.application.determine_ci_eligibility import DetermineCiEligibility
from app.modules.reviews.application.queue_messages import ReviewPublishPointer
from app.modules.reviews.application.rerun_run import RerunRun
from app.modules.reviews.application.store_review_output import StoreReviewOutput
from app.modules.reviews.application.try_enqueue_webhook_run import TryEnqueueWebhookRun
from app.modules.reviews.infrastructure.ci_eligibility_candidates import (
    SqlAlchemyEligibilityCandidateStore,
)
from app.modules.reviews.infrastructure.rerun_store import SqlAlchemyRerunUnitOfWork
from app.modules.reviews.infrastructure.review_output_unit_of_work import (
    SqlAlchemyReviewOutputUnitOfWork,
)
from app.modules.reviews.infrastructure.run_lifecycle_store import SqlAlchemyRunLifecycleStore
from app.modules.reviews.infrastructure.run_notifications import notify_run_state
from app.modules.reviews.infrastructure.run_repository import SqlAlchemyRunRepository
from app.modules.reviews.infrastructure.webhook_runs import SqlAlchemyWebhookRunUnitOfWork
from app.modules.workspaces.application.link_github_installations import GitHubInstallation
from tests.portal_postgres import (
    INSTALL_A,
    INSTALL_B,
    PR_OPEN,
    PROMPT,
    REPO_A,
    RULE_A,
    RUN_ATTEMPTED,
    RUN_B,
    RUN_CLOSED,
    RUN_DONE,
    WS_A,
    Env,
    portal_schema,
)
from tests.test_app_reinstall_postgres import FreshGitHub, apply, seed_archive
from tests.test_effective_review_eligibility_postgres import Publisher
from tests.test_retired_enqueue_postgres import Ci, prepare


@pytest.fixture
def origin_env() -> Iterator[Env]:
    database_url = os.environ.get("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("set TEST_DATABASE_URL to run PostgreSQL integration tests")
    with portal_schema(database_url, None) as env:
        yield env


@pytest.mark.integration
def test_run_origin_migration_backfills_terminal_and_nonterminal_history_and_round_trips(
    origin_env: Env,
) -> None:
    engine = create_engine(
        origin_env.database_url, connect_args={"options": f"-csearch_path={origin_env.schema}"}
    )
    try:
        with engine.connect() as connection:
            config = Config("alembic.ini")
            config.attributes["connection"] = connection
            command.downgrade(config, "20261010_0029")
            before = (
                connection.execute(text("SELECT id,to_jsonb(r) FROM runs r ORDER BY id"))
                .tuples()
                .all()
            )
            assert dict(connection.execute(text("SELECT id,state FROM runs")).tuples().all()) == {
                RUN_DONE: "succeeded",
                RUN_CLOSED: "succeeded",
                RUN_ATTEMPTED: "queued",
                RUN_B: "succeeded",
            }
            connection.commit()
            command.upgrade(config, "head")
            assert "origin_provider_installation_id" in {
                column["name"] for column in inspect(connection).get_columns("runs")
            }
            origins = (
                connection.execute(text("SELECT id,origin_provider_installation_id FROM runs"))
                .tuples()
                .all()
            )
            assert dict(origins) == {
                RUN_DONE: INSTALL_A,
                RUN_CLOSED: INSTALL_A,
                RUN_ATTEMPTED: INSTALL_A,
                RUN_B: INSTALL_B,
            }
            after = (
                connection.execute(
                    text(
                        "SELECT id,to_jsonb(r)-'origin_provider_installation_id'"
                        "-'severity_threshold'-'max_comments' FROM runs"
                        " r ORDER BY id"
                    )
                )
                .tuples()
                .all()
            )
            assert after == before
            assert connection.execute(
                text("SELECT severity_threshold, max_comments FROM runs ORDER BY id")
            ).tuples().all() == [(None, None), (None, None), (None, None), (None, None)]
            context = MigrationContext.configure(
                connection, opts={"compare_type": True, "compare_server_default": True}
            )
            assert compare_metadata(context, metadata) == []
            connection.commit()
            command.downgrade(config, "20261010_0029")
            assert "origin_provider_installation_id" not in {
                column["name"] for column in inspect(connection).get_columns("runs")
            }
            assert (
                connection.execute(text("SELECT id,to_jsonb(r) FROM runs r ORDER BY id"))
                .tuples()
                .all()
                == before
            )
            connection.commit()
            command.upgrade(config, "head")
            assert dict(
                connection.execute(text("SELECT id,origin_provider_installation_id FROM runs"))
                .tuples()
                .all()
            ) == dict(origins)
            connection.commit()
    finally:
        engine.dispose()


@pytest.mark.integration
def test_fresh_oauth_pins_null_source_origins_before_rebind_without_rewriting_history(
    origin_env: Env,
) -> None:
    async def exercise() -> None:
        engine = origin_env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            await seed_archive(factory)
            async with factory.begin() as session:
                await session.execute(text("UPDATE runs SET origin_provider_installation_id=NULL"))
                # A nonterminal historical Run must also retain its original association.
                await session.execute(
                    text("UPDATE runs SET state='queued' WHERE id=:id"), {"id": RUN_ATTEMPTED}
                )
                await session.execute(
                    text("UPDATE runs SET origin_provider_installation_id=:origin WHERE id=:id"),
                    {"id": RUN_DONE, "origin": INSTALL_B},
                )
                before = (
                    (
                        await session.execute(
                            text(
                                "SELECT id,to_jsonb(r)-'origin_provider_installation_id' FROM runs"
                                " r ORDER BY id"
                            )
                        )
                    )
                    .tuples()
                    .all()
                )
            await apply(factory, FreshGitHub())
            async with factory() as session:
                assert dict(
                    (
                        await session.execute(
                            text("SELECT id,origin_provider_installation_id FROM runs")
                        )
                    )
                    .tuples()
                    .all()
                ) == {
                    RUN_DONE: INSTALL_B,
                    RUN_CLOSED: INSTALL_A,
                    RUN_ATTEMPTED: INSTALL_A,
                    RUN_B: None,
                }
                after = (
                    (
                        await session.execute(
                            text(
                                "SELECT id,to_jsonb(r)-'origin_provider_installation_id' FROM runs"
                                " r ORDER BY id"
                            )
                        )
                    )
                    .tuples()
                    .all()
                )
                before_rows, after_rows = dict(before), dict(after)
                assert {
                    key: value for key, value in after_rows.items() if key != RUN_ATTEMPTED
                } == {key: value for key, value in before_rows.items() if key != RUN_ATTEMPTED}
                lifecycle = {"state", "error_code", "finished_at", "lease_until", "updated_at"}
                assert {
                    key: value
                    for key, value in after_rows[RUN_ATTEMPTED].items()
                    if key not in lifecycle
                } == {
                    key: value
                    for key, value in before_rows[RUN_ATTEMPTED].items()
                    if key not in lifecycle
                }
                assert after_rows[RUN_ATTEMPTED]["state"] == "cancelled"
                assert after_rows[RUN_ATTEMPTED]["error_code"] == "installation_replaced"
                assert after_rows[RUN_ATTEMPTED]["finished_at"] is not None
                assert after_rows[RUN_ATTEMPTED]["lease_until"] is None
                assert (
                    await session.scalar(
                        text(
                            "SELECT pi.external_id FROM runs r JOIN code_changes cc ON "
                            "cc.id=r.code_change_id "
                            "JOIN repositories repo ON repo.id=cc.repository_id "
                            "JOIN provider_installations pi ON "
                            "pi.id=repo.provider_installation_id WHERE r.id=:id"
                        ),
                        {"id": RUN_ATTEMPTED},
                    )
                    == 29
                )
        finally:
            await engine.dispose()

    asyncio.run(exercise())


@pytest.mark.integration
@pytest.mark.parametrize("writer", ["webhook", "rerun"])
@pytest.mark.parametrize("reinstalled", [False, True])
def test_both_run_writers_pin_the_locked_original_installation_uuid(
    origin_env: Env,
    writer: str,
    reinstalled: bool,
) -> None:
    async def exercise() -> None:
        engine = origin_env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        publisher = Publisher()
        try:
            if reinstalled:
                await seed_archive(factory)
                await apply(factory, FreshGitHub())
            await prepare(factory, "never", False)
            expected_external_id = 29 if reinstalled else 17
            async with factory() as session:
                expected_origin = await session.scalar(
                    text("SELECT id FROM provider_installations WHERE external_id=:external"),
                    {"external": expected_external_id},
                )
            assert expected_origin is not None
            if writer == "webhook":
                ci = Ci(factory)
                enqueue = TryEnqueueWebhookRun(
                    eligibility=DetermineCiEligibility(
                        candidates=SqlAlchemyEligibilityCandidateStore(factory),
                        ci=ci,
                        own_app_id=42,
                    ),
                    uow_factory=lambda: SqlAlchemyWebhookRunUnitOfWork(factory),
                    publisher=publisher,
                )
                result = await enqueue.execute(
                    PR_OPEN,
                    "a" * 40,
                    expected_installation_external_id=expected_external_id,
                )
                assert result.status.value == "enqueued"
                run_id = result.run_id
                assert ci.calls == []
            else:
                scope = AuthScope(42, (WS_A,))
                rerun = RerunRun(
                    lambda: SqlAlchemyRerunUnitOfWork(factory, scope),
                    SqlAlchemyRunRepository(factory, scope),
                    publisher,
                )
                item = await rerun.execute(RUN_DONE)
                assert item is not None
                assert item.status == "queued"
                run_id = item.id
            assert run_id is not None
            assert len(publisher.messages) == 1
            assert publisher.messages[0].installation_id == expected_external_id
            async with factory() as session:
                origin = (
                    await session.execute(
                        text(
                            "SELECT origin_provider_installation_id,trigger,state,head_sha "
                            "FROM runs WHERE id=:id"
                        ),
                        {"id": run_id},
                    )
                ).one()
                assert tuple(origin) == (expected_origin, writer, "queued", "a" * 40)
                assert await session.scalar(text("SELECT count(*) FROM runs")) == 5
        finally:
            await engine.dispose()

    asyncio.run(exercise())


@pytest.mark.integration
@pytest.mark.parametrize("state", ["queued", "running", "publishing"])
@pytest.mark.parametrize("legacy_origin", [False, True])
def test_fresh_oauth_cancels_source_work_before_rebind_and_late_finalization_cannot_revive_it(
    origin_env: Env, state: str, legacy_origin: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def exercise() -> None:
        engine = origin_env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        now = datetime.now(UTC)
        late_started = asyncio.Event()
        late_finalization: asyncio.Task[bool] | None = None
        notify = notify_run_state

        async def finalize() -> bool:
            async with factory.begin() as session:
                late_started.set()
                return await SqlAlchemyRunLifecycleStore(session).finish(
                    RUN_ATTEMPTED,
                    from_state=state,
                    worker_id="old-worker",
                    state="succeeded",
                    error_code=None,
                    error_message=None,
                    now=now,
                )

        async def checkpoint_notify(session: AsyncSession, run_id: UUID, status: RunState) -> None:
            nonlocal late_finalization
            assert run_id == RUN_ATTEMPTED and status == RunState.CANCELLED
            assert (
                await session.scalar(
                    text("SELECT provider_installation_id FROM repositories WHERE id=:id"),
                    {"id": REPO_A},
                )
                == INSTALL_A
            )
            for table, row_id in (
                ("provider_installations", INSTALL_A),
                ("repositories", REPO_A),
                ("runs", RUN_ATTEMPTED),
            ):
                async with factory.begin() as observer:
                    with pytest.raises(DBAPIError) as locked:
                        await observer.execute(
                            text(f"SELECT id FROM {table} WHERE id=:id FOR UPDATE NOWAIT"),
                            {"id": row_id},
                        )
                    assert getattr(locked.value.orig, "sqlstate", None) == "55P03"
            late_finalization = asyncio.create_task(finalize())
            await late_started.wait()
            assert not late_finalization.done()
            await notify(session, run_id, status)

        monkeypatch.setattr(rebinding, "notify_run_state", checkpoint_notify)
        target_installation = UUID(int=11329)
        target_repo, target_pr, target_run, target_rule = (UUID(int=n) for n in range(11340, 11344))
        try:
            await seed_archive(factory)
            async with factory.begin() as session:
                await session.execute(
                    text(
                        "UPDATE runs SET origin_provider_installation_id=:origin WHERE id!=:other"
                    ),
                    {"origin": INSTALL_A, "other": RUN_B},
                )
                await session.execute(
                    text(
                        "UPDATE runs SET state=:state,origin_provider_installation_id=:origin, "
                        "worker_id='old-worker',lease_until=:lease,finished_at=NULL WHERE id=:id"
                    ),
                    {
                        "id": RUN_ATTEMPTED,
                        "state": state,
                        "origin": None if legacy_origin else INSTALL_A,
                        "lease": now + timedelta(minutes=1),
                    },
                )
                # Already-confirmed target work in another target Repository remains valid.
                await session.execute(
                    text(
                        "INSERT INTO provider_installations "
                        "(id,workspace_id,provider,external_id,account_external_id,metadata) "
                        "VALUES (:id,:ws,'github',29,700,'{}')"
                    ),
                    {"id": target_installation, "ws": WS_A},
                )
                await session.execute(
                    text(
                        "INSERT INTO repositories "
                        "(id,provider_installation_id,external_id,full_name, "
                        "default_branch,web_url) VALUES (:id,:source,109,'octo/target','main', "
                        "'https://github.test/octo/target')"
                    ),
                    {"id": target_repo, "source": target_installation},
                )
                await session.execute(
                    text(
                        "INSERT INTO rule_versions "
                        "(id,repository_id,version,rules,checksum,is_active) "
                        "SELECT :id,:repo,version,rules,checksum,is_active FROM "
                        "rule_versions WHERE id=:old"
                    ),
                    {"id": target_rule, "repo": target_repo, "old": RULE_A},
                )
                await session.execute(
                    text(
                        "INSERT INTO code_changes "
                        "(id,repository_id,external_id,external_number,title, "
                        "source_branch,target_branch,base_sha,head_sha,state,web_url) "
                        "VALUES (:id,:repo,909,9,'Target "
                        "work','feature','main',:base,:head,'open', "
                        "'https://github.test/octo/target/pull/9')"
                    ),
                    {"id": target_pr, "repo": target_repo, "base": "b" * 40, "head": "c" * 40},
                )
                await session.execute(
                    text(
                        "INSERT INTO runs "
                        "(id,code_change_id,origin_provider_installation_id,base_sha, "
                        "head_sha,state,trigger,idempotency_key,engine,rule_version_id,pr"
                        "ompt_version_id,available_at) "
                        "VALUES (:id,:pr,:origin,:base,:head,'running','webhook',:key,'fa"
                        "st',:rule,:prompt,clock_timestamp())"
                    ),
                    {
                        "id": target_run,
                        "pr": target_pr,
                        "origin": target_installation,
                        "base": "b" * 40,
                        "head": "c" * 40,
                        "key": "9" * 64,
                        "rule": target_rule,
                        "prompt": PROMPT,
                    },
                )
                before = dict(
                    (await session.execute(text("SELECT id,to_jsonb(r) FROM runs r ORDER BY id")))
                    .tuples()
                    .all()
                )
            await apply(
                factory,
                FreshGitHub(installation=GitHubInstallation(29, "renamed-owner", (101, 109), 700)),
            )
            assert late_finalization is not None
            assert await late_finalization is False
            async with factory.begin() as session:
                row = (
                    await session.execute(
                        text(
                            "SELECT state,error_code,origin_provider_installation_id,lease_un"
                            "til,finished_at "
                            "FROM runs WHERE id=:id"
                        ),
                        {"id": RUN_ATTEMPTED},
                    )
                ).one()
                assert tuple(row[:4]) == ("cancelled", "installation_replaced", INSTALL_A, None)
                assert row[4] is not None
                assert (
                    await session.scalar(
                        text("SELECT provider_installation_id FROM repositories WHERE id=:id"),
                        {"id": REPO_A},
                    )
                    == target_installation
                )
                after = dict(
                    (await session.execute(text("SELECT id,to_jsonb(r) FROM runs r ORDER BY id")))
                    .tuples()
                    .all()
                )
                lifecycle = {
                    "state",
                    "error_code",
                    "finished_at",
                    "lease_until",
                    "updated_at",
                    "origin_provider_installation_id",
                }
                assert {k: v for k, v in after[RUN_ATTEMPTED].items() if k not in lifecycle} == {
                    k: v for k, v in before[RUN_ATTEMPTED].items() if k not in lifecycle
                }
                assert {k: v for k, v in after.items() if k != RUN_ATTEMPTED} == {
                    k: v for k, v in before.items() if k != RUN_ATTEMPTED
                }
                store = SqlAlchemyRunLifecycleStore(session)
                assert (
                    await store.finish(
                        RUN_ATTEMPTED,
                        from_state=state,
                        worker_id="old-worker",
                        state="succeeded",
                        error_code=None,
                        error_message=None,
                        now=now,
                    )
                    is False
                )
                assert (
                    await store.requeue(RUN_ATTEMPTED, worker_id="old-worker", available_at=now)
                    is False
                )
                assert (
                    await store.complete_publication(
                        RUN_ATTEMPTED,
                        review_id=555,
                        comment_ids=(),
                        findings_hash="a" * 64,
                        moved_to_body=False,
                        now=now,
                    )
                    is None
                )
                final = dict(
                    (await session.execute(text("SELECT id,to_jsonb(r) FROM runs r ORDER BY id")))
                    .tuples()
                    .all()
                )
                assert final == after

            class NoPublication:
                async def publish_review(self, pointer: ReviewPublishPointer) -> None:
                    raise AssertionError("cancelled source must not enqueue review publication")

            async with factory() as session:
                before_payloads = {}
                for table in ("findings", "run_actions", "context_payloads"):
                    before_payloads[table] = (
                        (
                            await session.execute(
                                text(f"SELECT to_jsonb(t) FROM {table} t ORDER BY id")
                            )
                        )
                        .scalars()
                        .all()
                    )
            late_output = StoreReviewOutput(
                lambda: SqlAlchemyReviewOutputUnitOfWork(factory),
                NoPublication(),
                worker_id="old-worker",
            )
            assert (
                await late_output.execute(
                    RUN_ATTEMPTED,
                    {
                        "findings": [],
                        "summary": {
                            "problem": "The old answer arrived after cancellation.",
                            "done_well": "The old source was recorded.",
                            "effort": "small",
                        },
                    },
                )
                is False
            )
            async with factory() as session:
                for table, payload in before_payloads.items():
                    assert (
                        await session.execute(
                            text(f"SELECT to_jsonb(t) FROM {table} t ORDER BY id")
                        )
                    ).scalars().all() == payload
                assert (
                    dict(
                        (
                            await session.execute(
                                text("SELECT id,to_jsonb(r) FROM runs r ORDER BY id")
                            )
                        )
                        .tuples()
                        .all()
                    )
                    == after
                )
            current = await SqlAlchemyRunRepository(factory, AuthScope(42, (WS_A,))).get_run(
                RUN_ATTEMPTED
            )
            assert current is not None and current.status == "cancelled"
        finally:
            await engine.dispose()

    asyncio.run(exercise())
