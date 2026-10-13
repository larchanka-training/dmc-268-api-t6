"""Source retirement stops the next actual VCS request, including cached Run inputs."""

from __future__ import annotations

import asyncio
import base64
import os
from collections.abc import Iterator
from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.modules.integrations.webhooks.infrastructure.github_installation_tree_provider import (
    GitHubAppInstallationAccessTokenProvider,
)
from app.modules.reviews.application.run_failures import RunCancelled
from app.modules.reviews.infrastructure.github_run_source import GitHubRunSource
from app.modules.reviews.infrastructure.github_vcs import HttpGitHubVcsProvider
from app.modules.reviews.infrastructure.run_repository import SqlAlchemyRunRepository
from tests.portal_postgres import INSTALL_A, REPO_A, RUN_ATTEMPTED, Env, portal_schema
from tests.test_app_reinstall_postgres import FreshGitHub, apply
from tests.test_retired_installation_postgres import DiscardTokenCache
from tests.test_run_origin_guard_postgres import prepare
from tests.test_run_publication_origin_postgres import retire

BLOB = "c" * 40


@pytest.fixture
def vcs_env() -> Iterator[Env]:
    database_url = os.environ.get("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("set TEST_DATABASE_URL to run PostgreSQL integration tests")
    with portal_schema(database_url, None) as env:
        yield env


@pytest.mark.integration
@pytest.mark.parametrize("operation", ["tree", "blob", "agents", "blob_loop"])
@pytest.mark.parametrize("retired", [False, True])
def test_retirement_during_vcs_token_stops_tree_or_blob_request(
    vcs_env: Env, operation: str, retired: bool
) -> None:
    async def exercise() -> None:
        engine = vcs_env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        calls: list[str] = []
        testing = False

        async def http(request: httpx.Request) -> httpx.Response:
            calls.append(request.method + " " + request.url.path)
            async with factory.begin() as session:
                await session.execute(
                    text("SELECT id FROM provider_installations WHERE id=:id FOR UPDATE NOWAIT"),
                    {"id": INSTALL_A},
                )
                await session.execute(
                    text("SELECT id FROM runs WHERE id=:id FOR UPDATE NOWAIT"),
                    {"id": RUN_ATTEMPTED},
                )
            if request.url.path == "/app/installations/17/access_tokens":
                if retired and testing and operation in {"tree", "blob"}:
                    await retire(factory)
                return httpx.Response(
                    201, json={"token": "token", "expires_at": "2030-01-01T00:00:00Z"}
                )
            if "/git/trees/" in request.url.path:
                if retired and testing and operation == "agents":
                    await retire(factory)
                return httpx.Response(
                    200,
                    json={
                        "tree": [
                            {
                                "type": "blob",
                                "path": "AGENTS.md" if operation == "agents" else "a.py",
                                "sha": BLOB,
                                "size": 3,
                            }
                        ]
                        + (
                            [{"type": "blob", "path": "b.py", "sha": "d" * 40, "size": 3}]
                            if operation == "blob_loop"
                            else []
                        )
                    },
                )
            if "/git/blobs/" in request.url.path:
                if retired and testing and operation == "blob_loop":
                    await retire(factory)
                return httpx.Response(
                    200,
                    json={
                        "sha": request.url.path.rsplit("/", 1)[1],
                        "encoding": "base64",
                        "content": base64.b64encode(b"ok\n").decode(),
                        "size": 3,
                    },
                )
            raise AssertionError(f"unexpected {request.url}")

        try:
            await prepare(factory, "active", "running")
            async with httpx.AsyncClient(
                base_url="https://api.github.test", transport=httpx.MockTransport(http)
            ) as client:
                tokens = GitHubAppInstallationAccessTokenProvider(
                    client=client,
                    app_id="42",
                    private_key="unused",
                    jwt_encoder=lambda claims, key: "jwt",
                    cache=DiscardTokenCache(),
                    now=lambda: datetime(2026, 10, 10, tzinfo=UTC).timestamp(),
                )
                source = GitHubRunSource(
                    client=client,
                    token_provider=tokens,
                    vcs=HttpGitHubVcsProvider(client=client, token_provider=tokens),
                    runs=SqlAlchemyRunRepository(factory, allow_unscoped=True),
                    run_id=RUN_ATTEMPTED,
                )
                if operation in {"blob", "blob_loop"}:
                    assert len(await source.fetch_tree(REPO_A)) == (
                        2 if operation == "blob_loop" else 1
                    )
                    calls.clear()
                testing = True

                async def execute() -> None:
                    if operation == "tree":
                        assert len(await source.fetch_tree(REPO_A)) == 1
                    elif operation == "agents":
                        assert (await source.fetch_agents_md(REPO_A)).content == "ok\n"
                    else:
                        paths = ("a.py", "b.py") if operation == "blob_loop" else ("a.py",)
                        assert (await source.fetch_files(REPO_A, paths))[0].content == "ok\n"

                if retired:
                    with pytest.raises(RunCancelled, match="installation_unavailable"):
                        await execute()
                else:
                    await execute()
            assert calls[0] == "POST /app/installations/17/access_tokens"
            assert len(calls) == (
                {"tree": 1, "blob": 1, "agents": 2, "blob_loop": 2}[operation]
                if retired
                else (4 if operation in {"agents", "blob_loop"} else 2)
            )
        finally:
            await engine.dispose()

    asyncio.run(exercise())


@pytest.mark.integration
@pytest.mark.parametrize(
    "stage",
    [
        "pr_token",
        "diff_token",
        "page",
        "last_page",
        "refetch_token",
        "classification",
        "cache_blob",
    ],
)
@pytest.mark.parametrize("retired", [False, True])
def test_processor_stops_each_subsequent_page_refetch_and_blob_without_swallowing_cancellation(
    vcs_env: Env, stage: str, retired: bool
) -> None:
    from app.modules.reviews.application.process_run import ReviewRunProcessor
    from app.modules.reviews.infrastructure.run_processing_unit_of_work import (
        SqlAlchemyRunProcessingUnitOfWork,
    )
    from app.modules.reviews.infrastructure.vcs_failures import ClassifiedVcsProvider
    from tests.portal_postgres import HEAD
    from tests.test_github_vcs_provider import _file, _pull_payload

    async def exercise() -> None:
        engine = vcs_env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        calls: list[str] = []
        token_calls = 0
        blob_calls = 0
        count = 2 if stage in {"classification", "cache_blob"} else 101

        async def change(phase: str) -> None:
            if retired and stage == phase:
                await retire(factory)

        async def http(request: httpx.Request) -> httpx.Response:
            nonlocal token_calls, blob_calls
            path = request.url.path
            calls.append(
                path
                + ("?page=" + request.url.params["page"] if "page" in request.url.params else "")
            )
            async with factory.begin() as session:
                await session.execute(
                    text("SELECT id FROM provider_installations WHERE id=:id FOR UPDATE NOWAIT"),
                    {"id": INSTALL_A},
                )
                await session.execute(
                    text("SELECT id FROM runs WHERE id=:id FOR UPDATE NOWAIT"),
                    {"id": RUN_ATTEMPTED},
                )
            if path == "/app/installations/17/access_tokens":
                token_calls += 1
                await change(
                    {1: "pr_token", 2: "diff_token", 3: "refetch_token"}.get(token_calls, "")
                )
                return httpx.Response(
                    201, json={"token": "token", "expires_at": "2030-01-01T00:00:00Z"}
                )
            if path.endswith("/pulls/3"):
                payload = _pull_payload(changed_files=count, head_sha=HEAD)
                payload["number"] = 3
                return httpx.Response(200, json=payload)
            if path.endswith("/pulls/3/files"):
                page = int(request.url.params["page"])
                await change("page" if page == 1 else "last_page")
                batch = []
                for i in range((page - 1) * 100, min(page * 100, count)):
                    item = _file(i)
                    item["sha"] = f"{i + 1:040x}"
                    if count == 101 and 0 < i < 100:
                        item["filename"] = f"dist/generated{i}.js"
                    if stage == "classification":
                        item["patch"] = None
                    batch.append(item)
                return httpx.Response(200, json=batch)
            if "/git/blobs/" in path:
                blob_calls += 1
                if blob_calls == 1:
                    await change("classification")
                    await change("cache_blob")
                sha = path.rsplit("/", 1)[1]
                return httpx.Response(
                    200, json={"sha": sha, "encoding": "base64", "content": "b2sK", "size": 3}
                )
            raise AssertionError(f"unexpected {request.url}")

        try:
            await prepare(factory, "active", "running")
            repository = SqlAlchemyRunRepository(factory, allow_unscoped=True)
            async with httpx.AsyncClient(
                base_url="https://api.github.test", transport=httpx.MockTransport(http)
            ) as client:
                tokens = GitHubAppInstallationAccessTokenProvider(
                    client=client,
                    app_id="42",
                    private_key="unused",
                    jwt_encoder=lambda claims, key: "jwt",
                    cache=DiscardTokenCache(),
                    now=lambda: datetime(2026, 10, 10, tzinfo=UTC).timestamp(),
                )
                processor = ReviewRunProcessor(
                    repository,
                    None,
                    vcs_provider=ClassifiedVcsProvider(
                        HttpGitHubVcsProvider(client=client, token_provider=tokens)
                    ),
                    uow_factory=lambda: SqlAlchemyRunProcessingUnitOfWork(factory),
                )
                if retired:
                    with pytest.raises(RunCancelled, match="installation_unavailable"):
                        await processor.prepare(RUN_ATTEMPTED)
                else:
                    assert await processor.prepare(RUN_ATTEMPTED) is True
            prefix = "/repos/octo/a"
            token = "/app/installations/17/access_tokens"
            initial = [token, prefix + "/pulls/3", token, prefix + "/pulls/3/files?page=1"]
            diff = initial if count == 2 else initial + [prefix + "/pulls/3/files?page=2"]
            refetch = diff + [token, prefix + "/pulls/3"]
            if retired:
                expected = {
                    "pr_token": initial[:1],
                    "diff_token": initial[:3],
                    "page": initial,
                    "last_page": diff,
                    "refetch_token": diff + [token],
                    "classification": refetch + [token, prefix + "/git/blobs/" + f"{1:040x}"],
                    "cache_blob": refetch + [token, prefix + "/git/blobs/" + f"{1:040x}"],
                }[stage]
                assert calls == expected
            else:
                assert calls[: len(refetch)] == refetch
                assert blob_calls == (4 if stage == "classification" else 2)
                assert all("/29/" not in call for call in calls)
        finally:
            await engine.dispose()

    asyncio.run(exercise())


@pytest.mark.integration
@pytest.mark.parametrize("operation", ["agents", "files", "meta"])
@pytest.mark.parametrize("origin", ["active", "retired", "rebound", "unknown"])
def test_cached_run_source_still_checks_original_identity_before_next_request(
    vcs_env: Env, operation: str, origin: str
) -> None:
    from tests.portal_postgres import HEAD
    from tests.test_github_vcs_provider import _pull_payload

    async def exercise() -> None:
        engine = vcs_env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        calls: list[str] = []

        async def http(request: httpx.Request) -> httpx.Response:
            calls.append(request.url.path)
            if "/access_tokens" in request.url.path:
                return httpx.Response(
                    201, json={"token": "token", "expires_at": "2030-01-01T00:00:00Z"}
                )
            if "/git/trees/" in request.url.path:
                return httpx.Response(
                    200,
                    json={"tree": [{"type": "blob", "path": "AGENTS.md", "sha": BLOB, "size": 3}]},
                )
            if "/git/blobs/" in request.url.path:
                return httpx.Response(
                    200, json={"sha": BLOB, "encoding": "base64", "content": "b2sK", "size": 3}
                )
            payload = _pull_payload(changed_files=1, head_sha=HEAD)
            payload["number"] = 3
            return httpx.Response(200, json=payload)

        try:
            await prepare(factory, "active", "running")
            async with httpx.AsyncClient(
                base_url="https://api.github.test", transport=httpx.MockTransport(http)
            ) as client:
                tokens = GitHubAppInstallationAccessTokenProvider(
                    client=client,
                    app_id="42",
                    private_key="unused",
                    jwt_encoder=lambda claims, key: "jwt",
                    cache=DiscardTokenCache(),
                    now=lambda: datetime(2026, 10, 10, tzinfo=UTC).timestamp(),
                )
                source = GitHubRunSource(
                    client=client,
                    token_provider=tokens,
                    vcs=HttpGitHubVcsProvider(client=client, token_provider=tokens),
                    runs=SqlAlchemyRunRepository(factory, allow_unscoped=True),
                    run_id=RUN_ATTEMPTED,
                )
                assert len(await source.fetch_tree(REPO_A)) == 1
                baseline = list(calls)
                if origin == "rebound":
                    async with factory.begin() as session:
                        await session.execute(
                            text(
                                "UPDATE provider_installations SET account_external_id=700 "
                                "WHERE id=:id"
                            ),
                            {"id": INSTALL_A},
                        )
                    await retire(factory)
                    await apply(factory, FreshGitHub())
                    async with factory() as session:
                        assert (
                            await session.scalar(
                                text(
                                    "SELECT p.external_id FROM provider_installations p "
                                    "JOIN repositories r ON r.provider_installation_id=p.id "
                                    "WHERE r.id=:id"
                                ),
                                {"id": REPO_A},
                            )
                            == 29
                        )
                else:
                    await prepare(factory, origin, "running")

                async def execute() -> None:
                    if operation == "agents":
                        assert (await source.fetch_agents_md(REPO_A)).content == "ok\n"
                    elif operation == "files":
                        assert len(await source.fetch_files(REPO_A, ("AGENTS.md",))) == 1
                    else:
                        assert await source.get_pull_request_meta(RUN_ATTEMPTED) is not None

                if origin == "active":
                    await execute()
                    assert len(calls) > len(baseline)
                else:
                    with pytest.raises(RunCancelled, match="installation_unavailable"):
                        await execute()
                    assert calls == baseline
        finally:
            await engine.dispose()

    asyncio.run(exercise())
