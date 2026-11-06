"""The public badge query against a migrated PostgreSQL (#454)."""

from __future__ import annotations

import asyncio
from typing import cast
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.modules.auth.application.scope import AuthScope
from app.modules.reviews.application.repository_badge import BadgeRun
from app.modules.reviews.infrastructure.repository_badge_queries import (
    SqlAlchemyRepositoryBadgeQueries,
)
from tests.portal_postgres import (
    HEAD,
    INSTALL_A,
    PR_OPEN,
    REPO_A,
    REPO_B,
    RUN_CLOSED,
    RUN_DONE,
    Env,
    api,
    clean_review_result,
)

pytest_plugins = ("tests.portal_postgres",)
pytestmark = pytest.mark.integration

PUBLIC_A = (
    f"UPDATE repositories SET private = false, private_observed_at = now() WHERE id = '{REPO_A}'"
)
DONE_FINISHED_LAST = (
    f"UPDATE runs SET finished_at = '2026-11-02T00:00:00Z' WHERE id = '{RUN_DONE}'",
    f"UPDATE runs SET finished_at = '2026-11-01T00:00:00Z' WHERE id = '{RUN_CLOSED}'",
)
CLOSED_FINISHED_LAST = (
    f"UPDATE runs SET finished_at = '2026-11-01T00:00:00Z' WHERE id = '{RUN_DONE}'",
    f"UPDATE runs SET finished_at = '2026-11-02T00:00:00Z' WHERE id = '{RUN_CLOSED}'",
)
# RUN_DONE carries the one published medium finding of the seed; RUN_CLOSED has none.
PUBLIC_A_WITH_MEDIUM = (PUBLIC_A, *DONE_FINISHED_LAST)


def _query(env: Env, name: str, *statements: str) -> BadgeRun | None:
    async def exercise() -> BadgeRun | None:
        engine = env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with factory.begin() as session:
                for statement in statements:
                    await session.execute(text(statement))
            return await SqlAlchemyRepositoryBadgeQueries(factory).latest_succeeded_run(name)
        finally:
            await engine.dispose()

    return asyncio.run(exercise())


@pytest.mark.parametrize(
    ("statements", "visible"),
    [
        pytest.param((), False, id="never-told"),
        pytest.param((f"UPDATE repositories SET private = true WHERE id = '{REPO_A}'",), False),
        pytest.param((PUBLIC_A,), True, id="public"),
    ],
)
def test_only_the_stored_public_flag_opens_a_repository(
    portal_env: Env, statements: tuple[str, ...], visible: bool
) -> None:
    run = _query(portal_env, "octo/a", *statements)

    assert (run is not None) is visible


def test_the_name_matches_in_any_case(portal_env: Env) -> None:
    assert _query(portal_env, "OCTO/A", PUBLIC_A) is not None


@pytest.mark.parametrize(
    "statement",
    [
        pytest.param(
            f"UPDATE provider_installations SET deleted_at = now() WHERE id = '{INSTALL_A}'",
            id="installation-deleted",
        ),
        pytest.param(
            f"UPDATE repositories SET installation_available = false WHERE id = '{REPO_A}'",
            id="access-removed",
        ),
        pytest.param(
            f"UPDATE provider_installations SET provider = 'gitlab' WHERE id = '{INSTALL_A}'",
            id="another-provider",
        ),
    ],
)
def test_a_repository_the_github_app_cannot_see_has_no_badge(
    portal_env: Env, statement: str
) -> None:
    assert _query(portal_env, "octo/a", PUBLIC_A, statement) is None


def test_a_paused_repository_keeps_its_latest_verdict(portal_env: Env) -> None:
    paused = f"UPDATE repositories SET enabled = false WHERE id = '{REPO_A}'"

    assert _query(portal_env, "octo/a", PUBLIC_A, paused) is not None


@pytest.mark.parametrize(
    ("statements", "expected"),
    [
        pytest.param(
            (
                "UPDATE repositories SET full_name = 'octo/a', private = false, "
                f"private_observed_at = now() WHERE id = '{REPO_B}'",
            ),
            False,
            id="two-public-rows",
        ),
        pytest.param(
            (f"UPDATE repositories SET full_name = 'OCTO/a' WHERE id = '{REPO_B}'",),
            False,
            id="another-row-in-another-case-unknown-flag",
        ),
        pytest.param(
            (
                f"UPDATE repositories SET full_name = 'octo/a', private = false, "
                f"installation_available = false WHERE id = '{REPO_B}'",
            ),
            True,
            id="a-row-without-access-does-not-count",
        ),
    ],
)
def test_several_active_rows_with_one_name_are_no_data(
    portal_env: Env, statements: tuple[str, ...], expected: bool
) -> None:
    run = _query(portal_env, "octo/a", PUBLIC_A, *statements)

    assert (run is not None) is expected


@pytest.mark.parametrize(
    ("statements", "severities"),
    [
        pytest.param(DONE_FINISHED_LAST, ("medium",), id="the-one-with-the-finding-last"),
        pytest.param(CLOSED_FINISHED_LAST, (), id="the-clean-one-last"),
        pytest.param(
            (f"UPDATE runs SET finished_at = '2026-11-01T00:00:00Z' WHERE id = '{RUN_DONE}'",),
            ("medium",),
            id="never-finished-sorts-after-finished",
        ),
        pytest.param(
            (
                *CLOSED_FINISHED_LAST,
                f"UPDATE runs SET state = 'failed' WHERE id = '{RUN_CLOSED}'",
            ),
            ("medium",),
            id="a-newer-failure-does-not-count",
        ),
    ],
)
def test_the_latest_run_is_the_last_finished_success(
    portal_env: Env, statements: tuple[str, ...], severities: tuple[str, ...]
) -> None:
    run = _query(portal_env, "octo/a", PUBLIC_A, *statements)

    assert run is not None
    assert run.published_severities == severities


def test_no_succeeded_run_is_no_run(portal_env: Env) -> None:
    failed = f"UPDATE runs SET state = 'failed' WHERE id IN ('{RUN_DONE}', '{RUN_CLOSED}')"

    assert _query(portal_env, "octo/a", PUBLIC_A, failed) is None


def test_only_published_findings_that_were_not_dropped_count(portal_env: Env) -> None:
    def finding(published: str, drop_reason: str) -> str:
        return (
            "INSERT INTO findings (id, run_id, file_path, line_start, severity, confidence, "
            "category, title, body, published, inline_comment, drop_reason) VALUES "
            f"('{uuid4()}', '{RUN_DONE}', 'a.py', 3, 'critical', 0.8, 'security', 'T', 'B', "
            f"{published}, false, {drop_reason})"
        )

    run = _query(
        portal_env,
        "octo/a",
        *PUBLIC_A_WITH_MEDIUM,
        finding("false", "NULL"),
        finding("true", "'duplicate'"),
    )

    assert run is not None
    assert run.published_severities == ("medium",)


def test_a_summary_only_run_is_marked(portal_env: Env) -> None:
    marked = (
        "INSERT INTO code_change_diffs (id, run_id, code_change_id, head_sha, filename, patch, "
        f"summary_only) VALUES ('{uuid4()}', '{RUN_DONE}', '{PR_OPEN}', '{HEAD}', 'a.py', "
        "'@@ -1 +1 @@', true)"
    )

    run = _query(portal_env, "octo/a", *PUBLIC_A_WITH_MEDIUM, marked)

    assert run is not None
    assert run.summary_only is True


def test_the_migration_indexes_the_name_in_lower_case(portal_env: Env) -> None:
    async def read() -> str | None:
        engine = portal_env.engine()
        try:
            async with engine.connect() as connection:
                return cast(
                    str | None,
                    await connection.scalar(
                        text(
                            "SELECT indexdef FROM pg_indexes WHERE schemaname = current_schema() "
                            "AND indexname = 'ix_repositories_lower_full_name'"
                        )
                    ),
                )
        finally:
            await engine.dispose()

    definition = asyncio.run(read())

    assert definition is not None
    assert "lower((full_name)::text)" in definition


def test_the_route_serves_a_real_result_and_hides_a_private_repository(portal_env: Env) -> None:
    saved = (
        "INSERT INTO run_actions (id, run_id, index, tool, request, response, started_at, "
        f"duration_ms) VALUES ('{uuid4()}', '{RUN_CLOSED}', 0, 'review.postprocess', "
        f"'{{}}'::jsonb, CAST('{clean_review_result(HEAD)}' AS jsonb), now(), 1)"
    )
    assert _query(portal_env, "octo/a", PUBLIC_A, *CLOSED_FINISHED_LAST, saved) is not None

    with api(portal_env, AuthScope(42, ())) as (client, factory):
        passed = client.get("/api/badges/github/octo/a.svg")
        unknown = client.get("/api/badges/github/octo/unknown.svg")

        async def make_private() -> None:
            async with factory.begin() as session:
                await session.execute(
                    text(f"UPDATE repositories SET private = true WHERE id = '{REPO_A}'")
                )

        assert client.portal is not None
        client.portal.call(make_private)
        private = client.get("/api/badges/github/octo/a.svg")

    assert ">passed</text>" in passed.text
    assert ">no data</text>" in unknown.text
    assert (private.status_code, private.headers, private.content) == (
        unknown.status_code,
        unknown.headers,
        unknown.content,
    )
