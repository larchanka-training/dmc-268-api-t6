"""Signed durable deliveries and real per-user OAuth/JWT across GitHub reinstall."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bootstrap.auth_api import get_refresh_local_session
from app.bootstrap.reviews_api import GitHubAuthHttpClients, ReviewsApiResources
from app.main import app, get_github_webhook_secret
from app.modules.auth.application.refresh_session import RefreshLocalSession
from app.modules.auth.infrastructure.jwt_tokens import Rs256AccessTokenIssuer
from app.modules.integrations.webhooks.application.receive_github_delivery import (
    ReceiveGitHubDelivery,
)
from tests.portal_postgres import (
    HEAD,
    INSTALL_A,
    PR_OPEN,
    REPO_A,
    RULE_A,
    RUN_ATTEMPTED,
    RUN_DONE,
    WS_A,
    Env,
    portal_schema,
)


@pytest.fixture
def reinstall_env() -> Iterator[Env]:
    database_url = os.environ.get("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("set TEST_DATABASE_URL to run PostgreSQL integration tests")
    with portal_schema(database_url, None) as env:
        yield env


_SECRET = "issue113-signed-delivery"
_BLOB = "f" * 40
_RULE = [{"name": "Custom", "include": ["**/*.py"], "exclude": [], "checks": ["Preserve me"]}]


async def read(factory: async_sessionmaker[AsyncSession], sql: str, **values: Any) -> list[Any]:
    async with factory() as session:
        return list((await session.execute(text(sql), values)).all())


async def seed_custom(factory: async_sessionmaker[AsyncSession]) -> None:
    async with factory.begin() as session:
        for sql, values in (
            (
                "UPDATE repositories SET enabled=false, max_comments=7, wait_for_ci='always', "
                "review_event='request_changes', branch_filter='{main,release/*}', "
                "severity_threshold='critical', rag_enabled=true, "
                "prompt_version_id=(SELECT id FROM prompt_versions "
                "WHERE key='review.system') WHERE id=:id",
                {"id": REPO_A},
            ),
            ("UPDATE workspaces SET daily_budget_usd=12.34 WHERE id=:id", {"id": WS_A}),
            (
                "UPDATE rule_versions SET rules=CAST(:rules AS "
                "jsonb),checksum=:checksum WHERE id=:id",
                {"id": RULE_A, "rules": json.dumps(_RULE), "checksum": "f" * 64},
            ),
            (
                "UPDATE runs SET diff_snapshotted_at=clock_timestamp() WHERE id=:id",
                {"id": RUN_DONE},
            ),
            (
                "INSERT INTO "
                "repo_conventions(id,repository_id,prompt_version_id,agents_md,key_patterns,"
                "recommendations,languages) SELECT :id,:repo,prompt_version_id,'Custom agents',"
                '\'["Keep me"]\',\'["Keep this recommendation"]\',\'{"Python": '
                "12}' FROM runs WHERE id=:run",
                {"id": UUID(int=1139001), "repo": REPO_A, "run": RUN_DONE},
            ),
            (
                "INSERT INTO context_payloads(id,run_id,schema_version,summary) "
                "VALUES (:id,:run,1,'{\"preserve\": true}')",
                {"id": UUID(int=1139002), "run": RUN_DONE},
            ),
            (
                "INSERT INTO "
                "code_change_diffs(id,run_id,code_change_id,head_sha,filename,patch,blob_sha) "
                "VALUES (:id,:run,:pr,:head,'a.py','@@ -1 +1 @@\n-old\n+new',:blob)",
                {
                    "id": UUID(int=1139003),
                    "run": RUN_DONE,
                    "pr": PR_OPEN,
                    "head": HEAD,
                    "blob": _BLOB,
                },
            ),
            (
                'INSERT INTO run_actions(id,run_id,"index",tool,request,response,'
                "started_at,duration_ms) "
                "VALUES (:id,:run,0,'fetch_diff','{}','{\"preserve\": true}',clock_timestamp(),1)",
                {"id": UUID(int=1139005), "run": RUN_DONE},
            ),
            (
                "INSERT INTO cached_file_blobs(id,repository_id,blob_sha,content,expires_at) "
                "VALUES (:id,:repo,:blob,'first\nsecond',clock_timestamp()+interval '1 day')",
                {"id": UUID(int=1139004), "repo": REPO_A, "blob": _BLOB},
            ),
        ):
            await session.execute(text(sql), values)


async def preserved(factory: async_sessionmaker[AsyncSession]) -> dict[str, list[Any]]:
    result = {}
    for table, condition in (
        ("runs", "id=:run"),
        ("code_changes", "repository_id=:repo"),
        ("rule_versions", "repository_id=:repo"),
        ("repo_conventions", "repository_id=:repo"),
        ("code_change_diffs", "run_id=:run"),
        ("context_payloads", "run_id=:run"),
        ("run_actions", "run_id=:run"),
        ("findings", "run_id=:run"),
        ("cached_file_blobs", "repository_id=:repo"),
    ):
        result[table] = await read(
            factory,
            f"SELECT to_jsonb(r) FROM {table} r WHERE {condition} ORDER BY id",
            run=RUN_DONE,
            repo=REPO_A,
        )
    return result


def assert_denied(client: TestClient, headers: dict[str, str]) -> None:
    assert client.get("/api/repos", headers=headers).json() == []
    assert client.get("/api/runs", headers=headers).json()["items"] == []
    for path in (
        f"/api/repos/{REPO_A}",
        f"/api/runs/{RUN_DONE}",
        f"/api/runs/{RUN_DONE}/comments",
        f"/api/runs/{RUN_DONE}/actions",
        f"/api/runs/{RUN_DONE}/actions/0/response",
        f"/api/runs/{RUN_DONE}/diff",
        f"/api/runs/{RUN_DONE}/files?path=a.py",
    ):
        assert client.get(path, headers=headers).status_code == 404
    assert (
        client.patch(f"/api/repos/{REPO_A}", json={"enabled": True}, headers=headers).status_code
        == 404
    )
    assert client.post(f"/api/runs/{RUN_DONE}/rerun", headers=headers).status_code == 404
    assert client.post(f"/api/runs/{RUN_ATTEMPTED}/cancel", headers=headers).status_code == 404


@pytest.mark.integration
@pytest.mark.parametrize("account_id", [700, 701])
def test_signed_reinstall_requires_each_users_fresh_oauth_and_preserves_only_same_account(
    reinstall_env: Env, monkeypatch: pytest.MonkeyPatch, account_id: int
) -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_key = key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()).decode()
    for name, value in {
        "GITHUB_CLIENT_ID": "test-client",
        "GITHUB_CLIENT_SECRET": "test-secret",
        "AUTH_JWT_PRIVATE_KEY": private_key,
        "AUTH_JWT_PUBLIC_KEY": key.public_key()
        .public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)
        .decode(),
        "AUTH_JWT_ISSUER": "test-issuer",
        "AUTH_JWT_AUDIENCE": "test-audience",
    }.items():
        monkeypatch.setenv(name, value)
    for name in ("DATABASE_URL", "RABBITMQ_URL", "GITHUB_WEBHOOK_SECRET"):
        monkeypatch.delenv(name, raising=False)
    oauth_calls: list[str] = []
    provider_calls: list[str] = []

    def github(request: httpx.Request) -> httpx.Response:
        oauth_calls.append(request.url.path)
        if request.url.path == "/login/oauth/access_token":
            return httpx.Response(200, json={"access_token": json.loads(request.content)["code"]})
        token = request.headers["Authorization"].removeprefix("Bearer ")
        person, installation = token.split("-")
        user_id = 42 if person == "alice" else 43
        if request.url.path == "/user":
            return httpx.Response(200, json={"id": user_id, "login": person})
        if request.url.path == "/user/installations":
            return httpx.Response(
                200,
                json={
                    "total_count": 1,
                    "installations": [
                        {
                            "id": int(installation),
                            "account": {
                                "id": 700 if installation == "17" else account_id,
                                "login": "octo" if installation == "17" else "renamed-owner",
                            },
                        }
                    ],
                },
            )
        assert request.url.path == f"/user/installations/{installation}/repositories"
        return httpx.Response(200, json={"total_count": 1, "repositories": [{"id": 101}]})

    class Tokens:
        async def get_installation_access_token(self, installation_external_id: int) -> str:
            assert installation_external_id == 29
            return "new29-token"

    def provider(request: httpx.Request) -> httpx.Response:
        provider_calls.append(request.url.path)
        assert request.headers["Authorization"] == "Bearer new29-token"
        if request.url.path.endswith("/git/trees/main"):
            return httpx.Response(
                200, json={"tree": [{"path": "a.py", "type": "blob", "size": 12}]}
            )
        assert request.url.path.endswith("/labels")
        return httpx.Response(201, json={})

    engine = reinstall_env.engine()
    factory = async_sessionmaker(engine, expire_on_commit=False)
    resources = ReviewsApiResources(engine, factory)
    oauth = httpx.AsyncClient(base_url="https://github.test", transport=httpx.MockTransport(github))
    github_api = httpx.AsyncClient(
        base_url="https://api.github.test", transport=httpx.MockTransport(github)
    )
    installation_api = httpx.AsyncClient(
        base_url="https://api.github.test", transport=httpx.MockTransport(provider)
    )
    clock = [datetime.now(UTC)]
    receiver = ReceiveGitHubDelivery(
        uow_factory=resources.github_webhook_receipts,
        dispatcher=resources.github_installation_delivery_dispatcher(
            client=installation_api, token_provider=Tokens()
        ),
        now=lambda: clock[0],
    )
    app.dependency_overrides[get_github_webhook_secret] = lambda: _SECRET
    # Bob's second refresh replays the cookie already rotated after `deleted`, so it passes
    # only through the grace branch: an injected hour, not the real-clock 15 s default.
    app.dependency_overrides[get_refresh_local_session] = lambda: RefreshLocalSession(
        uow_factory=resources.auth_session_uow,
        issuer=Rs256AccessTokenIssuer(private_key, issuer="test-issuer", audience="test-audience"),
        grace_period=timedelta(hours=1),
    )

    try:
        with TestClient(app, base_url="https://testserver") as client:
            app.state.reviews_api_resources = resources
            app.state.github_auth_http_clients = GitHubAuthHttpClients(oauth, github_api)

            try:

                def callback(code: str) -> tuple[dict[str, str], str]:
                    response = client.post("/api/auth/github/callback", json={"code": code})
                    assert response.status_code == 200
                    cookie = client.cookies.get("refresh_token")
                    assert cookie is not None
                    return {"Authorization": f"Bearer {response.json()['accessToken']}"}, cookie

                def refresh(cookie: str) -> dict[str, str]:
                    count = len(oauth_calls)
                    response = client.post(
                        "/api/auth/refresh", headers={"Cookie": f"refresh_token={cookie}"}
                    )
                    assert response.status_code == 200
                    assert len(oauth_calls) == count
                    return {"Authorization": f"Bearer {response.json()['accessToken']}"}

                def delivery(payload: dict[str, Any], identity: str) -> None:
                    body = json.dumps(payload).encode()
                    signature = hmac.new(_SECRET.encode(), body, hashlib.sha256).hexdigest()
                    response = client.post(
                        "/webhooks/github",
                        content=body,
                        headers={
                            "X-GitHub-Event": "installation",
                            "X-GitHub-Delivery": identity,
                            "X-Hub-Signature-256": f"sha256={signature}",
                        },
                    )
                    assert response.status_code == 202
                    asyncio.run(receiver.replay_pending())

                alice, alice_cookie = callback("alice-17")
                bob, bob_cookie = callback("bob-17")
                assert client.get(f"/api/repos/{REPO_A}", headers=alice).status_code == 200
                assert client.get(f"/api/runs/{RUN_DONE}", headers=bob).status_code == 200
                asyncio.run(seed_custom(factory))
                before = asyncio.run(preserved(factory))
                deleted = {"action": "deleted", "installation": {"id": 17, "account": {"id": 700}}}
                delivery(deleted, "113-deleted17")
                delivery(deleted, "113-deleted17")
                for headers in (alice, bob, refresh(alice_cookie), refresh(bob_cookie)):
                    assert_denied(client, headers)
                created = {
                    "action": "created",
                    "installation": {"id": 29, "account": {"id": account_id}},
                    "repositories": [
                        {
                            "id": 101,
                            "full_name": "renamed-owner/a",
                            "default_branch": "main",
                            "html_url": "https://github.test/renamed-owner/a",
                        }
                    ],
                }
                delivery(created, "113-created29")
                clock[0] += timedelta(minutes=5)
                assert asyncio.run(receiver.replay_pending()) == 1
                assert provider_calls == []
                assert (
                    asyncio.run(
                        read(factory, "SELECT id FROM provider_installations WHERE external_id=29")
                    )
                    == []
                )
                for headers in (alice, bob):
                    assert_denied(client, headers)
                fresh_alice, _ = callback("alice-29")
                assert client.get(f"/api/repos/{REPO_A}", headers=bob).status_code == 404
                assert_denied(client, refresh(bob_cookie))
                asyncio.run(receiver.replay_pending())
                assert provider_calls == [
                    "/repos/renamed-owner/a/git/trees/main",
                    "/repos/renamed-owner/a/labels",
                ]
                assert asyncio.run(
                    read(
                        factory,
                        "SELECT external_id,account_external_id,deleted_at IS NOT NULL "
                        "FROM provider_installations WHERE id=:id",
                        id=INSTALL_A,
                    )
                ) == [(17, 700, True)]
                assert asyncio.run(preserved(factory)) == before
                listed = client.get("/api/repos", headers=fresh_alice)
                assert listed.status_code == 200 and len(listed.json()) == 1
                target = listed.json()[0]
                if account_id == 700:
                    assert target["id"] == str(REPO_A) and target["enabled"] is False
                    assert client.get(f"/api/repos/{REPO_A}", headers=alice).status_code == 200
                    assert client.get(f"/api/runs/{RUN_DONE}", headers=alice).status_code == 200
                    repository = client.get(f"/api/repos/{target['id']}", headers=fresh_alice)
                    assert repository.json()["branchFilter"] == ["main", "release/*"]
                    assert repository.json()["severityThreshold"] == "critical"
                    assert repository.json()["ragEnabled"] is True
                    assert asyncio.run(
                        read(
                            factory,
                            "SELECT "
                            "i.external_id,i.workspace_id,r.enabled,r.max_comments,r.wait_for_ci::text,"
                            "r.review_event::text,r.branch_filter,r.severity_threshold::text,"
                            "r.rag_enabled,w.daily_budget_usd FROM repositories r "
                            "JOIN provider_installations i ON i.id=r.provider_installation_id "
                            "JOIN workspaces w ON w.id=i.workspace_id WHERE r.id=:id",
                            id=REPO_A,
                        )
                    )[0] == (
                        29,
                        WS_A,
                        False,
                        7,
                        "always",
                        "request_changes",
                        ["main", "release/*"],
                        "critical",
                        True,
                        Decimal("12.34"),
                    )
                    assert asyncio.run(
                        read(
                            factory,
                            "SELECT state::text,error_code FROM runs WHERE id=:id",
                            id=RUN_ATTEMPTED,
                        )
                    ) == [("cancelled", "installation_replaced")]
                    assert client.get(
                        f"/api/runs/{RUN_DONE}/actions/0/response", headers=alice
                    ).json() == {"preserve": True}
                    calls = list(provider_calls)
                    cached = client.get(f"/api/runs/{RUN_DONE}/files?path=a.py", headers=alice)
                    assert cached.status_code == 200 and cached.json()["lines"] == [
                        "first",
                        "second",
                    ]
                    assert (
                        client.get(
                            f"/api/runs/{RUN_DONE}/files?path=absent.py", headers=alice
                        ).status_code
                        == 404
                    )
                    assert provider_calls == calls
                    assert (
                        client.post(f"/api/runs/{RUN_DONE}/rerun", headers=alice).status_code == 409
                    )
                    assert (
                        client.patch(
                            f"/api/repos/{REPO_A}", headers=alice, json={"enabled": True}
                        ).status_code
                        == 200
                    )
                    rerun = client.post(f"/api/runs/{RUN_DONE}/rerun", headers=alice)
                    assert rerun.status_code == 202
                    assert asyncio.run(
                        read(
                            factory,
                            "SELECT "
                            "i.external_id,"
                            "r.origin_provider_installation_id=p.provider_installation_id "
                            "FROM runs r JOIN code_changes c ON c.id=r.code_change_id "
                            "JOIN repositories p ON p.id=c.repository_id "
                            "JOIN provider_installations i ON "
                            "i.id=r.origin_provider_installation_id WHERE r.id=:id",
                            id=UUID(rerun.json()["id"]),
                        )
                    ) == [(29, True)]
                else:
                    assert target["id"] != str(REPO_A) and target["enabled"] is True
                    ((workspace, budget),) = asyncio.run(
                        read(
                            factory,
                            "SELECT i.workspace_id,w.daily_budget_usd FROM "
                            "provider_installations i JOIN workspaces w ON w.id=i.workspace_id "
                            "WHERE i.external_id=29",
                        )
                    )
                    # A fresh Workspace, not the archived one with its budget.
                    assert workspace != WS_A and budget == 0
                    assert (
                        client.get(f"/api/repos/{REPO_A}", headers=fresh_alice).status_code == 404
                    )
                    assert (
                        client.get(f"/api/runs/{RUN_DONE}", headers=fresh_alice).status_code == 404
                    )
                    assert client.get("/api/runs", headers=fresh_alice).json()["items"] == []
                    assert asyncio.run(
                        read(
                            factory,
                            "SELECT count(*) FROM repo_conventions WHERE repository_id=:id",
                            id=UUID(target["id"]),
                        )
                    ) == [(0,)]
                delivery(deleted, "113-stale-deleted17")
                delivery(created, "113-created29")
                assert len(provider_calls) == 2
                fresh_bob, _ = callback("bob-29")
                assert (
                    client.get(f"/api/repos/{target['id']}", headers=fresh_bob).status_code == 200
                )
                assert client.get(f"/api/repos/{REPO_A}", headers=bob).status_code == (
                    200 if account_id == 700 else 404
                )
            finally:
                del app.state.reviews_api_resources
    finally:
        app.dependency_overrides.clear()

        async def close() -> None:
            await oauth.aclose()
            await github_api.aclose()
            await installation_api.aclose()
            await engine.dispose()

        asyncio.run(close())
