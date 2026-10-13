"""Late VCS responses cannot alter a terminal Run or its immutable diff history."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import UUID

import httpx
import pytest
from sqlalchemy import event, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.integrations.webhooks.infrastructure.github_installation_tree_provider import (
    StaticGitHubInstallationAccessTokenProvider,
)
from app.modules.reviews.application.get_run_diff import DiffSnapshot
from app.modules.reviews.application.process_run import ReviewRunProcessor
from app.modules.reviews.application.run_failures import RunCancelled
from app.modules.reviews.infrastructure.github_vcs import HttpGitHubVcsProvider
from app.modules.reviews.infrastructure.models import CodeChangeDiff, Run
from app.modules.reviews.infrastructure.run_lifecycle_store import SqlAlchemyRunLifecycleStore
from app.modules.reviews.infrastructure.run_processing_unit_of_work import (
    SqlAlchemyRunProcessingUnitOfWork,
)
from app.modules.reviews.infrastructure.run_repository import SqlAlchemyRunRepository
from tests.portal_postgres import HEAD, INSTALL_A, INSTALL_B, REPO_A, RUN_ATTEMPTED, Env
from tests.test_app_reinstall_postgres import FreshGitHub, apply
from tests.test_github_vcs_provider import _file, _pull_payload
from tests.test_run_origin_guard_postgres import prepare
from tests.test_run_publication_origin_postgres import retire
from tests.test_run_vcs_origin_postgres import vcs_env as vcs_env


async def history_in(session: AsyncSession) -> dict[str, object]:
    result: dict[str, object] = {
        "run": await session.scalar(
            text("SELECT to_jsonb(r) FROM runs r WHERE id=:id"), {"id": RUN_ATTEMPTED}
        ),
    }
    for table in ("code_change_diffs", "findings", "comments", "run_actions"):
        condition = (
            "r.finding_id IN (SELECT id FROM findings WHERE run_id=:id)"
            if table == "comments"
            else "r.run_id=:id"
        )
        result[table] = list(
            (
                await session.scalars(
                    text(f"SELECT to_jsonb(r) FROM {table} r WHERE {condition} ORDER BY r.id"),
                    {"id": RUN_ATTEMPTED},
                )
            ).all()
        )
    return result


async def history(factory: async_sessionmaker[AsyncSession]) -> dict[str, object]:
    async with factory() as session:
        return await history_in(session)


@pytest.mark.integration
@pytest.mark.parametrize("change", ["active", "deleted", "rebound"])
def test_final_refetch_response_cannot_write_diff_after_source_terminalization(
    vcs_env: Env, change: str
) -> None:
    async def exercise() -> None:
        engine = vcs_env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        terminal: dict[str, object] | None = None
        calls: list[str] = []
        pull_calls = 0

        async def http(request: httpx.Request) -> httpx.Response:
            nonlocal terminal, pull_calls
            path = request.url.path
            calls.append(path)
            if path.endswith("/pulls/3"):
                pull_calls += 1
                if pull_calls == 2 and change != "active":
                    await retire(factory)
                    if change == "rebound":
                        await apply(factory, FreshGitHub())
                    else:
                        async with factory.begin() as session:
                            assert await SqlAlchemyRunLifecycleStore(session).finish(
                                RUN_ATTEMPTED,
                                from_state="running",
                                worker_id=None,
                                state="cancelled",
                                error_code="installation_unavailable",
                                error_message=None,
                                now=datetime.now(UTC),
                            )
                    terminal = await history(factory)
                payload = _pull_payload(changed_files=2, head_sha=HEAD)
                payload["number"] = 3
                return httpx.Response(200, json=payload)
            if path.endswith("/pulls/3/files"):
                return httpx.Response(200, json=[_file(1), _file(2)])
            if "/git/blobs/" in path:
                return httpx.Response(
                    200,
                    json={
                        "sha": path.rsplit("/", 1)[1],
                        "encoding": "base64",
                        "content": "b2sK",
                        "size": 3,
                    },
                )
            raise AssertionError(f"unexpected {request.url}")

        try:
            await prepare(factory, "active", "running")
            async with factory.begin() as session:
                await session.execute(
                    text("UPDATE provider_installations SET account_external_id=700 WHERE id=:id"),
                    {"id": INSTALL_A},
                )
            async with httpx.AsyncClient(
                base_url="https://api.github.test", transport=httpx.MockTransport(http)
            ) as client:
                processor = ReviewRunProcessor(
                    SqlAlchemyRunRepository(factory, allow_unscoped=True),
                    None,
                    vcs_provider=HttpGitHubVcsProvider(
                        client=client,
                        token_provider=StaticGitHubInstallationAccessTokenProvider("token"),
                    ),
                    uow_factory=lambda: SqlAlchemyRunProcessingUnitOfWork(factory),
                )
                if change == "active":
                    assert await processor.prepare(RUN_ATTEMPTED) is True
                    async with factory() as session:
                        assert (
                            await session.scalar(
                                text(
                                    "SELECT diff_snapshotted_at IS NOT NULL FROM runs WHERE id=:id"
                                ),
                                {"id": RUN_ATTEMPTED},
                            )
                            is True
                        )
                        assert (
                            await session.scalar(
                                text("SELECT count(*) FROM code_change_diffs WHERE run_id=:id"),
                                {"id": RUN_ATTEMPTED},
                            )
                            == 2
                        )
                else:
                    with pytest.raises(RunCancelled, match="installation_unavailable"):
                        await processor.prepare(RUN_ATTEMPTED)
                    assert terminal is not None
                    assert await history(factory) == terminal
                    assert calls == [
                        "/repos/octo/a/pulls/3",
                        "/repos/octo/a/pulls/3/files",
                        "/repos/octo/a/pulls/3",
                    ]
        finally:
            await engine.dispose()

    asyncio.run(exercise())


@pytest.mark.integration
@pytest.mark.parametrize(
    "state", ["queued", "running", "publishing", "succeeded", "failed", "cancelled", "skipped"]
)
@pytest.mark.parametrize("stored", [False, True])
def test_snapshot_writer_requires_running_and_preserves_existing_history(
    vcs_env: Env, state: str, stored: bool
) -> None:
    async def exercise() -> None:
        engine = vcs_env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            await prepare(factory, "active", state)
            async with factory.begin() as session:
                pr = await session.scalar(select(Run.code_change_id).where(Run.id == RUN_ATTEMPTED))
                assert pr is not None
                if stored:
                    session.add(
                        CodeChangeDiff(
                            id=UUID(int=11380),
                            run_id=RUN_ATTEMPTED,
                            code_change_id=pr,
                            head_sha=HEAD,
                            filename="history.py",
                            patch="immutable",
                        )
                    )
                    await session.execute(
                        text("UPDATE runs SET diff_snapshotted_at=clock_timestamp() WHERE id=:id"),
                        {"id": RUN_ATTEMPTED},
                    )
            before = await history(factory)
            async with factory.begin() as session:
                repository = SqlAlchemyRunRepository(session=session, allow_unscoped=True)
                if state == "running":
                    result = await repository.store_diff_snapshots(
                        RUN_ATTEMPTED, pr, HEAD, [DiffSnapshot(filename="fresh.py", patch="fresh")]
                    )
                    assert [file.filename for file in result] == [
                        "history.py" if stored else "fresh.py"
                    ]
                else:
                    with pytest.raises(RunCancelled, match="run_inactive"):
                        await repository.store_diff_snapshots(
                            RUN_ATTEMPTED,
                            pr,
                            HEAD,
                            [DiffSnapshot(filename="fresh.py", patch="fresh")],
                        )
            if state != "running" or stored:
                assert await history(factory) == before
        finally:
            await engine.dispose()

    asyncio.run(exercise())


@pytest.mark.integration
@pytest.mark.parametrize("origin", ["unknown", "retired", "rebound"])
def test_snapshot_writer_refuses_inactive_origin_even_while_running(
    vcs_env: Env, origin: str
) -> None:
    async def exercise() -> None:
        engine = vcs_env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            await prepare(factory, origin, "running")
            before = await history(factory)
            async with factory.begin() as session:
                pr = await session.scalar(select(Run.code_change_id).where(Run.id == RUN_ATTEMPTED))
                assert pr is not None
                with pytest.raises(RunCancelled, match="installation_unavailable"):
                    await SqlAlchemyRunRepository(
                        session=session, allow_unscoped=True
                    ).store_diff_snapshots(
                        RUN_ATTEMPTED, pr, HEAD, [DiffSnapshot(filename="fresh.py", patch="fresh")]
                    )
            assert await history(factory) == before
        finally:
            await engine.dispose()

    asyncio.run(exercise())


@pytest.mark.integration
def test_snapshot_writer_locks_origin_and_repository_before_waiting_for_run(vcs_env: Env) -> None:
    async def exercise() -> None:
        engine = vcs_env.engine()
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
            if "FOR UPDATE OF runs" in statement:
                waiting.set()

        event.listen(engine.sync_engine, "before_cursor_execute", observe)
        try:
            await prepare(factory, "active", "running")
            async with factory.begin() as holder:
                pr = await holder.scalar(
                    text("SELECT code_change_id FROM runs WHERE id=:id FOR UPDATE"),
                    {"id": RUN_ATTEMPTED},
                )
                assert pr is not None

                async def store() -> None:
                    async with factory.begin() as session:
                        await SqlAlchemyRunRepository(
                            session=session, allow_unscoped=True
                        ).store_diff_snapshots(
                            RUN_ATTEMPTED,
                            pr,
                            HEAD,
                            [DiffSnapshot(filename="fresh.py", patch="fresh")],
                        )

                task = asyncio.create_task(store())
                await asyncio.wait_for(waiting.wait(), 5)
                assert not task.done()
                for table, row_id in [
                    ("provider_installations", INSTALL_A),
                    ("repositories", REPO_A),
                ]:
                    with pytest.raises(DBAPIError) as blocked:
                        async with factory.begin() as probe:
                            await probe.execute(
                                text(f"SELECT id FROM {table} WHERE id=:id FOR UPDATE NOWAIT"),
                                {"id": row_id},
                            )
                    assert getattr(blocked.value.orig, "sqlstate", None) == "55P03"
            await asyncio.wait_for(task, 5)
        finally:
            event.remove(engine.sync_engine, "before_cursor_execute", observe)
            await engine.dispose()

    asyncio.run(exercise())


@pytest.mark.integration
@pytest.mark.parametrize("replacement", [False, True])
def test_origin_change_while_snapshot_writer_waits_refreshes_cached_run_and_refuses_write(
    vcs_env: Env, replacement: bool
) -> None:
    async def exercise() -> None:
        engine = vcs_env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        ready, start, waiting = asyncio.Event(), asyncio.Event(), asyncio.Event()
        terminal: dict[str, object] | None = None

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

            async def store() -> bool:
                async with factory.begin() as session:
                    cached = await session.get(Run, RUN_ATTEMPTED)
                    assert cached is not None and cached.state == "running"
                    ready.set()
                    await start.wait()
                    try:
                        await SqlAlchemyRunRepository(
                            session=session, allow_unscoped=True
                        ).store_diff_snapshots(
                            RUN_ATTEMPTED,
                            cached.code_change_id,
                            HEAD,
                            [DiffSnapshot(filename="fresh.py", patch="fresh")],
                        )
                    except RunCancelled:
                        return False
                    return True

            async with factory.begin() as source:
                await source.execute(
                    text("SELECT id FROM provider_installations WHERE id=:id FOR UPDATE"),
                    {"id": INSTALL_A},
                )
                task = asyncio.create_task(store())
                await asyncio.wait_for(ready.wait(), 5)
                start.set()
                await asyncio.wait_for(waiting.wait(), 5)
                assert not task.done()
                if replacement:
                    await source.execute(
                        text(
                            "UPDATE provider_installations SET deleted_at=clock_timestamp() "
                            "WHERE id=:id"
                        ),
                        {"id": INSTALL_A},
                    )
                    await source.execute(
                        text("UPDATE provider_installations SET external_id=29 WHERE id=:id"),
                        {"id": INSTALL_B},
                    )
                    await source.execute(
                        text("SELECT id FROM repositories WHERE id=:id FOR UPDATE"), {"id": REPO_A}
                    )
                    await source.execute(
                        text(
                            "UPDATE repositories SET provider_installation_id=:origin WHERE id=:id"
                        ),
                        {"origin": INSTALL_B, "id": REPO_A},
                    )
                    await source.execute(
                        text(
                            "UPDATE runs SET state='cancelled',error_code='installation_replaced' "
                            "WHERE id=:id"
                        ),
                        {"id": RUN_ATTEMPTED},
                    )
                    terminal = await history_in(source)
            assert await asyncio.wait_for(task, 5) is (not replacement)
            if replacement:
                assert terminal is not None
                assert await history(factory) == terminal
        finally:
            event.remove(engine.sync_engine, "before_cursor_execute", observe)
            await engine.dispose()

    asyncio.run(exercise())
