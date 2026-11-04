"""PostgreSQL restart and invalidation proof for pinned tree pages (#360)."""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from dataclasses import replace
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import Connection, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.modules.code_index.application.bitbucket_persistence import SourceListingPage
from app.modules.code_index.application.vcs import ArchiveEntry, VcsUnavailable
from app.modules.code_index.domain.enums import PurgeReason
from app.modules.code_index.infrastructure.bitbucket_snapshot_source import (
    BitbucketRepositorySnapshotSource,
    BitbucketSourceBudgets,
)
from app.modules.code_index.infrastructure.bitbucket_tree_checkpoint import (
    SqlAlchemyBitbucketPublishedManifest,
    SqlAlchemyBitbucketTreeCheckpoint,
)
from app.modules.code_index.infrastructure.index_store import SqlAlchemyCodeIndexStore
from app.modules.integrations.bitbucket.application.request_credentials import (
    BitbucketRequestCredential,
    BoundBitbucketTarget,
)
from tests.test_code_index_store_postgres import _repository

pytest_plugins = ("tests.test_code_index_store_postgres",)


@pytest.mark.integration
def test_published_manifest_requires_matching_publication_binding_and_generation(
    code_index_database: Connection,
) -> None:
    db = code_index_database
    repository_id = UUID(_repository(db))
    db.execute(
        text(
            "INSERT INTO code_index_states "
            "(repository_id, status, indexed_commit_sha, published_publication, generation) "
            "VALUES (:id, 'ready', :sha, 1, 4)"
        ),
        {"id": repository_id, "sha": "a" * 40},
    )
    schema = str(db.scalar(text("SELECT current_schema()")))
    db.commit()
    target = BoundBitbucketTarget(uuid4(), uuid4(), repository_id, uuid4(), 1, 3)
    url = make_url(os.environ["TEST_DATABASE_URL"]).set(drivername="postgresql+psycopg_async")
    engine = create_async_engine(url, connect_args={"options": f"-csearch_path={schema}"})
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    entries = {"a.py": ArchiveEntry("a.py", 7)}

    async def exercise() -> None:
        try:
            manifest = SqlAlchemyBitbucketPublishedManifest(sessions)
            assert not await manifest.save(target, "b" * 40, entries)
            assert await manifest.load(target, "a" * 40) is None
            assert await manifest.save(target, "a" * 40, entries)
            restarted = SqlAlchemyBitbucketPublishedManifest(sessions)
            assert await restarted.load(target, "a" * 40) == entries
            assert await restarted.load(replace(target, binding_generation=2), "a" * 40) is None
            async with sessions.begin() as session:
                await session.execute(
                    text("UPDATE code_index_states SET status='building' WHERE repository_id=:id"),
                    {"id": repository_id},
                )
            assert await restarted.load(target, "a" * 40) == entries
            async with sessions.begin() as session:
                await session.execute(
                    text("UPDATE code_index_states SET generation=5 WHERE repository_id=:id"),
                    {"id": repository_id},
                )
            assert await restarted.load(target, "a" * 40) is None
            assert not await manifest.save(target, "a" * 40, entries)
            async with sessions.begin() as session:
                await session.execute(
                    text(
                        "UPDATE code_index_states SET generation=4, status='ready' "
                        "WHERE repository_id=:id"
                    ),
                    {"id": repository_id},
                )
                await SqlAlchemyCodeIndexStore(session).purge(repository_id, PurgeReason.DISABLED)
            assert await restarted.load(target, "a" * 40) is None
        finally:
            await engine.dispose()

    asyncio.run(exercise())


@pytest.mark.integration
def test_checkpoint_survives_restart_and_invalidates_on_binding_sha_and_purge(
    code_index_database: Connection,
) -> None:
    db = code_index_database
    repository_id = UUID(_repository(db))
    db.execute(
        text("INSERT INTO code_index_states (repository_id) VALUES (:id)"),
        {"id": repository_id},
    )
    schema = str(db.scalar(text("SELECT current_schema()")))
    db.commit()
    target = BoundBitbucketTarget(uuid4(), uuid4(), repository_id, uuid4(), 1, 3)
    page = SourceListingPage(
        "a" * 40,
        "",
        {"pagelen": "100"},
        {"size": 2, "values": [{"path": "a.py"}], "next": "page=2"},
        317,
    )
    url = make_url(os.environ["TEST_DATABASE_URL"]).set(drivername="postgresql+psycopg_async")
    engine = create_async_engine(url, connect_args={"options": f"-csearch_path={schema}"})
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def exercise() -> None:
        try:
            first = SqlAlchemyBitbucketTreeCheckpoint(sessions)
            await first.charge(target, page.target_sha, page.directory, page.params)
            await first.add_bytes(target, page.target_sha, page.directory, page.params, 17)
            interrupted = SqlAlchemyBitbucketTreeCheckpoint(sessions)
            assert await interrupted.load(target, page.target_sha) == (
                replace(page, payload={}, bytes_read=17, complete=False),
            )
            await interrupted.charge(target, page.target_sha, page.directory, page.params)
            await interrupted.add_bytes(
                target, page.target_sha, page.directory, page.params, page.bytes_read
            )
            await first.save(target, page)
            restarted = SqlAlchemyBitbucketTreeCheckpoint(sessions)
            assert await restarted.load(target, "a" * 40) == (
                replace(page, attempts=2, bytes_read=334),
            )
            rebound = replace(target, binding_generation=4)
            assert await restarted.load(rebound, "a" * 40) == ()
            assert await restarted.load(target, "a" * 40) == ()
            await restarted.charge(rebound, page.target_sha, page.directory, page.params)
            await restarted.add_bytes(
                rebound, page.target_sha, page.directory, page.params, page.bytes_read
            )
            await restarted.save(rebound, page)
            assert await restarted.load(rebound, "b" * 40) == ()
            changed_sha = replace(page, target_sha="b" * 40)
            await restarted.charge(rebound, changed_sha.target_sha, page.directory, page.params)
            await restarted.add_bytes(
                rebound, changed_sha.target_sha, page.directory, page.params, page.bytes_read
            )
            await restarted.save(rebound, changed_sha)
            assert len(await restarted.load(rebound, "b" * 40)) == 1
            async with sessions.begin() as session:
                await SqlAlchemyCodeIndexStore(session).purge(repository_id, PurgeReason.DISABLED)
            assert await restarted.load(rebound, "b" * 40) == ()
        finally:
            await engine.dispose()

    asyncio.run(exercise())
    assert (
        db.scalar(
            text("SELECT count(*) FROM bitbucket_tree_checkpoint_pages WHERE repository_id = :id"),
            {"id": repository_id},
        )
        == 0
    )


@pytest.mark.integration
def test_partial_stream_bytes_bound_a_restarted_snapshot(
    code_index_database: Connection,
) -> None:
    db = code_index_database
    repository_id = UUID(_repository(db))
    db.execute(
        text("INSERT INTO code_index_states (repository_id) VALUES (:id)"),
        {"id": repository_id},
    )
    schema = str(db.scalar(text("SELECT current_schema()")))
    db.commit()
    target = BoundBitbucketTarget(uuid4(), uuid4(), repository_id, uuid4(), 1, 3)
    url = make_url(os.environ["TEST_DATABASE_URL"]).set(drivername="postgresql+psycopg_async")
    engine = create_async_engine(url, connect_args={"options": f"-csearch_path={schema}"})
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    partial = b" " * 25
    complete = b'{"values":[],"size":0}'
    requests = 0

    class Interrupted(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield partial
            raise httpx.ReadError("stream interrupted")

    class Credentials:
        async def credential_for(self, bound: BoundBitbucketTarget) -> BitbucketRequestCredential:
            assert bound == target
            return BitbucketRequestCredential(bound, "test-token")

        async def checkpoint(self, bound: BoundBitbucketTarget) -> None:
            assert bound == target

    async def resolve() -> BoundBitbucketTarget:
        return target

    def handle(_: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        if requests == 1:
            return httpx.Response(200, stream=Interrupted())
        return httpx.Response(200, content=complete)

    async def exercise() -> None:
        try:
            async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
                checkpoint = SqlAlchemyBitbucketTreeCheckpoint(sessions)
                budgets = BitbucketSourceBudgets(read_bytes=len(partial) + len(complete) - 1)

                def source() -> BitbucketRepositorySnapshotSource:
                    return BitbucketRepositorySnapshotSource(
                        client=client,
                        target_for_repository=resolve,
                        credentials=Credentials(),
                        budgets=budgets,
                        checkpoint=checkpoint,
                    )

                with pytest.raises(VcsUnavailable, match="source request failed"):
                    await source().archive("a" * 40, max_bytes=100_000)
                stored = await checkpoint.load(target, "a" * 40)
                assert len(stored) == 1
                assert stored[0].bytes_read == 25 and not stored[0].complete
                with pytest.raises(VcsUnavailable, match="work budget exhausted"):
                    await source().archive("a" * 40, max_bytes=100_000)
        finally:
            await engine.dispose()

    asyncio.run(exercise())
    assert requests == 2
