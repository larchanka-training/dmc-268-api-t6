"""PostgreSQL evidence for the index a review opens (#144, ADR 0001 §4, §6.1).

A review reads the state row of its repository under the effective RAG condition and
never waits for a build: a row another session holds is read as committed, with no write.
A writer of the repository row is waited for only until the gate's lock timeout. The
row is locked only when the review writes, and the writes are decided again under the lock.
Under its own lock the review requests an update of a `stale` index and a full of a live
`empty` one (at most once a day and once per target, never while a build holds the lease),
brings a TTL-purged index back in a new generation, and extends `last_used_at` at most once
an hour, so a TTL purge spares an index reviews still read. A `failed`, a removed or
disabled index and a missing row take no write; with RAG off or a GitLab installation the
state is not read.

Opt-in: TEST_DATABASE_URL must target a test database whose user can create schemas.
Each test migrates its own schema to head and drops it afterwards.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from functools import partial
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import CreateSchema, DropSchema

from alembic import command
from app.modules.code_index.domain.enums import PurgeReason
from app.modules.code_index.infrastructure.index_store import SqlAlchemyCodeIndexStore
from app.modules.code_index.infrastructure.models import CodeIndexState
from app.modules.code_index.infrastructure.review_index import (
    SqlAlchemyReviewIndexAccess,
    SqlAlchemyReviewIndexUnitOfWork,
)
from app.modules.reviews.application.rag_index import OpenReviewIndex, RagReadiness
from tests.test_code_index_store_postgres import (
    C1,
    C2,
    _build,
    _claim,
    _enable,
    _Index,
    _mark_failed,
    _migration_config,
    _module,
    _purge,
    _purge_unused,
    _repository,
    _rows,
    _unused_for_31_days,
    _wait_until_blocked,
)


@dataclass(frozen=True)
class _Reviews:
    index: _Index
    # Four repositories with RAG on, each in a Workspace and installation of its own;
    # `index.a` and `index.b` are the first two.
    ids: tuple[UUID, ...]


@pytest.fixture
def reviews() -> Iterator[_Reviews]:
    database_url = os.environ.get("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("set TEST_DATABASE_URL to run PostgreSQL integration tests")
    schema = f"test_code_index_{uuid4().hex}"
    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            connection.execute(CreateSchema(schema))
            connection.execute(text(f'SET search_path TO "{schema}"'))
            connection.commit()
            try:
                command.upgrade(_migration_config(connection), "head")
                ids = tuple(
                    UUID(_repository(connection, installation_external_id=17 + number))
                    for number in range(4)
                )
                # `_repository` leaves the toggle off.
                connection.execute(text("UPDATE repositories SET rag_enabled = true"))
                connection.commit()
                async_engine = create_async_engine(
                    database_url,
                    connect_args={"options": f"-csearch_path={schema}"},
                    poolclass=NullPool,
                )
                try:
                    factory = async_sessionmaker(async_engine, expire_on_commit=False)
                    yield _Reviews(_Index(factory, ids[0], ids[1]), ids)
                finally:
                    asyncio.run(async_engine.dispose())
            finally:
                connection.rollback()
                connection.execute(text("SET search_path TO public"))
                connection.execute(DropSchema(schema, cascade=True))
                connection.commit()
    finally:
        engine.dispose()


async def _review(index: _Index, repository_id: UUID) -> RagReadiness:
    use_case = OpenReviewIndex(partial(SqlAlchemyReviewIndexUnitOfWork, index.factory))
    return await use_case.execute(repository_id)


def _seen(readiness: RagReadiness) -> tuple[object, ...]:
    """What the review sees: the status, the indexed commit, the stored code and the target."""
    assert readiness.enabled and readiness.index is not None
    index = readiness.index
    return (
        index.status,
        index.indexed_commit_sha,
        index.last_error_code,
        readiness.default_head_sha,
    )


# The state columns a review may write, with `xmin`: any update of the row changes it, a
# row lock does not.
_ROW = (
    "SELECT xmin::text AS version, generation, status::text AS status, "
    "purge_reason::text AS purge_reason, target_commit_sha, wake_requested_at, last_used_at, "
    "review_full_requested_at, review_full_requested_sha "
    "FROM code_index_states WHERE repository_id = :repository"
)


async def _row(index: _Index, repository_id: UUID) -> dict[str, Any] | None:
    async with index.factory() as session:
        row = (
            (await session.execute(text(_ROW), {"repository": repository_id}))
            .mappings()
            .one_or_none()
        )
    return None if row is None else dict(row)


async def _clock(index: _Index) -> datetime:
    async with index.factory() as session:
        return cast(datetime, await session.scalar(text("SELECT clock_timestamp()")))


async def _set(index: _Index, repository_id: UUID, assignments: str, **values: object) -> None:
    async with index.factory.begin() as session:
        await session.execute(
            text(f"UPDATE code_index_states SET {assignments} WHERE repository_id = :repository"),
            {"repository": repository_id, **values},
        )


async def _stale(index: _Index, repository_id: UUID) -> None:
    """A published index at C1 whose target moved to C2, with no marker and a use 2 h ago."""
    await _enable(index, repository_id)
    await _build(index, repository_id, C1, _module("aa01", 1))
    await _set(
        index,
        repository_id,
        "target_commit_sha = :target, wake_requested_at = NULL, "
        "last_used_at = clock_timestamp() - interval '2 hours'",
        target=C2,
    )


async def _ttl_purged(index: _Index, repository_id: UUID) -> None:
    await _enable(index, repository_id)
    await _build(index, repository_id, C1, _module("aa01", 1))
    await _purge(index, repository_id, PurgeReason.TTL)


_RAG_OFF = "UPDATE repositories SET rag_enabled = false WHERE id = :repository"
_GITLAB = (
    "UPDATE provider_installations SET provider = 'gitlab' "
    "WHERE id = (SELECT provider_installation_id FROM repositories WHERE id = :repository)"
)


@pytest.mark.integration
def test_a_stale_index_requests_an_update_and_keeps_its_target(reviews: _Reviews) -> None:
    async def scenario() -> None:
        index = reviews.index
        await _stale(index, index.a)
        before = await _row(index, index.a)
        assert before is not None and before["wake_requested_at"] is None
        start = await _clock(index)

        readiness = await asyncio.wait_for(_review(index, index.a), 5)

        end = await _clock(index)
        # The review reads the stale index at its commit; it does not wait for the update.
        assert _seen(readiness) == ("stale", C1, None, C2)
        assert readiness.index is not None and readiness.index.usable
        after = await _row(index, index.a)
        assert after is not None
        assert (after["generation"], after["status"], after["target_commit_sha"]) == (
            1,
            "ready",
            C2,
        )
        assert start <= after["wake_requested_at"] <= end
        assert start <= after["last_used_at"] <= end
        assert (after["review_full_requested_at"], after["review_full_requested_sha"]) == (
            None,
            None,
        )

        # A terminal refusal of the target asks for nothing; a healable code is retried.
        await _set(
            index,
            index.a,
            "wake_requested_at = NULL, last_error_code = 'vcs_access_denied'",
        )
        refused = await _row(index, index.a)
        readiness = await _review(index, index.a)
        assert _seen(readiness) == ("stale", C1, "vcs_access_denied", C2)
        assert await _row(index, index.a) == refused

        await _set(index, index.a, "wake_requested_at = NULL, last_error_code = 'vcs_unavailable'")
        start = await _clock(index)
        await _review(index, index.a)
        retried = await _row(index, index.a)
        assert retried is not None
        assert start <= retried["wake_requested_at"] <= await _clock(index)
        assert retried["target_commit_sha"] == C2

    asyncio.run(scenario())


@pytest.mark.integration
def test_a_ttl_purged_index_comes_back_in_a_new_generation_once_a_day(
    reviews: _Reviews,
) -> None:
    async def scenario() -> None:
        index = reviews.index
        await _ttl_purged(index, index.a)
        purged = await _row(index, index.a)
        assert purged is not None
        assert (purged["generation"], purged["purge_reason"], purged["wake_requested_at"]) == (
            2,
            "ttl",
            None,
        )
        start = await _clock(index)

        readiness = await _review(index, index.a)

        end = await _clock(index)
        assert _seen(readiness) == ("empty", None, None, None)
        assert readiness.index is not None and readiness.index.not_ready_reason == "empty"
        back = await _row(index, index.a)
        assert back is not None
        assert (back["generation"], back["status"], back["purge_reason"]) == (3, "empty", None)
        assert start <= back["wake_requested_at"] <= end
        assert start <= back["last_used_at"] <= end
        # The purge cleared the target, so only the time is recorded.
        assert start <= back["review_full_requested_at"] <= end
        assert back["review_full_requested_sha"] is None

        # The daily limit holds across a TTL purge: a second one within the day stays purged.
        await _purge(index, index.a, PurgeReason.TTL)
        again = await _row(index, index.a)
        assert again is not None
        assert (again["generation"], again["purge_reason"]) == (4, "ttl")
        assert again["review_full_requested_at"] == back["review_full_requested_at"]
        assert _seen(await _review(index, index.a)) == ("empty", None, None, None)
        assert await _row(index, index.a) == again

    asyncio.run(scenario())


@pytest.mark.integration
def test_a_review_full_is_queued_once_a_day_and_once_per_target(reviews: _Reviews) -> None:
    async def scenario() -> None:
        index = reviews.index
        await _enable(index, index.a)
        # A live empty index for C1 whose wake-up is spent, last used 2 h ago.
        await _set(
            index,
            index.a,
            "target_commit_sha = :target, wake_requested_at = NULL, "
            "last_used_at = clock_timestamp() - interval '2 hours'",
            target=C1,
        )
        start = await _clock(index)

        assert _seen(await _review(index, index.a)) == ("empty", None, None, C1)

        end = await _clock(index)
        queued = await _row(index, index.a)
        assert queued is not None
        assert (queued["generation"], queued["status"], queued["target_commit_sha"]) == (
            1,
            "empty",
            C1,
        )
        assert start <= queued["wake_requested_at"] <= end
        assert start <= queued["last_used_at"] <= end
        assert start <= queued["review_full_requested_at"] <= end
        assert queued["review_full_requested_sha"] == C1

        # A repeat within 24 h queues no full and extends nothing.
        await _set(index, index.a, "wake_requested_at = NULL")
        spent = await _row(index, index.a)
        assert _seen(await _review(index, index.a)) == ("empty", None, None, C1)
        assert await _row(index, index.a) == spent

        # After 24 h the same target is still refused.
        await _set(
            index,
            index.a,
            "review_full_requested_at = review_full_requested_at - interval '24 hours'",
        )
        day_later = await _row(index, index.a)
        await _review(index, index.a)
        assert await _row(index, index.a) == day_later

        # A new target is allowed.
        await _set(index, index.a, "target_commit_sha = :target", target=C2)
        start = await _clock(index)
        assert _seen(await _review(index, index.a)) == ("empty", None, None, C2)
        end = await _clock(index)
        renewed = await _row(index, index.a)
        assert renewed is not None
        assert start <= renewed["wake_requested_at"] <= end
        assert start <= renewed["review_full_requested_at"] <= end
        assert renewed["review_full_requested_sha"] == C2
        # The first full extended `last_used_at` within the hour, so this one does not.
        assert renewed["last_used_at"] == queued["last_used_at"]

    asyncio.run(scenario())


@pytest.mark.integration
def test_a_review_wakes_no_index_while_a_build_holds_its_lease(reviews: _Reviews) -> None:
    async def scenario() -> None:
        index = reviews.index
        await _stale(index, index.a)
        await _claim(index, index.a)
        start = await _clock(index)

        readiness = await asyncio.wait_for(_review(index, index.a), 5)

        end = await _clock(index)
        assert _seen(readiness) == ("stale", C1, None, C2)
        leased = await _row(index, index.a)
        assert leased is not None
        assert leased["wake_requested_at"] is None
        # The hourly extension still applies: `_stale` left the use 2 h old.
        assert start <= leased["last_used_at"] <= end

    asyncio.run(scenario())


@pytest.mark.integration
def test_a_review_wakes_an_index_whose_build_lease_expired(reviews: _Reviews) -> None:
    async def scenario() -> None:
        index = reviews.index
        await _stale(index, index.a)
        await _claim(index, index.a)
        # The build stopped without releasing: its lease is still on the row, but over.
        await _set(index, index.a, "lease_expires_at = clock_timestamp() - interval '1 minute'")
        start = await _clock(index)

        readiness = await asyncio.wait_for(_review(index, index.a), 5)

        end = await _clock(index)
        assert _seen(readiness) == ("stale", C1, None, C2)
        expired = await _row(index, index.a)
        assert expired is not None and expired["wake_requested_at"] is not None
        assert start <= expired["wake_requested_at"] <= end
        assert expired["target_commit_sha"] == C2

    asyncio.run(scenario())


@pytest.mark.integration
def test_a_failed_a_removed_a_disabled_and_a_missing_index_take_no_write(
    reviews: _Reviews,
) -> None:
    async def scenario() -> None:
        index = reviews.index
        failed, removed, disabled, missing = reviews.ids
        await _enable(index, failed)
        fence = await _claim(index, failed)
        await _mark_failed(index, failed, fence, "limit_exceeded:symbols")
        for repository_id, reason in (
            (removed, PurgeReason.REMOVED),
            (disabled, PurgeReason.DISABLED),
        ):
            await _ttl_purged(index, repository_id)
            # A later removal or toggle-off replaces the TTL reason; the converge pass owns it.
            await _purge(index, repository_id, reason)
        for repository_id in (failed, removed, disabled):
            await _unused_for_31_days(index, repository_id)
        before = {repository_id: await _row(index, repository_id) for repository_id in reviews.ids}
        assert before[missing] is None

        seen = [_seen(await _review(index, repository_id)) for repository_id in reviews.ids]

        assert seen == [
            ("failed", None, "limit_exceeded:symbols", None),
            ("empty", None, None, None),
            ("empty", None, None, None),
            ("empty", None, None, None),
        ]
        assert {
            repository_id: await _row(index, repository_id) for repository_id in reviews.ids
        } == (before)
        assert await _rows(
            index,
            "SELECT count(*) FROM code_index_states WHERE repository_id = :repository",
            repository=missing,
        ) == [(0,)]

    asyncio.run(scenario())


@pytest.mark.integration
def test_a_usable_index_extends_last_used_at_at_most_once_an_hour(reviews: _Reviews) -> None:
    async def scenario() -> None:
        index = reviews.index
        await _enable(index, index.a)
        await _build(index, index.a, C1, _module("aa01", 1))
        await _set(index, index.a, "last_used_at = clock_timestamp() - interval '59 minutes'")
        recent = await _row(index, index.a)

        assert _seen(await _review(index, index.a)) == ("ready", C1, None, None)
        assert await _row(index, index.a) == recent

        for assignment in (
            "last_used_at = clock_timestamp() - interval '61 minutes'",
            "last_used_at = NULL",
        ):
            await _set(index, index.a, assignment)
            old = await _row(index, index.a)
            assert old is not None
            start = await _clock(index)
            assert _seen(await _review(index, index.a)) == ("ready", C1, None, None)
            extended = await _row(index, index.a)
            assert extended is not None
            assert start <= extended["last_used_at"] <= await _clock(index)
            # A ready index requests nothing.
            assert extended["wake_requested_at"] == old["wake_requested_at"]
            assert extended["generation"] == 1

    asyncio.run(scenario())


@pytest.mark.integration
def test_a_review_keeps_its_index_from_the_ttl_purge_and_a_publication_does_not(
    reviews: _Reviews,
) -> None:
    async def scenario() -> None:
        index = reviews.index
        for repository_id in (index.a, index.b):
            await _enable(index, repository_id)
            await _build(index, repository_id, C1, _module("aa01", 1))
            await _unused_for_31_days(index, repository_id)

        await _review(index, index.a)

        assert await _purge_unused(index, index.a) is False
        assert await _purge_unused(index, index.b) is True
        used = await _row(index, index.a)
        assert used is not None
        await _build(index, index.a, C2, _module("aa02", 1), base=C1, full=False)
        published = await _row(index, index.a)
        assert published is not None
        assert published["last_used_at"] == used["last_used_at"]

    asyncio.run(scenario())


@pytest.mark.integration
def test_a_review_waits_for_a_toggle_off_and_does_not_resurrect_the_index(
    reviews: _Reviews,
) -> None:
    async def scenario() -> None:
        index = reviews.index
        await _ttl_purged(index, index.a)

        # PATCH `ragEnabled=false` in flight: the flag and the purge in one transaction.
        async with index.factory() as toggle:
            await toggle.execute(text(_RAG_OFF), {"repository": index.a})
            await SqlAlchemyCodeIndexStore(toggle).purge(index.a, PurgeReason.DISABLED)
            toggle_pid = await toggle.scalar(text("SELECT pg_backend_pid()"))
            review = asyncio.create_task(_review(index, index.a))
            await _wait_until_blocked(index, toggle_pid, 1, review)
            await toggle.commit()

        assert await asyncio.wait_for(review, 5) == RagReadiness(enabled=False)
        row = await _row(index, index.a)
        assert row is not None
        assert (
            row["generation"],
            row["status"],
            row["purge_reason"],
            row["wake_requested_at"],
            row["review_full_requested_at"],
        ) == (2, "empty", "disabled", None, None)

    asyncio.run(scenario())


@pytest.mark.integration
def test_a_review_skips_a_locked_index_row_and_writes_nothing(reviews: _Reviews) -> None:
    async def scenario() -> None:
        index = reviews.index
        await _stale(index, index.a)
        await _ttl_purged(index, index.b)
        before = {
            repository_id: await _row(index, repository_id) for repository_id in (index.a, index.b)
        }

        async with index.factory() as holder:
            await holder.execute(
                text("SELECT 1 FROM code_index_states WHERE repository_id IN (:a, :b) FOR UPDATE"),
                {"a": index.a, "b": index.b},
            )
            # Both return while the row is held: the committed row, read-only.
            stale = await asyncio.wait_for(_review(index, index.a), 5)
            purged = await asyncio.wait_for(_review(index, index.b), 5)
            await holder.rollback()

        assert _seen(stale) == ("stale", C1, None, C2)
        assert _seen(purged) == ("empty", None, None, None)
        assert {
            repository_id: await _row(index, repository_id) for repository_id in (index.a, index.b)
        } == (before)

    asyncio.run(scenario())


_LOCK_NOWAIT = "SELECT 1 FROM code_index_states WHERE repository_id = :repository FOR UPDATE NOWAIT"


@pytest.mark.integration
def test_a_review_locks_the_index_row_only_when_it_writes(reviews: _Reviews) -> None:
    async def scenario() -> None:
        index = reviews.index
        # A: a ready index used 59 min ago, so the review writes nothing. B: a stale one,
        # so the review requests an update.
        await _enable(index, index.a)
        await _build(index, index.a, C1, _module("aa01", 1))
        await _set(index, index.a, "last_used_at = clock_timestamp() - interval '59 minutes'")
        await _stale(index, index.b)
        recent = await _row(index, index.a)

        async with index.factory() as reader, index.factory() as writer:
            read = await SqlAlchemyReviewIndexAccess(reader).open_for_review(index.a)
            written = await SqlAlchemyReviewIndexAccess(writer).open_for_review(index.b)
            # Both review transactions are still open.
            async with index.factory() as probe:
                await probe.execute(text(_LOCK_NOWAIT), {"repository": index.a})
                await probe.rollback()
                with pytest.raises(OperationalError) as held:
                    await probe.execute(text(_LOCK_NOWAIT), {"repository": index.b})
                await probe.rollback()
            await reader.rollback()
            await writer.rollback()

        assert _seen(read) == ("ready", C1, None, None)
        assert _seen(written) == ("stale", C1, None, C2)
        assert getattr(held.value.orig, "sqlstate", None) == "55P03"
        assert await _row(index, index.a) == recent

    asyncio.run(scenario())


@pytest.mark.integration
def test_a_review_decides_its_writes_again_under_the_index_row_lock(
    reviews: _Reviews, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        index = reviews.index
        await _stale(index, index.a)
        marked: dict[str, Any] = {}
        try_lock = SqlAlchemyReviewIndexAccess._try_lock_state

        async def marked_first(
            access: SqlAlchemyReviewIndexAccess, repository_id: UUID
        ) -> CodeIndexState | None:
            # After the review read the row without a lock, another session queues the
            # update and uses the index, before the review takes the lock.
            await _set(
                index,
                repository_id,
                "wake_requested_at = clock_timestamp(), last_used_at = clock_timestamp()",
            )
            row = await _row(index, repository_id)
            assert row is not None
            marked.update(row)
            return await try_lock(access, repository_id)

        monkeypatch.setattr(SqlAlchemyReviewIndexAccess, "_try_lock_state", marked_first)

        readiness = await asyncio.wait_for(_review(index, index.a), 5)

        assert _seen(readiness) == ("stale", C1, None, C2)
        assert marked, "the review did not take the row lock"
        # Under the lock the update is already queued and the use is recent: no write.
        assert await _row(index, index.a) == marked

    asyncio.run(scenario())


@pytest.mark.integration
@pytest.mark.parametrize("statement", [_RAG_OFF, _GITLAB], ids=["rag_off", "gitlab"])
def test_rag_off_or_a_gitlab_installation_reads_no_index_state(
    reviews: _Reviews, statement: str
) -> None:
    async def scenario() -> None:
        index = reviews.index
        for repository_id in (index.a, index.b):
            await _ttl_purged(index, repository_id)
        async with index.factory.begin() as session:
            await session.execute(text(statement), {"repository": index.a})
        before = await _row(index, index.a)

        async with index.factory() as holder:
            # Any read of the state table waits for this lock.
            await holder.execute(text("LOCK TABLE code_index_states IN ACCESS EXCLUSIVE MODE"))
            holder_pid = await holder.scalar(text("SELECT pg_backend_pid()"))
            assert await asyncio.wait_for(_review(index, index.a), 5) == RagReadiness(enabled=False)
            # A repository with RAG on does read it, so the lock is in the way.
            enabled = asyncio.create_task(_review(index, index.b))
            await _wait_until_blocked(index, holder_pid, 1, enabled)
            await holder.rollback()

        assert _seen(await asyncio.wait_for(enabled, 5)) == ("empty", None, None, None)
        assert await _row(index, index.a) == before

    asyncio.run(scenario())


@pytest.mark.integration
def test_rag_off_is_read_without_waiting_for_a_writer_of_the_repository(
    reviews: _Reviews,
) -> None:
    async def scenario() -> None:
        index = reviews.index
        await _stale(index, index.a)
        async with index.factory.begin() as session:
            await session.execute(text(_RAG_OFF), {"repository": index.a})
        before = await _row(index, index.a)

        async with index.factory() as holder:
            # A committed toggle-off, then any writer of the repository row in flight.
            await holder.execute(
                text("SELECT 1 FROM repositories WHERE id = :repository FOR UPDATE"),
                {"repository": index.a},
            )
            assert await asyncio.wait_for(_review(index, index.a), 5) == RagReadiness(enabled=False)
            await holder.rollback()

        assert await _row(index, index.a) == before

    asyncio.run(scenario())


@pytest.mark.integration
def test_rag_on_waits_for_a_writer_of_the_repository_only_until_the_lock_timeout(
    reviews: _Reviews,
) -> None:
    async def scenario() -> None:
        index = reviews.index
        await _stale(index, index.a)
        before = await _row(index, index.a)

        async with index.factory() as holder:
            # A long writer of the repository row in flight, with RAG on.
            await holder.execute(
                text("SELECT 1 FROM repositories WHERE id = :repository FOR UPDATE"),
                {"repository": index.a},
            )
            started = time.monotonic()
            with pytest.raises(OperationalError) as caught:
                await asyncio.wait_for(_review(index, index.a), 5)
            waited = time.monotonic() - started
            await holder.rollback()

        # The gate fails after its 2 s lock timeout, well before the run deadline.
        assert getattr(caught.value.orig, "sqlstate", None) == "55P03"
        assert 1.5 <= waited < 3
        assert await _row(index, index.a) == before

    asyncio.run(scenario())


@pytest.mark.integration
def test_the_review_lock_timeout_ends_with_the_gate_transaction(reviews: _Reviews) -> None:
    async def scenario() -> None:
        index = reviews.index
        for repository_id in (index.a, index.b):
            await _stale(index, repository_id)
        async with index.factory() as session:
            schema = await session.scalar(text("SELECT current_schema()"))
        # One pooled connection: a setting that outlives the gate transaction stays on it.
        engine = create_async_engine(
            os.environ["TEST_DATABASE_URL"],
            connect_args={"options": f"-csearch_path={schema}"},
            pool_size=1,
            max_overflow=0,
        )
        try:
            pooled = async_sessionmaker(engine, expire_on_commit=False)
            review = OpenReviewIndex(partial(SqlAlchemyReviewIndexUnitOfWork, pooled))

            async def setting() -> tuple[object, object]:
                async with pooled() as session:
                    return (
                        await session.scalar(text("SELECT pg_backend_pid()")),
                        await session.scalar(text("SHOW lock_timeout")),
                    )

            default = await setting()
            # A gate transaction that commits.
            assert _seen(await review.execute(index.a)) == ("stale", C1, None, C2)
            assert await setting() == default
            # A gate transaction that fails on the lock timeout.
            async with index.factory() as holder:
                await holder.execute(
                    text("SELECT 1 FROM repositories WHERE id = :repository FOR UPDATE"),
                    {"repository": index.b},
                )
                with pytest.raises(OperationalError):
                    await asyncio.wait_for(review.execute(index.b), 5)
                await holder.rollback()
            assert await setting() == default
        finally:
            await engine.dispose()

    asyncio.run(scenario())


@pytest.mark.integration
def test_a_review_of_one_repository_never_writes_another(reviews: _Reviews) -> None:
    async def scenario() -> None:
        index = reviews.index
        for repository_id in (index.a, index.b):
            await _stale(index, repository_id)
        other = await _row(index, index.b)

        await _review(index, index.a)

        written = await _row(index, index.a)
        assert written is not None and written["wake_requested_at"] is not None
        assert await _row(index, index.b) == other

        for repository_id in (index.a, index.b):
            await _purge(index, repository_id, PurgeReason.TTL)
        other = await _row(index, index.b)

        await _review(index, index.a)

        back = await _row(index, index.a)
        assert back is not None and back["generation"] == 3
        assert await _row(index, index.b) == other

    asyncio.run(scenario())
