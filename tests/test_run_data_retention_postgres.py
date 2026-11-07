"""The scheduled cleanup of stale run data against real PostgreSQL (#457, SD §10)."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.auth.application.scope import AuthScope
from app.modules.reviews.application.get_run_file_lines import (
    BlobCacheKey,
    BlobCacheStatus,
)
from app.modules.reviews.application.run_data_retention import PurgeExpiredRunData
from app.modules.reviews.infrastructure.blob_cache import SqlAlchemyBlobCache
from app.modules.reviews.infrastructure.models import (
    CachedFileBlob,
    CodeChangeDiff,
    ContextManifest,
    ContextPayload,
    Run,
    RunAction,
)
from app.modules.reviews.infrastructure.run_data_retention import (
    SqlAlchemyRunDataRetentionUnitOfWork,
)
from app.modules.reviews.infrastructure.run_lifecycle_store import SqlAlchemyRunLifecycleStore
from tests.portal_postgres import (
    HEAD,
    PR_CLOSED,
    PR_OPEN,
    REPO_A,
    RULE_A,
    RUN_CLOSED,
    RUN_DONE,
    WS_A,
    Env,
    api,
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


async def _store_run_data(
    factory: Factory, run_id: UUID, code_change_id: UUID, *, marked: bool = True
) -> None:
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
                    summary_only=marked,
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


async def _updated_at(factory: Factory, run_id: UUID) -> datetime | None:
    """The id of the run's SSE event."""
    async with factory() as session:
        updated_at: datetime | None = await session.scalar(
            select(Run.updated_at).where(Run.id == run_id)
        )
        return updated_at


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
        event_id = await _updated_at(factory, RUN_DONE)

        assert await _task(factory).run(30) is False

        assert await _diff_rows(factory, RUN_DONE) == _PURGED_ROWS
        assert await _manifests(factory, RUN_DONE) == 0
        assert await _marker(factory, RUN_DONE) == _NOW
        assert await _updated_at(factory, RUN_DONE) == event_id

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


_STOP: dict[str, Any] = {
    "status": "summary_only",
    "reason": "diff_inventory_incomplete",
    "blocks": [],
}
_BUILDER: dict[str, Any] = {
    "status": "summary_only",
    "reason": "no_reviewable_files",
    "reviewable_lines": 0,
    "blocks": [],
}
_REVIEW: dict[str, Any] = {"status": "review", "reason": None, "reviewable_lines": 3, "blocks": []}


async def _store_payload(factory: Factory, run_id: UUID, summary: dict[str, Any]) -> None:
    async with factory.begin() as session:
        session.add(ContextPayload(run_id=run_id, schema_version=2, summary=summary))


async def _payload(factory: Factory, run_id: UUID) -> Any:
    async with factory() as session:
        return await session.scalar(
            select(ContextPayload.summary).where(ContextPayload.run_id == run_id)
        )


@pytest.mark.parametrize(
    ("summary", "kept"),
    [
        pytest.param(_STOP, _STOP, id="summary-only-stop"),
        pytest.param(_BUILDER, _BUILDER, id="builder-payload-with-the-same-status"),
        pytest.param(_REVIEW, None, id="reviewed"),
        pytest.param({"files": 3}, None, id="no-status"),
        pytest.param({"status": None, "files": 3}, None, id="null-status"),
    ],
)
def test_only_a_summary_only_status_keeps_its_context_payload(
    portal_env: Env, summary: dict[str, Any], kept: dict[str, Any] | None
) -> None:
    async def body(factory: Factory) -> None:
        await _store_payload(factory, RUN_DONE, summary)
        await _finish(factory, RUN_DONE, "succeeded")

        assert await _task(factory).run(30) is False

        assert await _payload(factory, RUN_DONE) == kept

    _on_database(portal_env, body)


# One published medium finding comes with the seed of RUN_DONE.
_MEDIUM_ONLY = {"critical": 0, "high": 0, "medium": 1, "low": 0, "info": 0}
_SAVED_SUMMARY = {
    "problem": "One finding stays open.",
    "done_well": "The rest reads well.",
    "effort": "small",
}
_SAVED_RESULT = {
    "verdict": "attention",
    "severity_counts": _MEDIUM_ONLY,
    "summary": _SAVED_SUMMARY,
    "coverage": {
        "version": 1,
        "status": "complete",
        "head_sha": HEAD,
        "snapshot_digest": "0" * 64,
        "diff_digest": "0" * 64,
        "total_files": 2,
        "reviewed_files": 2,
        "file_list_complete": True,
        "reason_counts": {},
        "exclusion_counts": {},
        "reasons": [],
        "exclusions": [],
    },
}


async def _store_saved_result(factory: Factory, run_id: UUID) -> None:
    async with factory.begin() as session:
        session.add(
            RunAction(
                run_id=run_id,
                index=0,
                tool="review.postprocess",
                request={},
                response=_SAVED_RESULT,
                started_at=_OLD,
                duration_ms=0,
            )
        )


def _run_answers(client: TestClient) -> dict[str, Any]:
    answers = {
        "list": client.get("/api/runs"),
        "detail": client.get(f"/api/runs/{RUN_DONE}"),
        "pulls": client.get(f"/api/repos/{REPO_A}/pulls", params={"state": "open"}),
    }
    assert {name: answer.status_code for name, answer in answers.items()} == {
        "list": 200,
        "detail": 200,
        "pulls": 200,
    }
    return {name: answer.json() for name, answer in answers.items()}


@pytest.mark.parametrize(
    ("marked", "summary", "saved", "summary_only", "verdict"),
    [
        pytest.param(False, _REVIEW, True, False, "attention", id="saved-result"),
        pytest.param(False, _REVIEW, False, False, "attention", id="no-saved-result"),
        pytest.param(True, _REVIEW, False, True, None, id="marked-row"),
        pytest.param(False, _STOP, False, True, None, id="stage-stop"),
        pytest.param(False, _BUILDER, False, False, "attention", id="builder-payload"),
        pytest.param(True, _BUILDER, False, True, None, id="marked-row-with-a-reason"),
    ],
)
def test_the_run_answers_do_not_change_after_the_cleanup(
    portal_env: Env,
    marked: bool,
    summary: dict[str, Any],
    saved: bool,
    summary_only: bool,
    verdict: str | None,
) -> None:
    reports: list[Any] = []

    async def prepare(factory: Factory) -> None:
        await _store_run_data(factory, RUN_DONE, PR_OPEN, marked=marked)
        await _store_payload(factory, RUN_DONE, summary)
        if saved:
            await _store_saved_result(factory, RUN_DONE)
        await _finish(factory, RUN_DONE, "succeeded")

    async def report(factory: Factory) -> None:
        async with factory() as session:
            reports.append(await SqlAlchemyRunLifecycleStore(session).check_run_report(RUN_DONE))

    async def purge(factory: Factory) -> None:
        assert await _task(factory).run(30) is False
        assert await _marker(factory, RUN_DONE) == _NOW
        assert await _diff_rows(factory, RUN_DONE) != []

    _on_database(portal_env, prepare)
    with api(portal_env, AuthScope(42, (WS_A,))) as (client, _factory):
        before = _run_answers(client)
        _on_database(portal_env, report)
        _on_database(portal_env, purge)
        after = _run_answers(client)
        _on_database(portal_env, report)

    assert after == before
    assert reports[1] == reports[0]
    assert after["detail"]["summaryOnly"] is summary_only
    assert after["detail"]["verdict"] == verdict
    assert after["detail"]["severityCounts"] == _MEDIUM_ONLY
    if saved:
        assert after["detail"]["summary"]["problem"] == "One finding stays open."
    assert reports[1].summary_only is summary_only
    listed = next(item for item in after["list"]["items"] if item["id"] == str(RUN_DONE))
    assert listed["summaryOnly"] is summary_only
    pull = next(item for item in after["pulls"]["items"] if item["number"] == 1)
    assert pull["latestRun"]["verdict"] == verdict


def test_the_diff_of_a_purged_run_answers_with_its_files_without_text(portal_env: Env) -> None:
    async def prepare(factory: Factory) -> None:
        await _store_run_data(factory, RUN_DONE, PR_OPEN)
        await _finish(factory, RUN_DONE, "succeeded")

    async def purge(factory: Factory) -> None:
        assert await _task(factory).run(30) is False

    _on_database(portal_env, prepare)
    _on_database(portal_env, purge)
    with api(portal_env, AuthScope(42, (WS_A,))) as (client, _factory):
        answer = client.get(f"/api/runs/{RUN_DONE}/diff")

    assert answer.status_code == 200
    assert answer.json() == [
        {"filename": "src/a.py", "patch": None},
        {"filename": "vendor/big.js", "patch": None},
    ]


_SOURCE = "print('client code')\n"


async def _store_blob(factory: Factory, blob_sha: str, expires_at: datetime) -> None:
    async with factory.begin() as session:
        session.add(
            CachedFileBlob(
                repository_id=REPO_A, blob_sha=blob_sha, content=_SOURCE, expires_at=expires_at
            )
        )


async def _blob(factory: Factory, blob_sha: str) -> tuple[str, datetime] | None:
    async with factory() as session:
        row = (
            await session.execute(
                select(CachedFileBlob.content, CachedFileBlob.expires_at).where(
                    CachedFileBlob.blob_sha == blob_sha
                )
            )
        ).one_or_none()
        return None if row is None else (row[0], row[1])


def test_an_expired_blob_loses_its_content_and_keeps_its_row(portal_env: Env) -> None:
    expired = _NOW - timedelta(seconds=1)
    at_expiry = _NOW
    live = _NOW + timedelta(seconds=1)

    async def body(factory: Factory) -> None:
        await _store_blob(factory, "1" * 40, expired)
        await _store_blob(factory, "2" * 40, at_expiry)
        await _store_blob(factory, "3" * 40, live)

        assert await _task(factory).run(30) is False

        assert await _blob(factory, "1" * 40) == ("", expired)
        assert await _blob(factory, "2" * 40) == ("", at_expiry)
        assert await _blob(factory, "3" * 40) == (_SOURCE, live)

    _on_database(portal_env, body)


def test_the_blob_sweep_reads_only_the_index_of_blobs_with_text(portal_env: Env) -> None:
    # Cleared rows stay forever, so a pass must not walk them: the planner has to reach the
    # sweep through the partial index even when a sequential scan is off the table.
    sweeps: list[tuple[str, Any]] = []

    def capture(
        _connection: Any, _cursor: Any, statement: str, parameters: Any, *_rest: Any
    ) -> None:
        if statement.startswith("SELECT cached_file_blobs.id"):
            sweeps.append((statement, parameters))

    async def run() -> None:
        engine = portal_env.engine()
        try:
            factory = async_sessionmaker(engine, expire_on_commit=False)
            await _store_blob(factory, "1" * 40, _NOW - timedelta(seconds=1))
            event.listen(engine.sync_engine, "before_cursor_execute", capture)
            try:
                assert await _task(factory).run(30) is False
            finally:
                event.remove(engine.sync_engine, "before_cursor_execute", capture)
            assert len(sweeps) == 1
            statement, parameters = sweeps[0]
            async with engine.connect() as connection:
                await connection.exec_driver_sql("SET enable_seqscan = off")
                explained = await connection.exec_driver_sql(
                    "EXPLAIN (FORMAT JSON) " + statement, parameters
                )
                nodes = [explained.scalar_one()[0]["Plan"]]
        finally:
            await engine.dispose()
        for node in nodes:
            nodes.extend(node.get("Plans", []))
        assert [node["Index Name"] for node in nodes if "Index Name" in node] == [
            "ix_cached_file_blobs_retention_candidates"
        ], nodes

    asyncio.run(run())


def test_a_cleared_blob_comes_back_when_a_later_run_caches_it(portal_env: Env) -> None:
    key = BlobCacheKey(repository_id=REPO_A, blob_sha="1" * 40)
    later = _NOW + timedelta(days=1)

    async def body(factory: Factory) -> None:
        await _store_blob(factory, key.blob_sha, _NOW - timedelta(seconds=1))
        assert await _task(factory).run(30) is False

        async with factory.begin() as session:
            await SqlAlchemyBlobCache(session, now=lambda: later).put(key, _SOURCE)

        entry = await SqlAlchemyBlobCache(factory, now=lambda: later).get(key)
        assert entry.status is BlobCacheStatus.HIT
        assert entry.content == _SOURCE

    _on_database(portal_env, body)


def test_the_file_lines_of_a_cleared_blob_still_answer_as_expired(portal_env: Env) -> None:
    async def prepare(factory: Factory) -> None:
        await _store_run_data(factory, RUN_DONE, PR_OPEN)
        await _finish(factory, RUN_DONE, "succeeded")
        await _store_blob(factory, "a" * 40, _NOW - timedelta(days=30))

    async def purge(factory: Factory) -> None:
        assert await _task(factory).run(30) is False
        assert await _blob(factory, "a" * 40) == ("", _NOW - timedelta(days=30))

    _on_database(portal_env, prepare)
    with api(portal_env, AuthScope(42, (WS_A,))) as (client, _factory):
        before = client.get(f"/api/runs/{RUN_DONE}/files", params={"path": "src/a.py"})
        _on_database(portal_env, purge)
        after = client.get(f"/api/runs/{RUN_DONE}/files", params={"path": "src/a.py"})

    assert before.status_code == 410
    assert after.status_code == 410
    assert after.json() == before.json()
