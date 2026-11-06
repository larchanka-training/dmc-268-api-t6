"""The scheduled cleanup of stale run data against real PostgreSQL (#457, SD §10)."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.reviews.application.run_data_retention import PurgeExpiredRunData
from app.modules.reviews.infrastructure.models import CodeChangeDiff, ContextManifest, Run
from app.modules.reviews.infrastructure.run_data_retention import (
    SqlAlchemyRunDataRetentionUnitOfWork,
)
from tests.portal_postgres import (
    HEAD,
    PR_CLOSED,
    PR_OPEN,
    REPO_A,
    RULE_A,
    RUN_CLOSED,
    RUN_DONE,
    Env,
)

pytestmark = pytest.mark.integration
pytest_plugins = ("tests.portal_postgres",)

Factory = async_sessionmaker[AsyncSession]

_NOW = datetime(2026, 11, 6, 12, tzinfo=UTC)
_PERIOD = timedelta(days=90)
_OLD = _NOW - _PERIOD - timedelta(seconds=1)
_PATCH = (
    "diff --git a/src/a.py b/src/a.py\n--- a/src/a.py\n+++ b/src/a.py\n@@ -1 +1 @@\n-old\n+new\n"
)
_REVIEW_PATCH = "@@ -1 +1 @@\n-old\n+new\n"


def _on_database(env: Env, body: Callable[[Factory], Awaitable[None]]) -> None:
    async def run() -> None:
        engine = env.engine()
        try:
            await body(async_sessionmaker(engine, expire_on_commit=False))
        finally:
            await engine.dispose()

    asyncio.run(run())


def _task(factory: Factory, now: datetime = _NOW) -> PurgeExpiredRunData:
    return PurgeExpiredRunData(
        uow=partial(SqlAlchemyRunDataRetentionUnitOfWork, factory),
        retention=_PERIOD,
        now=lambda: now,
    )


async def _finish(
    factory: Factory, run_id: UUID, state: str, finished_at: datetime | None = _OLD
) -> None:
    async with factory.begin() as session:
        await session.execute(
            text(
                "UPDATE runs SET state = CAST(:state AS run_state), finished_at = :finished "
                "WHERE id = :run"
            ),
            {"state": state, "finished": finished_at, "run": run_id},
        )


async def _store_run_data(factory: Factory, run_id: UUID, code_change_id: UUID) -> None:
    """Two diff rows and a manifest with its policy snapshot."""
    snapshot_id = uuid4()
    async with factory.begin() as session:
        session.add_all(
            [
                CodeChangeDiff(
                    run_id=run_id,
                    code_change_id=code_change_id,
                    head_sha=HEAD,
                    filename="src/a.py",
                    patch=_PATCH,
                    review_patch=_REVIEW_PATCH,
                    blob_sha="a" * 40,
                    status="modified",
                    additions=1,
                    deletions=1,
                    changes=2,
                ),
                CodeChangeDiff(
                    run_id=run_id,
                    code_change_id=code_change_id,
                    head_sha=HEAD,
                    filename="vendor/big.js",
                    patch=_REVIEW_PATCH,
                    status="added",
                    additions=9_000,
                    changes=9_000,
                    omission_reason="too_large",
                    summary_only=True,
                ),
            ]
        )
        await session.execute(
            text(
                "INSERT INTO run_policy_snapshots (run_id, snapshot_id, repository_id, "
                "schema_version, policy_base_sha, ignore_patterns, ignore_digest, parser_profile, "
                "parser_version, matcher_version, limits_version, builtin_exclusions_version, "
                "priority_matcher_version, service_defaults_rule_version_id, defaults_checksum, "
                "prompt_bindings, execution_profile, policy_digest) VALUES (:run, :snapshot, "
                ":repository, 1, :base, '[]'::jsonb, :zeros, 'rules-markdown/v1', '4.0.0', "
                "'review-ignore/v1', 'limits/v1', 'builtin-exclusions/v1', 'priority/v1', :rule, "
                ":zeros, '{}'::jsonb, '{}'::jsonb, :zeros)"
            ),
            {
                "run": run_id,
                "snapshot": snapshot_id,
                "repository": REPO_A,
                "base": "b" * 40,
                "zeros": "0" * 64,
                "rule": RULE_A,
            },
        )
        session.add(
            ContextManifest(
                id=uuid4(),
                run_id=run_id,
                attempt=1,
                call_id=uuid4(),
                stage="review",
                recovery_kind="none",
                policy_snapshot_id=snapshot_id,
                digest="0" * 64,
                rendered_messages_sha256="0" * 64,
                prompt_checksum="0" * 64,
                model_profile_version="0" * 64,
                block_count=0,
                input_tokens=0,
                blocks=[],
            )
        )


async def _diff_rows(factory: Factory, run_id: UUID) -> list[tuple[Any, ...]]:
    async with factory() as session:
        rows = await session.scalars(
            select(CodeChangeDiff)
            .where(CodeChangeDiff.run_id == run_id)
            .order_by(CodeChangeDiff.filename)
        )
        return [
            (
                row.filename,
                row.patch,
                row.review_patch,
                row.blob_sha,
                row.status,
                row.additions,
                row.deletions,
                row.changes,
                row.omission_reason,
                row.summary_only,
                row.head_sha,
            )
            for row in rows
        ]


async def _manifests(factory: Factory, run_id: UUID) -> int:
    async with factory() as session:
        return len(
            (
                await session.scalars(
                    select(ContextManifest.id).where(ContextManifest.run_id == run_id)
                )
            ).all()
        )


async def _marker(factory: Factory, run_id: UUID) -> datetime | None:
    async with factory() as session:
        return await session.scalar(select(Run.data_purged_at).where(Run.id == run_id))


_PURGED_ROWS = [
    ("src/a.py", None, None, "a" * 40, "modified", 1, 1, 2, None, False, HEAD),
    ("vendor/big.js", None, None, None, "added", 9_000, 0, 9_000, "too_large", True, HEAD),
]
_INTACT_ROWS = [
    ("src/a.py", _PATCH, _REVIEW_PATCH, "a" * 40, "modified", 1, 1, 2, None, False, HEAD),
    ("vendor/big.js", _REVIEW_PATCH, None, None, "added", 9_000, 0, 9_000, "too_large", True, HEAD),
]


@pytest.mark.parametrize("state", ["succeeded", "failed", "cancelled", "skipped"])
def test_a_terminal_run_past_the_period_loses_its_diff_text_and_manifests(
    portal_env: Env, state: str
) -> None:
    async def body(factory: Factory) -> None:
        await _store_run_data(factory, RUN_DONE, PR_OPEN)
        await _finish(factory, RUN_DONE, state)

        assert await _task(factory).run(30) is False

        assert await _diff_rows(factory, RUN_DONE) == _PURGED_ROWS
        assert await _manifests(factory, RUN_DONE) == 0
        assert await _marker(factory, RUN_DONE) == _NOW

    _on_database(portal_env, body)


@pytest.mark.parametrize(
    ("state", "finished_at"),
    [
        pytest.param("succeeded", _NOW - _PERIOD, id="exactly-at-the-period"),
        pytest.param("succeeded", _NOW - timedelta(days=89), id="within-the-period"),
        pytest.param("succeeded", None, id="terminal-without-finished-at"),
        pytest.param("queued", _OLD, id="queued"),
        pytest.param("running", _OLD, id="running"),
        pytest.param("publishing", _OLD, id="publishing"),
    ],
)
def test_a_young_or_unfinished_run_stays_untouched(
    portal_env: Env, state: str, finished_at: datetime | None
) -> None:
    async def body(factory: Factory) -> None:
        await _store_run_data(factory, RUN_DONE, PR_OPEN)
        await _finish(factory, RUN_DONE, state, finished_at)

        assert await _task(factory).run(30) is False

        assert await _diff_rows(factory, RUN_DONE) == _INTACT_ROWS
        assert await _manifests(factory, RUN_DONE) == 1
        assert await _marker(factory, RUN_DONE) is None

    _on_database(portal_env, body)


def test_a_repeated_pass_keeps_the_first_marker(portal_env: Env) -> None:
    async def body(factory: Factory) -> None:
        await _store_run_data(factory, RUN_DONE, PR_OPEN)
        await _finish(factory, RUN_DONE, "succeeded")

        assert await _task(factory).run(30) is False
        assert await _task(factory, _NOW + timedelta(hours=1)).run(30) is False

        assert await _diff_rows(factory, RUN_DONE) == _PURGED_ROWS
        assert await _marker(factory, RUN_DONE) == _NOW

    _on_database(portal_env, body)


def test_two_concurrent_passes_purge_every_run(portal_env: Env) -> None:
    async def body(factory: Factory) -> None:
        for run_id, code_change_id in ((RUN_DONE, PR_OPEN), (RUN_CLOSED, PR_CLOSED)):
            await _store_run_data(factory, run_id, code_change_id)
            await _finish(factory, run_id, "succeeded")

        results = await asyncio.gather(_task(factory).run(30), _task(factory).run(30))

        assert list(results) == [False, False]
        for run_id in (RUN_DONE, RUN_CLOSED):
            assert await _diff_rows(factory, run_id) == _PURGED_ROWS
            assert await _manifests(factory, run_id) == 0
            assert await _marker(factory, run_id) == _NOW

    _on_database(portal_env, body)


def test_a_pass_skips_a_locked_run_without_waiting_and_takes_it_later(portal_env: Env) -> None:
    async def body(factory: Factory) -> None:
        for run_id, code_change_id in ((RUN_DONE, PR_OPEN), (RUN_CLOSED, PR_CLOSED)):
            await _store_run_data(factory, run_id, code_change_id)
            await _finish(factory, run_id, "succeeded")

        async with factory.begin() as holder:
            await holder.execute(select(Run.id).where(Run.id == RUN_DONE).with_for_update())
            await asyncio.wait_for(_task(factory).run(30), timeout=10)
            assert await _marker(factory, RUN_DONE) is None
            assert await _marker(factory, RUN_CLOSED) == _NOW

        assert await _task(factory, _NOW + timedelta(hours=1)).run(30) is False
        assert await _marker(factory, RUN_DONE) == _NOW + timedelta(hours=1)

    _on_database(portal_env, body)
