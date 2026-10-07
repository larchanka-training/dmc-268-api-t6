"""Opt-in PostgreSQL proof for hashed refresh sessions and profile persistence."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import CreateSchema, DropSchema

from alembic import command
from app.modules.auth.application.exchange_github_code import AuthenticatedUser
from app.modules.auth.infrastructure.models import AuthRefreshSession, GitHubUserProfile
from app.modules.auth.infrastructure.sessions import SqlAlchemyAuthSessionUnitOfWork


@pytest.fixture
def auth_schema() -> Iterator[tuple[str, str]]:
    database_url = os.environ.get("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("set TEST_DATABASE_URL to run PostgreSQL integration tests")
    schema = f"test_auth_session_{uuid4().hex}"
    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            connection.execute(CreateSchema(schema))
            connection.execute(text(f'SET search_path TO "{schema}"'))
            connection.commit()
            config = Config("alembic.ini")
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
            yield database_url, schema
            connection.rollback()
            connection.execute(text("SET search_path TO public"))
            connection.execute(DropSchema(schema, cascade=True))
            connection.commit()
    finally:
        engine.dispose()


@pytest.mark.integration
def test_auth_session_uow_persists_profile_and_hash_only(
    auth_schema: tuple[str, str],
) -> None:
    database_url, schema = auth_schema
    engine = create_async_engine(
        database_url,
        connect_args={"options": f"-csearch_path={schema}"},
        poolclass=NullPool,
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    expires_at = datetime.now(UTC) + timedelta(days=30)

    async def exercise() -> None:
        async with SqlAlchemyAuthSessionUnitOfWork(factory) as uow:
            await uow.sessions.create(
                AuthenticatedUser(42, "octocat", "Octo Cat", "https://github.test/avatar.png"),
                "a" * 64,
                uuid4(),
                expires_at,
            )
            await uow.commit()
        async with factory() as session:
            profile = await session.scalar(
                select(GitHubUserProfile).where(GitHubUserProfile.id == 42)
            )
            refresh = await session.scalar(select(AuthRefreshSession))
            assert profile is not None
            assert (profile.login, profile.name, profile.avatar_url) == (
                "octocat",
                "Octo Cat",
                "https://github.test/avatar.png",
            )
            assert refresh is not None
            assert refresh.github_user_id == 42
            assert refresh.token_hash == "a" * 64
            assert refresh.expires_at == expires_at
            assert "ghu_" not in str(profile.__dict__) + str(refresh.__dict__)

    try:
        asyncio.run(exercise())
    finally:
        asyncio.run(engine.dispose())


@pytest.mark.integration
def test_repeated_http_callback_expands_scope_and_repository_access_without_logout(
    auth_schema: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json

    import httpx
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives.serialization import (
        Encoding,
        NoEncryption,
        PrivateFormat,
        PublicFormat,
    )
    from fastapi.testclient import TestClient

    from app.bootstrap.reviews_api import GitHubAuthHttpClients, ReviewsApiResources
    from app.main import app
    from app.modules.auth.infrastructure.jwt_tokens import Rs256AccessTokenVerifier
    from app.modules.repositories.infrastructure.models import ProviderInstallation, Repository
    from app.modules.workspaces.infrastructure.models import GitHubUserRepositoryAccess

    database_url, schema = auth_schema
    engine = create_async_engine(
        database_url, connect_args={"options": f"-csearch_path={schema}"}, poolclass=NullPool
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private = key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()).decode()
    public = key.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo).decode()
    for name, value in {
        "GITHUB_CLIENT_ID": "test-client",
        "GITHUB_CLIENT_SECRET": "test-secret",
        "AUTH_JWT_PRIVATE_KEY": private,
        "AUTH_JWT_PUBLIC_KEY": public,
        "AUTH_JWT_ISSUER": "test-issuer",
        "AUTH_JWT_AUDIENCE": "test-audience",
    }.items():
        monkeypatch.setenv(name, value)

    def github(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/login/oauth/access_token":
            code = json.loads(request.content)["code"]
            assert code in {"first-code", "sync-access-code"}
            return httpx.Response(200, json={"access_token": code})
        token = request.headers["Authorization"]
        second = token == "Bearer sync-access-code"
        assert token in {"Bearer first-code", "Bearer sync-access-code"}
        if request.url.path == "/user":
            return httpx.Response(200, json={"id": 42, "login": "octocat"})
        if request.url.path == "/user/installations":
            installations = [{"id": 17, "account": {"login": "alpha"}}]
            if second:
                installations.append({"id": 18, "account": {"login": "beta"}})
            return httpx.Response(
                200,
                json={
                    "total_count": len(installations),
                    "installations": installations,
                },
            )
        if request.url.path == "/user/installations/17/repositories":
            ids = [101, 102] if second else [101]
        elif request.url.path == "/user/installations/18/repositories" and second:
            ids = [201]
        else:
            raise AssertionError(f"unexpected GitHub request: {request.url.path}")
        return httpx.Response(
            200,
            json={
                "total_count": len(ids),
                "repositories": [{"id": item} for item in ids],
            },
        )

    oauth = httpx.AsyncClient(base_url="https://github.test", transport=httpx.MockTransport(github))
    api = httpx.AsyncClient(
        base_url="https://api.github.test", transport=httpx.MockTransport(github)
    )
    app.state.reviews_api_resources = ReviewsApiResources(engine, factory)
    app.state.github_auth_http_clients = GitHubAuthHttpClients(oauth, api)

    async def onboard(installation_external_id: int, repo_id: int, name: str) -> None:
        async with factory.begin() as session:
            installation = await session.scalar(
                select(ProviderInstallation).where(
                    ProviderInstallation.external_id == installation_external_id
                )
            )
            assert installation is not None
            session.add(
                Repository(
                    provider_installation_id=installation.id,
                    external_id=repo_id,
                    full_name=name,
                    default_branch="main",
                    web_url=f"https://github.test/{name}",
                )
            )

    async def verify_persisted_grants() -> None:
        async with factory() as session:
            grants = (
                await session.scalars(
                    select(GitHubUserRepositoryAccess.repository_external_id)
                    .where(GitHubUserRepositoryAccess.github_user_id == 42)
                    .order_by(GitHubUserRepositoryAccess.repository_external_id)
                )
            ).all()
            assert grants == [101, 102, 201]
            sessions = (await session.scalars(select(AuthRefreshSession))).all()
            assert len(sessions) == 2
            assert len({item.family_id for item in sessions}) == 2
            assert all(item.revoked_at is None for item in sessions)

    async def close() -> None:
        await oauth.aclose()
        await api.aclose()
        await engine.dispose()

    try:
        client = TestClient(app, base_url="https://testserver")
        first = client.post("/api/auth/github/callback", json={"code": "first-code"})
        assert first.status_code == 200
        first_cookie = client.cookies.get("refresh_token")
        assert first_cookie is not None
        verifier = Rs256AccessTokenVerifier(public, issuer="test-issuer", audience="test-audience")
        first_scope = verifier.verify_token(first.json()["accessToken"])
        assert len(first_scope.workspace_ids) == 1
        asyncio.run(onboard(17, 101, "alpha/original"))
        asyncio.run(onboard(17, 102, "alpha/new"))
        original_headers = {"Authorization": f"Bearer {first.json()['accessToken']}"}
        listed = client.get("/api/repos", headers=original_headers)
        assert listed.status_code == 200
        assert [item["fullName"] for item in listed.json()] == ["alpha/original"]

        # Keep the same client and live cookie: no logout or clearing browser state.
        second = client.post("/api/auth/github/callback", json={"code": "sync-access-code"})
        assert second.status_code == 200
        assert client.cookies.get("refresh_token") != first_cookie
        second_scope = verifier.verify_token(second.json()["accessToken"])
        assert len(second_scope.workspace_ids) == 2
        assert set(first_scope.workspace_ids) < set(second_scope.workspace_ids)
        asyncio.run(onboard(18, 201, "beta/new-installation"))
        listed = client.get(
            "/api/repos", headers={"Authorization": f"Bearer {second.json()['accessToken']}"}
        )
        assert listed.status_code == 200
        assert {item["fullName"] for item in listed.json()} == {
            "alpha/original",
            "alpha/new",
            "beta/new-installation",
        }
        asyncio.run(verify_persisted_grants())
    finally:
        del app.state.github_auth_http_clients
        del app.state.reviews_api_resources
        asyncio.run(close())
