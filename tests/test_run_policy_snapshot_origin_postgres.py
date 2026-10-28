"""Policy inputs may become durable only while their immutable Run origin is active."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator, Mapping
from dataclasses import replace
from uuid import UUID

import pytest
from sqlalchemy import event, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.repositories.infrastructure.models import ProviderInstallation, Repository
from app.modules.reviews.application.policy_documents import PolicyEntryKind, PolicyTreeEntry
from app.modules.reviews.application.policy_snapshot import AGENTS_DOCUMENT_PATH, PolicySnapshot
from app.modules.reviews.application.prepare_policy_snapshot import (
    PreparePolicySnapshot,
    build_policy_snapshot,
)
from app.modules.reviews.application.review_ignore import parse_review_ignore
from app.modules.reviews.application.run_failures import RunCancelled
from app.modules.reviews.infrastructure.models import Run
from app.modules.reviews.infrastructure.policy_snapshot_unit_of_work import (
    SqlAlchemyPolicySnapshotStore,
    SqlAlchemyPolicySnapshotUnitOfWork,
)
from tests.portal_postgres import (
    INSTALL_A,
    INSTALL_B,
    REPO_A,
    RUN_ATTEMPTED,
    Env,
    portal_schema,
)
from tests.test_policy_snapshot_postgres import _documents
from tests.test_run_origin_guard_postgres import prepare


@pytest.fixture
def policy_env() -> Iterator[Env]:
    url = os.environ.get("TEST_DATABASE_URL")
    if url is None:
        pytest.skip("set TEST_DATABASE_URL to test policy origin writes")
    with portal_schema(url, None) as env:
        yield env


async def snapshot_for(factory: async_sessionmaker[AsyncSession]) -> PolicySnapshot:
    async with SqlAlchemyPolicySnapshotUnitOfWork(factory) as work:
        pins = await work.policy.get_pins(RUN_ATTEMPTED)
    assert pins is not None
    rules, agents = _documents()
    return build_policy_snapshot(
        pins,
        rules=rules.document,
        agents=agents.document,
        ignore=parse_review_ignore(rules.document.text),
        snapshot_id=UUID(int=11381),
    )


async def counts(factory: async_sessionmaker[AsyncSession]) -> tuple[int, int]:
    async with factory() as session:
        documents = await session.scalar(text("SELECT count(*) FROM run_policy_documents"))
        snapshots = await session.scalar(text("SELECT count(*) FROM run_policy_snapshots"))
    return int(documents), int(snapshots)


@pytest.mark.integration
@pytest.mark.parametrize("origin", ["retired", "rebound", "unknown", "repo_removed"])
@pytest.mark.parametrize("boundary", ["documents", "snapshot", "existing_snapshot"])
def test_origin_rejection_preserves_staged_bytes_and_frozen_snapshot(
    policy_env: Env, origin: str, boundary: str
) -> None:
    async def scenario() -> None:
        engine = policy_env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            await prepare(factory, "active", "running")
            snapshot = await snapshot_for(factory)
            frozen = None
            if boundary != "documents":
                async with SqlAlchemyPolicySnapshotUnitOfWork(factory) as work:
                    await work.policy.stage_documents(RUN_ATTEMPTED, _documents())
                    await work.commit()
            if boundary == "existing_snapshot":
                async with SqlAlchemyPolicySnapshotUnitOfWork(factory) as work:
                    frozen = await work.policy.create_snapshot(snapshot)
                    await work.commit()

            # The load has returned; this independent transaction revokes authority
            # before the next policy write, including between the two commits.
            await prepare(factory, origin, "running")
            with pytest.raises(RunCancelled, match="installation_unavailable"):
                async with SqlAlchemyPolicySnapshotUnitOfWork(factory) as work:
                    if boundary == "documents":
                        await work.policy.stage_documents(RUN_ATTEMPTED, _documents())
                    else:
                        await work.policy.create_snapshot(
                            replace(snapshot, snapshot_id=UUID(int=11382), policy_digest="f" * 64)
                        )
                    await work.commit()

            assert (
                await counts(factory)
                == {"documents": (0, 0), "snapshot": (2, 0), "existing_snapshot": (2, 1)}[boundary]
            )
            async with SqlAlchemyPolicySnapshotUnitOfWork(factory) as work:
                assert await work.policy.get_pins(RUN_ATTEMPTED) is not None
                staged = await work.policy.get_staged_documents(RUN_ATTEMPTED)
                expected = () if boundary == "documents" else _documents()
                assert {item.document.path: item for item in staged} == {
                    item.document.path: item for item in expected
                }
                assert await work.policy.get_snapshot(RUN_ATTEMPTED) == frozen
        finally:
            await engine.dispose()

    asyncio.run(scenario())


@pytest.mark.integration
@pytest.mark.parametrize("retired", [False, True])
def test_final_document_read_releases_db_locks_and_fences_staging(
    policy_env: Env, retired: bool
) -> None:
    async def scenario() -> None:
        engine = policy_env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        calls: list[str] = []

        class Source:
            async def list_entries(self, tree_oid: str | None) -> Mapping[str, PolicyTreeEntry]:
                calls.append("tree")
                return {AGENTS_DOCUMENT_PATH: PolicyTreeEntry(PolicyEntryKind.FILE, "a" * 40, 3)}

            async def read_blob(self, oid: str) -> bytes:
                calls.append("blob")
                # The real read UoW must have ended before the provider boundary.
                async with factory.begin() as session:
                    for table, identifier in (
                        ("provider_installations", INSTALL_A),
                        ("repositories", REPO_A),
                        ("runs", RUN_ATTEMPTED),
                    ):
                        await session.execute(
                            text(f"SELECT id FROM {table} WHERE id=:id FOR UPDATE NOWAIT"),
                            {"id": identifier},
                        )
                    if retired:
                        await session.execute(
                            text(
                                "UPDATE provider_installations SET deleted_at=clock_timestamp() "
                                "WHERE id=:id"
                            ),
                            {"id": INSTALL_A},
                        )
                return b"ok\n"

        try:
            await prepare(factory, "active", "running")
            use_case = PreparePolicySnapshot(
                uow_factory=lambda: SqlAlchemyPolicySnapshotUnitOfWork(factory),
                source_factory=lambda pins: Source(),
            )
            if retired:
                with pytest.raises(RunCancelled, match="installation_unavailable"):
                    await use_case.execute(RUN_ATTEMPTED)
                # The missing rules document was staged while the origin was active.
                assert await counts(factory) == (1, 0)
            else:
                frozen = await use_case.execute(RUN_ATTEMPTED)
                assert frozen.agents.text == "ok\n"
                assert await use_case.execute(RUN_ATTEMPTED) == frozen
                assert await counts(factory) == (2, 1)
            # One root listing per document: rules (missing), then AGENTS.md.
            assert calls == ["tree", "tree", "blob"]
        finally:
            await engine.dispose()

    asyncio.run(scenario())


@pytest.mark.integration
@pytest.mark.parametrize("boundary", ["documents", "snapshot"])
def test_policy_writer_waits_for_rebind_and_rejects_after_it_commits(
    policy_env: Env, boundary: str
) -> None:
    async def scenario() -> None:
        engine = policy_env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        waiting = asyncio.Event()

        def observe(
            connection: object,
            cursor: object,
            statement: str,
            parameters: object,
            context: object,
            many: bool,
        ) -> None:
            if (
                statement.startswith("SELECT provider_installations.id")
                and "FOR SHARE" in statement
            ):
                waiting.set()

        event.listen(engine.sync_engine, "before_cursor_execute", observe)
        try:
            await prepare(factory, "active", "running")
            snapshot = await snapshot_for(factory)
            if boundary == "snapshot":
                async with SqlAlchemyPolicySnapshotUnitOfWork(factory) as work:
                    await work.policy.stage_documents(RUN_ATTEMPTED, _documents())
                    await work.commit()
            waiting.clear()

            async def write() -> None:
                async with SqlAlchemyPolicySnapshotUnitOfWork(factory) as work:
                    if boundary == "documents":
                        await work.policy.stage_documents(RUN_ATTEMPTED, _documents())
                    else:
                        await work.policy.create_snapshot(snapshot)
                    await work.commit()

            async with factory.begin() as rebind:
                await rebind.execute(
                    text("SELECT id FROM provider_installations WHERE id=:id FOR UPDATE"),
                    {"id": INSTALL_A},
                )
                await rebind.execute(
                    text("SELECT id FROM repositories WHERE id=:id FOR UPDATE"), {"id": REPO_A}
                )
                task = asyncio.create_task(write())
                await asyncio.wait_for(waiting.wait(), 5)
                assert not task.done()
                # PostgreSQL, rather than timing, proves the policy SQL is blocked.
                blocker_pid = await rebind.scalar(text("SELECT pg_backend_pid()"))
                async with asyncio.timeout(5), factory() as observer:
                    while not await observer.scalar(
                        text(
                            "SELECT EXISTS (SELECT 1 FROM pg_stat_activity "
                            "WHERE :pid = ANY(pg_blocking_pids(pid)))"
                        ),
                        {"pid": blocker_pid},
                    ):
                        await asyncio.sleep(0)
                await rebind.execute(
                    text(
                        "UPDATE provider_installations SET deleted_at=clock_timestamp() "
                        "WHERE id=:id"
                    ),
                    {"id": INSTALL_A},
                )
                await rebind.execute(
                    text("UPDATE repositories SET provider_installation_id=:origin WHERE id=:id"),
                    {"origin": INSTALL_B, "id": REPO_A},
                )
            with pytest.raises(RunCancelled, match="installation_unavailable"):
                await asyncio.wait_for(task, 5)
            assert await counts(factory) == ((0, 0) if boundary == "documents" else (2, 0))
        finally:
            event.remove(engine.sync_engine, "before_cursor_execute", observe)
            await engine.dispose()

    asyncio.run(scenario())


@pytest.mark.integration
@pytest.mark.parametrize("boundary", ["documents", "snapshot"])
def test_policy_writer_holds_provider_share_locks_while_waiting_for_run(
    policy_env: Env, boundary: str
) -> None:
    async def scenario() -> None:
        engine = policy_env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        waiting = asyncio.Event()

        def observe(
            connection: object,
            cursor: object,
            statement: str,
            parameters: object,
            context: object,
            many: bool,
        ) -> None:
            if statement.startswith("SELECT runs.id") and "FOR UPDATE OF runs" in statement:
                waiting.set()

        event.listen(engine.sync_engine, "before_cursor_execute", observe)
        try:
            await prepare(factory, "active", "running")
            snapshot = await snapshot_for(factory)
            if boundary == "snapshot":
                async with SqlAlchemyPolicySnapshotUnitOfWork(factory) as work:
                    await work.policy.stage_documents(RUN_ATTEMPTED, _documents())
                    await work.commit()
            waiting.clear()

            async def write() -> None:
                async with SqlAlchemyPolicySnapshotUnitOfWork(factory) as work:
                    if boundary == "documents":
                        await work.policy.stage_documents(RUN_ATTEMPTED, _documents())
                    else:
                        await work.policy.create_snapshot(snapshot)
                    await work.commit()

            async with factory.begin() as blocker:
                await blocker.execute(
                    text("SELECT id FROM runs WHERE id=:id FOR UPDATE"), {"id": RUN_ATTEMPTED}
                )
                task = asyncio.create_task(write())
                await asyncio.wait_for(waiting.wait(), 5)
                assert not task.done()
                for table, identifier in (
                    ("provider_installations", INSTALL_A),
                    ("repositories", REPO_A),
                ):
                    async with factory() as observer:
                        with pytest.raises(DBAPIError) as refused:
                            await observer.execute(
                                text(
                                    f"SELECT id FROM {table} WHERE id=:id FOR NO KEY UPDATE NOWAIT"
                                ),
                                {"id": identifier},
                            )
                        assert getattr(refused.value.orig, "sqlstate", None) == "55P03"
                    # Shared authority locks permit other readers / FK key access.
                    async with factory.begin() as reader:
                        await reader.execute(
                            text(f"SELECT id FROM {table} WHERE id=:id FOR KEY SHARE NOWAIT"),
                            {"id": identifier},
                        )
            await asyncio.wait_for(task, 5)
            assert await counts(factory) == ((2, 0) if boundary == "documents" else (2, 1))
        finally:
            event.remove(engine.sync_engine, "before_cursor_execute", observe)
            await engine.dispose()

    asyncio.run(scenario())


@pytest.mark.integration
@pytest.mark.parametrize("state", ["queued", "cancelled", "succeeded", "publishing"])
@pytest.mark.parametrize("boundary", ["documents", "snapshot"])
def test_active_origin_with_inactive_run_rejects_policy_write(
    policy_env: Env, state: str, boundary: str
) -> None:
    async def scenario() -> None:
        engine = policy_env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            await prepare(factory, "active", "running")
            snapshot = await snapshot_for(factory)
            if boundary == "snapshot":
                async with SqlAlchemyPolicySnapshotUnitOfWork(factory) as work:
                    await work.policy.stage_documents(RUN_ATTEMPTED, _documents())
                    await work.commit()
            await prepare(factory, "active", state)
            with pytest.raises(RunCancelled, match="run_inactive"):
                async with SqlAlchemyPolicySnapshotUnitOfWork(factory) as work:
                    if boundary == "documents":
                        await work.policy.stage_documents(RUN_ATTEMPTED, _documents())
                    else:
                        await work.policy.create_snapshot(snapshot)
                    await work.commit()
            assert await counts(factory) == ((0, 0) if boundary == "documents" else (2, 0))
        finally:
            await engine.dispose()

    asyncio.run(scenario())


@pytest.mark.integration
@pytest.mark.parametrize("origin", ["retired", "rebound", "unknown", "repo_removed"])
def test_rejected_document_retry_preserves_partial_staging(policy_env: Env, origin: str) -> None:
    async def scenario() -> None:
        engine = policy_env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            await prepare(factory, "active", "running")
            rules, agents = _documents()
            async with SqlAlchemyPolicySnapshotUnitOfWork(factory) as work:
                await work.policy.stage_documents(RUN_ATTEMPTED, (rules,))
                await work.commit()
            await prepare(factory, origin, "running")
            with pytest.raises(RunCancelled, match="installation_unavailable"):
                async with SqlAlchemyPolicySnapshotUnitOfWork(factory) as work:
                    await work.policy.stage_documents(RUN_ATTEMPTED, (rules, agents))
                    await work.commit()
            assert await counts(factory) == (1, 0)
            async with SqlAlchemyPolicySnapshotUnitOfWork(factory) as work:
                assert await work.policy.get_staged_documents(RUN_ATTEMPTED) == (rules,)
                assert await work.policy.get_snapshot(RUN_ATTEMPTED) is None
        finally:
            await engine.dispose()

    asyncio.run(scenario())


@pytest.mark.integration
@pytest.mark.parametrize("boundary", ["documents", "snapshot"])
def test_missing_run_rejects_policy_write_as_unavailable(policy_env: Env, boundary: str) -> None:
    async def scenario() -> None:
        engine = policy_env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            snapshot = replace(await snapshot_for(factory), run_id=UUID(int=11389))
            with pytest.raises(RunCancelled, match="installation_unavailable"):
                async with SqlAlchemyPolicySnapshotUnitOfWork(factory) as work:
                    if boundary == "documents":
                        await work.policy.stage_documents(snapshot.run_id, _documents())
                    else:
                        await work.policy.create_snapshot(snapshot)
                    await work.commit()
            assert await counts(factory) == (0, 0)
        finally:
            await engine.dispose()

    asyncio.run(scenario())


@pytest.mark.integration
@pytest.mark.parametrize(
    "origin,state,reason",
    [
        ("retired", "running", "installation_unavailable"),
        ("rebound", "running", "installation_unavailable"),
        ("repo_removed", "running", "installation_unavailable"),
        ("active", "succeeded", "run_inactive"),
    ],
)
def test_policy_write_refreshes_cached_authority_rows(
    policy_env: Env, origin: str, state: str, reason: str
) -> None:
    async def scenario() -> None:
        engine = policy_env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            await prepare(factory, "active", "running")
            async with factory() as session:
                # Keep strong references to the pre-write identity-map entities.
                run = await session.get(Run, RUN_ATTEMPTED)
                repository = await session.get(Repository, REPO_A)
                installation = await session.get(ProviderInstallation, INSTALL_A)
                assert run is not None and repository is not None and installation is not None
                assert run.state == "running" and installation.deleted_at is None
                await prepare(factory, origin, state)
                with pytest.raises(RunCancelled, match=reason):
                    await SqlAlchemyPolicySnapshotStore(session).stage_documents(
                        RUN_ATTEMPTED, _documents()
                    )
                await session.rollback()
            assert await counts(factory) == (0, 0)
        finally:
            await engine.dispose()

    asyncio.run(scenario())


@pytest.mark.integration
def test_policy_store_leaves_both_commits_to_the_caller(policy_env: Env) -> None:
    async def scenario() -> None:
        engine = policy_env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            await prepare(factory, "active", "running")
            snapshot = await snapshot_for(factory)
            async with SqlAlchemyPolicySnapshotUnitOfWork(factory) as work:
                await work.policy.stage_documents(RUN_ATTEMPTED, _documents())
            assert await counts(factory) == (0, 0)
            async with SqlAlchemyPolicySnapshotUnitOfWork(factory) as work:
                await work.policy.stage_documents(RUN_ATTEMPTED, _documents())
                await work.commit()
            async with SqlAlchemyPolicySnapshotUnitOfWork(factory) as work:
                await work.policy.create_snapshot(snapshot)
            assert await counts(factory) == (2, 0)
        finally:
            await engine.dispose()

    asyncio.run(scenario())
