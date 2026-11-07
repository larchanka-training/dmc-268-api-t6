"""Bitbucket credential rows keep secret and tenant authority isolated (#333)."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Connection, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from alembic import command
from app.bootstrap.bitbucket import get_bitbucket_management
from app.bootstrap.portal_auth import get_auth_scope
from app.bootstrap.reviews_api import ReviewsApiResources
from app.main import app
from app.modules.auth.application.scope import AuthScope
from app.modules.integrations.bitbucket.application.manage_connection import (
    ManageBitbucketConnection,
)
from app.modules.integrations.bitbucket.application.provider_error import BitbucketProviderError
from app.modules.integrations.bitbucket.application.request_credentials import (
    BitbucketTargetInactive,
)
from app.modules.integrations.bitbucket.infrastructure.connection_store import (
    SqlAlchemyBitbucketConnectionStore,
    SqlAlchemyBitbucketManagementUnitOfWork,
)
from app.modules.integrations.bitbucket.infrastructure.credential_cipher import (
    BitbucketCredentialCipher,
    CredentialContext,
    CredentialPurpose,
)
from app.modules.integrations.bitbucket.infrastructure.credential_codec import (
    BitbucketCredentialCodecAdapter,
)
from app.modules.integrations.bitbucket.infrastructure.repository_provider import (
    BitbucketCloudRepositoryProvider,
    BitbucketRepositorySnapshot,
)
from app.modules.integrations.bitbucket.infrastructure.request_credentials import (
    SqlAlchemyBitbucketTargetReader,
)
from app.modules.repositories.application.onboard_repository import load_default_rule_sets
from tests.bitbucket_connection_postgres_support import _config

pytest_plugins = ("tests.bitbucket_connection_postgres_support",)


@pytest.mark.integration
def test_browser_connect_reuses_one_credential_and_disconnects_one_repository(
    bitbucket_connection_schema: Connection,
) -> None:
    db = bitbucket_connection_schema
    command.upgrade(_config(db), "head")
    workspace, remote_workspace, first_uuid, second_uuid = uuid4(), uuid4(), uuid4(), uuid4()
    db.execute(
        text("INSERT INTO workspaces (id, name, daily_budget_usd) VALUES (:id, 'test', 7)"),
        {"id": workspace},
    )
    db.execute(
        text("INSERT INTO github_user_profiles (id, login, name) VALUES (42, 'alice', 'Alice')")
    )
    db.execute(
        text(
            "INSERT INTO github_user_workspace_access (github_user_id, workspace_id) "
            "VALUES (42, :workspace)"
        ),
        {"workspace": workspace},
    )
    schema = str(db.scalar(text("SELECT current_schema()")))
    db.commit()
    database_url = make_url(os.environ["TEST_DATABASE_URL"]).set(
        drivername="postgresql+psycopg_async"
    )
    engine = create_async_engine(database_url, connect_args={"options": f"-csearch_path={schema}"})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    rotated_repositories: list[str] = []
    probe_requests: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] in {
            "Bearer shared-token",
            "Bearer different-token",
            "Bearer rotated-token",
            "Bearer transferred-token",
            "Bearer repository-only-token",
        }
        if request.url.path == "/2.0/user":
            return httpx.Response(200, json={"uuid": "{00000000-0000-4000-8000-000000000555}"})
        if request.url.path.endswith("/pullrequests"):
            probe_requests.append(str(request.url))
            if request.headers["Authorization"] == "Bearer repository-only-token":
                return httpx.Response(403, json={"error": {"message": "scope denied"}})
            return httpx.Response(200, json={"values": [], "size": 0, "pagelen": 1})
        if request.url.path == "/2.0/repositories/other-space/first":
            return httpx.Response(
                200,
                json={
                    "uuid": "{" + str(first_uuid) + "}",
                    "full_name": "other-space/first",
                    "slug": "first",
                    "name": "first",
                    "is_private": True,
                    "workspace": {
                        "uuid": "{d9fb570e-22f0-4d8e-868c-0474ab37ce68}",
                        "slug": "other-space",
                    },
                    "links": {"html": {"href": "https://bitbucket.org/other-space/first"}},
                    "mainbranch": {"name": "main"},
                },
            )
        if request.url.path == "/2.0/repositories/nvdovnar":
            return httpx.Response(
                200,
                json={
                    "values": [
                        {
                            "uuid": "{" + str(first_uuid) + "}",
                            "full_name": "nvdovnar/first",
                            "slug": "first",
                            "name": "first",
                            "is_private": True,
                            "workspace": {
                                "uuid": "{" + str(remote_workspace) + "}",
                                "slug": "nvdovnar",
                            },
                            "links": {"html": {"href": "https://bitbucket.org/nvdovnar/first"}},
                            "mainbranch": {"name": "main"},
                            "language": "Python",
                        }
                    ]
                },
            )
        slug = request.url.path.rsplit("/", 1)[-1]
        if request.url.path.startswith(f"/2.0/repositories/{{{remote_workspace}}}/"):
            remote_uuid = UUID(slug.strip("{}"))
            assert remote_uuid in {first_uuid, second_uuid}
            slug = "first-renamed" if remote_uuid == first_uuid else "second-renamed"
            if request.headers["Authorization"] == "Bearer rotated-token":
                rotated_repositories.append(slug)
        elif request.headers["Authorization"] == "Bearer rotated-token":
            assert slug == "first-renamed"
            remote_uuid = first_uuid
        else:
            assert slug in {"first", "second"}
            remote_uuid = first_uuid if slug == "first" else second_uuid
        returned_workspace = (
            UUID("d9fb570e-22f0-4d8e-868c-0474ab37ce68")
            if request.headers["Authorization"] == "Bearer transferred-token"
            else remote_workspace
        )
        return httpx.Response(
            200,
            json={
                "uuid": "{" + str(remote_uuid) + "}",
                "full_name": f"nvdovnar/{slug}",
                "slug": slug,
                "name": slug,
                "is_private": True,
                "workspace": {"uuid": "{" + str(returned_workspace) + "}", "slug": "nvdovnar"},
                "links": {"html": {"href": f"https://bitbucket.org/nvdovnar/{slug}"}},
                "mainbranch": {"name": "main"},
                "language": "Python",
            },
        )

    class RacingProvider(BitbucketCloudRepositoryProvider):
        race_target: tuple[UUID, UUID] | None = None
        fail_uuid: UUID | None = None

        async def fetch_repository_by_uuid(
            self, *, repository_uuid: UUID, token: str, workspace_uuid: UUID | None = None
        ) -> BitbucketRepositorySnapshot:
            if self.fail_uuid == repository_uuid:
                self.fail_uuid = None
                raise BitbucketProviderError("provider_unavailable")
            if self.race_target is not None:
                installation_id, repository_id = self.race_target
                self.race_target = None
                async with factory() as session:
                    store = SqlAlchemyBitbucketConnectionStore(session)
                    assert await store.disconnect_repository(
                        workspace_id=workspace,
                        installation_id=installation_id,
                        repository_id=repository_id,
                    )
                    rebound, newly_active = await store.bind_repository(
                        workspace_id=workspace,
                        installation_id=installation_id,
                        repository_uuid=first_uuid,
                        full_name="nvdovnar/first",
                        default_branch="main",
                        web_url="https://bitbucket.org/nvdovnar/first",
                    )
                    assert rebound == repository_id and newly_active
                    await store.set_repository_webhook_secret(
                        workspace_id=workspace,
                        installation_id=installation_id,
                        repository_id=repository_id,
                        secret=BitbucketCredentialCipher(
                            active_key_id="test", keys={"test": b"a" * 32}
                        ).encrypt(
                            "r" * 40,
                            context=CredentialContext(installation_id, workspace, repository_id),
                            purpose=CredentialPurpose.WEBHOOK_SECRET,
                        ),
                    )
                    await session.commit()
            return await super().fetch_repository_by_uuid(
                repository_uuid=repository_uuid, token=token, workspace_uuid=workspace_uuid
            )

    provider_http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    racing_provider = RacingProvider(provider_http)
    manager = ManageBitbucketConnection(
        uow_factory=lambda: SqlAlchemyBitbucketManagementUnitOfWork(factory),
        cipher=BitbucketCredentialCodecAdapter(
            BitbucketCredentialCipher(active_key_id="test", keys={"test": b"a" * 32})
        ),
        provider=racing_provider,
        rule_sets=load_default_rule_sets(Path(__file__).resolve().parents[1] / "review" / "rules"),
        webhook_base_url="https://portal.example",
    )
    app.state.reviews_api_resources = ReviewsApiResources(engine, factory)
    app.dependency_overrides[get_auth_scope] = lambda: AuthScope(42, (workspace,))
    app.dependency_overrides[get_bitbucket_management] = lambda: manager

    def rotation_state() -> tuple[tuple[object, ...], tuple[tuple[object, ...], ...]]:
        state = (
            tuple(
                db.execute(
                    text(
                        "SELECT credential_generation, token_key_id, token_ciphertext, "
                        "intake_enabled FROM bitbucket_connections "
                        "WHERE provider_installation_id=:id"
                    ),
                    {"id": installation_id},
                ).one()
            ),
            tuple(
                tuple(row)
                for row in db.execute(
                    text(
                        "SELECT full_name, default_branch, web_url FROM repositories "
                        "WHERE provider_installation_id=:id ORDER BY id"
                    ),
                    {"id": installation_id},
                ).all()
            ),
        )
        db.commit()
        return state

    try:
        client = TestClient(app)
        url = f"/api/workspaces/{workspace}/bitbucket/connections"
        discovery = client.post(
            f"/api/workspaces/{workspace}/bitbucket/discovery",
            json={"bitbucketWorkspace": "nvdovnar", "apiToken": "shared-token"},
        )
        assert discovery.status_code == 200
        assert discovery.json()["repositories"][0]["uuid"] == str(first_uuid)
        assert discovery.json()["repositories"][0]["isPrivate"] is True
        assert "shared-token" not in discovery.text
        first = client.post(
            url,
            json={
                "bitbucketWorkspace": "nvdovnar",
                "repositorySlug": "first",
                "repositoryUuid": str(first_uuid),
                "apiToken": "shared-token",
            },
        )
        assert first.status_code == 201
        assert first.json()["repositoryUuid"] == str(first_uuid)
        assert first.json()["webhook"]["secret"]
        second = client.post(
            url,
            json={
                "bitbucketWorkspace": "nvdovnar",
                "repositorySlug": "second",
                "repositoryUuid": str(second_uuid),
                "apiToken": "shared-token",
            },
        )
        assert second.status_code == 201
        missing_pr_scope_on_connect = client.post(
            url,
            json={
                "bitbucketWorkspace": "other-space",
                "repositorySlug": "first",
                "repositoryUuid": str(first_uuid),
                "apiToken": "repository-only-token",
            },
        )
        assert missing_pr_scope_on_connect.status_code == 422
        assert missing_pr_scope_on_connect.json() == {"detail": "repository_inaccessible"}
        other_workspace_same_repository = client.post(
            url,
            json={
                "bitbucketWorkspace": "other-space",
                "repositorySlug": "first",
                "repositoryUuid": str(first_uuid),
                "apiToken": "shared-token",
            },
        )
        assert other_workspace_same_repository.status_code == 409
        assert other_workspace_same_repository.json() == {"detail": "repository_uuid_conflict"}
        assert len(client.get(url).json()["connections"]) == 2
        assert len(probe_requests) == 4
        assert all("pullrequests?pagelen=1" in request for request in probe_requests)
        installation_id = first.json()["id"]
        db.execute(
            text(
                "UPDATE bitbucket_connections SET intake_enabled=true "
                "WHERE provider_installation_id=:id"
            ),
            {"id": installation_id},
        )
        db.commit()
        before_failed_rotation = rotation_state()
        missing_pr_scope = client.put(
            f"{url}/{first.json()['id']}/credential",
            json={"apiToken": "repository-only-token"},
        )
        assert missing_pr_scope.status_code == 422
        assert missing_pr_scope.json() == {"detail": "repository_inaccessible"}
        assert rotation_state() == before_failed_rotation
        assert client.get(url).json()["connections"][0]["credentialGeneration"] == 1
        transferred = client.put(
            f"{url}/{first.json()['id']}/credential",
            json={"apiToken": "transferred-token"},
        )
        assert transferred.status_code == 409
        assert transferred.json() == {"detail": "repository_uuid_conflict"}
        assert rotation_state() == before_failed_rotation
        assert client.get(url).json()["connections"][0]["credentialGeneration"] == 1
        assert second.json()["id"] == first.json()["id"]
        assert second.json()["webhook"]["secret"]
        assert second.json()["webhook"]["secret"] != first.json()["webhook"]["secret"]
        for connected in (first, second):
            assert (
                db.scalar(
                    text(
                        "SELECT refresh_requested_at FROM repository_rules_status "
                        "WHERE repository_id=:id"
                    ),
                    {"id": connected.json()["repositoryId"]},
                )
                is not None
            )
        assert first.json()["repositoryId"] in first.json()["webhook"]["url"]
        assert second.json()["repositoryId"] in second.json()["webhook"]["url"]
        first_secret_before = db.scalar(
            text(
                "SELECT secret_ciphertext FROM bitbucket_repository_webhook_secrets "
                "WHERE repository_id=:id"
            ),
            {"id": first.json()["repositoryId"]},
        )
        assert first_secret_before is not None
        rotated_hook = client.post(
            f"{url}/{second.json()['id']}/repositories/"
            f"{second.json()['repositoryId']}/webhook-secret"
        )
        assert rotated_hook.status_code == 200
        assert rotated_hook.json()["secret"] != second.json()["webhook"]["secret"]
        assert (
            db.scalar(
                text(
                    "SELECT secret_ciphertext FROM bitbucket_repository_webhook_secrets "
                    "WHERE repository_id=:id"
                ),
                {"id": first.json()["repositoryId"]},
            )
            == first_secret_before
        )
        racing_provider.race_target = (UUID(first.json()["id"]), UUID(first.json()["repositoryId"]))
        stale_rotation = client.put(
            f"{url}/{first.json()['id']}/credential",
            json={"apiToken": "rotated-token"},
        )
        assert stale_rotation.status_code == 409
        assert stale_rotation.json()["detail"] == "connection_changed"
        assert rotation_state() == before_failed_rotation
        racing_provider.fail_uuid = second_uuid
        failed_rotation = client.put(
            f"{url}/{first.json()['id']}/credential",
            json={"apiToken": "rotated-token"},
        )
        assert failed_rotation.status_code == 502
        assert failed_rotation.json() == {"detail": "provider_unavailable"}
        assert failed_rotation.headers["X-Bitbucket-Repository-UUID"] == str(second_uuid)
        assert "rotated-token" not in failed_rotation.text
        assert rotation_state() == before_failed_rotation
        rotated_credential = client.put(
            f"{url}/{first.json()['id']}/credential",
            json={"apiToken": "rotated-token"},
        )
        assert rotated_credential.status_code == 200
        assert rotated_credential.json()["credentialGeneration"] == 2
        assert rotation_state()[0][3] is True
        assert set(rotated_repositories) == {"first-renamed", "second-renamed"}
        assert {item["repositorySlug"] for item in client.get(url).json()["connections"]} == {
            "first-renamed",
            "second-renamed",
        }
        assert {item["repositoryId"] for item in client.get(url).json()["connections"]} == {
            first.json()["repositoryId"],
            second.json()["repositoryId"],
        }
        assert {item["id"] for item in client.get("/api/repos").json()} == {
            first.json()["repositoryId"],
            second.json()["repositoryId"],
        }
        target_reader = SqlAlchemyBitbucketTargetReader(factory)
        sibling_target = asyncio.run(
            target_reader.resolve_target(
                workspace_id=workspace,
                installation_id=UUID(first.json()["id"]),
                repository_uuid=second_uuid,
            )
        )
        disconnected_target = asyncio.run(
            target_reader.resolve_target(
                workspace_id=workspace,
                installation_id=UUID(first.json()["id"]),
                repository_uuid=first_uuid,
            )
        )
        disconnect = client.delete(
            f"{url}/{first.json()['id']}/repositories/{first.json()['repositoryId']}"
        )
        assert disconnect.status_code == 204
        repeated_disconnect = client.delete(
            f"{url}/{first.json()['id']}/repositories/{first.json()['repositoryId']}"
        )
        assert repeated_disconnect.status_code == 204
        assert [item["repositoryId"] for item in client.get(url).json()["connections"]] == [
            second.json()["repositoryId"]
        ]
        assert [item["id"] for item in client.get("/api/repos").json()] == [
            second.json()["repositoryId"]
        ]
        asyncio.run(target_reader.checkpoint(sibling_target))
        with pytest.raises(BitbucketTargetInactive):
            asyncio.run(target_reader.checkpoint(disconnected_target))
        assert (
            db.scalar(
                text(
                    "SELECT secret_ciphertext FROM bitbucket_repository_webhook_secrets "
                    "WHERE repository_id=:id"
                ),
                {"id": first.json()["repositoryId"]},
            )
            is None
        )
        conflicting = client.post(
            url,
            json={
                "bitbucketWorkspace": "nvdovnar",
                "repositorySlug": "first",
                "repositoryUuid": str(first_uuid),
                "apiToken": "different-token",
            },
        )
        assert conflicting.status_code == 409
        assert conflicting.json() == {"detail": "credential_rotation_required"}
        # The final disconnect must count only this installation's repositories,
        # even while another Bitbucket installation and other providers stay live.
        other_workspace = uuid4()
        db.execute(
            text("INSERT INTO workspaces (id, name, daily_budget_usd) VALUES (:id, 'other', 7)"),
            {"id": other_workspace},
        )
        unrelated_repositories: list[UUID] = []
        for provider, owner, external_id in (
            ("github", workspace, 900001),
            ("gitlab", other_workspace, 900002),
        ):
            other_installation, other_repository = uuid4(), uuid4()
            db.execute(
                text(
                    "INSERT INTO provider_installations "
                    '(id, workspace_id, provider, external_id, "metadata") '
                    "VALUES (:id, :workspace, :provider, :external_id, '{}'::jsonb)"
                ),
                {
                    "id": other_installation,
                    "workspace": owner,
                    "provider": provider,
                    "external_id": external_id,
                },
            )
            db.execute(
                text(
                    "INSERT INTO repositories "
                    "(id, provider_installation_id, external_id, full_name, "
                    "default_branch, web_url) "
                    "VALUES (:id, :installation, :external_id, :name, 'main', :url)"
                ),
                {
                    "id": other_repository,
                    "installation": other_installation,
                    "external_id": external_id,
                    "name": f"{provider}/unrelated",
                    "url": f"https://example.test/{provider}/unrelated",
                },
            )
            unrelated_repositories.append(other_repository)
        db.commit()
        peer_installation = uuid4()
        peer_remote_workspace = uuid4()
        peer_remote_repository = uuid4()

        async def create_peer_bitbucket_connection() -> UUID:
            peer_cipher = BitbucketCredentialCipher(active_key_id="test", keys={"test": b"a" * 32})
            async with factory() as session:
                store = SqlAlchemyBitbucketConnectionStore(session)
                await store.create_connection(
                    workspace_id=workspace,
                    installation_id=peer_installation,
                    bitbucket_workspace_uuid=peer_remote_workspace,
                    token=peer_cipher.encrypt(
                        "peer-token",
                        context=CredentialContext(peer_installation, workspace),
                        purpose=CredentialPurpose.TOKEN,
                    ),
                )
                peer_repository, created = await store.bind_repository(
                    workspace_id=workspace,
                    installation_id=peer_installation,
                    repository_uuid=peer_remote_repository,
                    full_name="peer/active",
                    default_branch="main",
                    web_url="https://bitbucket.org/peer/active",
                )
                assert created
                await store.set_repository_webhook_secret(
                    workspace_id=workspace,
                    installation_id=peer_installation,
                    repository_id=peer_repository,
                    secret=peer_cipher.encrypt(
                        "h" * 40,
                        context=CredentialContext(peer_installation, workspace, peer_repository),
                        purpose=CredentialPurpose.WEBHOOK_SECRET,
                    ),
                )
                await session.commit()
                return peer_repository

        peer_repository = asyncio.run(create_peer_bitbucket_connection())
        assert (
            client.delete(
                f"{url}/{second.json()['id']}/repositories/{second.json()['repositoryId']}"
            ).status_code
            == 204
        )
        revoked_connection = db.execute(
            text(
                "SELECT revoked_at, token_ciphertext FROM bitbucket_connections "
                "WHERE provider_installation_id=:installation"
            ),
            {"installation": first.json()["id"]},
        ).one()
        assert revoked_connection.revoked_at is not None
        assert revoked_connection.token_ciphertext is None
        peer_connection = db.execute(
            text(
                "SELECT revoked_at, token_ciphertext FROM bitbucket_connections "
                "WHERE provider_installation_id=:installation"
            ),
            {"installation": peer_installation},
        ).one()
        assert peer_connection.revoked_at is None
        assert peer_connection.token_ciphertext is not None
        assert db.scalar(
            text("SELECT installation_available FROM repositories WHERE id=:id"),
            {"id": peer_repository},
        )
        assert (
            db.scalar(
                text(
                    "SELECT count(*) FROM repositories "
                    "WHERE id IN (:first, :second) AND installation_available"
                ),
                {"first": unrelated_repositories[0], "second": unrelated_repositories[1]},
            )
            == 2
        )
        db.execute(
            text(
                "UPDATE repository_rules_status SET refresh_requested_at=NULL "
                "WHERE repository_id=:id"
            ),
            {"id": first.json()["repositoryId"]},
        )
        db.commit()
        reconnected = client.post(
            url,
            json={
                "bitbucketWorkspace": "nvdovnar",
                "repositorySlug": "first-renamed",
                "repositoryUuid": str(first_uuid),
                "apiToken": "rotated-token",
            },
        )
        assert reconnected.status_code == 201
        assert reconnected.json()["id"] == first.json()["id"]
        assert reconnected.json()["webhook"]["secret"] != first.json()["webhook"]["secret"]
        assert (
            db.scalar(
                text(
                    "SELECT refresh_requested_at FROM repository_rules_status "
                    "WHERE repository_id=:id"
                ),
                {"id": first.json()["repositoryId"]},
            )
            is not None
        )
        db.execute(
            text(
                "DELETE FROM github_user_workspace_access "
                "WHERE github_user_id=42 AND workspace_id=:workspace"
            ),
            {"workspace": workspace},
        )
        db.commit()
        assert client.get(url).status_code == 404
        assert (
            client.post(
                f"/api/workspaces/{workspace}/bitbucket/discovery",
                json={"bitbucketWorkspace": "nvdovnar", "apiToken": "shared-token"},
            ).status_code
            == 404
        )
        assert (
            client.put(
                f"{url}/{first.json()['id']}/credential",
                json={"apiToken": "rotated-token"},
            ).status_code
            == 404
        )
        assert (
            client.delete(
                f"{url}/{first.json()['id']}/repositories/{first.json()['repositoryId']}"
            ).status_code
            == 404
        )
        assert (
            client.post(
                f"{url}/{first.json()['id']}/repositories/"
                f"{first.json()['repositoryId']}/webhook-secret"
            ).status_code
            == 404
        )
    finally:
        app.dependency_overrides.clear()
        del app.state.reviews_api_resources
        asyncio.run(provider_http.aclose())
        asyncio.run(engine.dispose())
