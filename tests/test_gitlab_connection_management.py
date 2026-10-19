"""Supported GitLab management boundaries, using synthetic credentials only."""

from __future__ import annotations

import pytest

from app.modules.integrations.gitlab.application.manage_connection import (
    GitLabManagementError,
    GitLabOriginPolicy,
)


def test_operator_origin_is_explicitly_allowed_and_canonical() -> None:
    policy = GitLabOriginPolicy(frozenset({"https://gitlab.com"}))
    assert policy.validate("https://gitlab.com") == "https://gitlab.com"
    with pytest.raises(GitLabManagementError, match="origin_not_allowed"):
        policy.validate("https://other.example.com")


def test_create_and_bind_are_supported_without_a_database_transaction_during_http() -> None:
    import asyncio
    from collections.abc import Mapping
    from dataclasses import replace
    from types import TracebackType
    from typing import Self
    from uuid import UUID

    from app.modules.integrations.gitlab.application.manage_connection import (
        ConnectionSnapshot,
        CredentialEnvelope,
        ManageGitLabConnection,
    )
    from app.modules.repositories.application.installation_repositories import RepositorySnapshot
    from app.modules.repositories.application.onboard_repository import (
        DefaultRuleSet,
        PersistedRuleVersion,
    )
    from app.modules.repositories.application.sync_installation_repositories import (
        RepositoryOnboardingInput,
    )

    workspace = UUID("00000000-0000-0000-0000-000000000001")
    installation = UUID("00000000-0000-0000-0000-000000000002")
    repository = UUID("00000000-0000-0000-0000-000000000003")
    active = False
    commits: list[bool] = []

    class Store:
        connection: ConnectionSnapshot | None = None
        rules: PersistedRuleVersion | None = None

        async def workspace_exists(self, workspace_id: UUID, *, lock: bool = False) -> bool:
            return workspace_id == workspace

        async def connection_snapshot(
            self, installation_id: UUID, *, lock: bool = False
        ) -> ConnectionSnapshot | None:
            return self.connection

        async def create_connection(
            self, snapshot: ConnectionSnapshot, webhook_digest: str
        ) -> None:
            self.connection = snapshot

        async def bind_project(
            self, snapshot: ConnectionSnapshot, project: RepositorySnapshot
        ) -> tuple[UUID, bool]:
            assert project.full_name == "parent/nested/project"
            self.connection = replace(snapshot, bindings=((42, repository),))
            return repository, not snapshot.bindings

        async def prompt_ready(self) -> bool:
            return True

        async def get_active_rule_version(self, repository_id: UUID) -> PersistedRuleVersion | None:
            return self.rules

        async def get_or_create_initial_rule_version(
            self, repository_id: UUID, version: int, rules: list[dict[str, object]]
        ) -> PersistedRuleVersion:
            self.rules = PersistedRuleVersion(repository_id, version, rules, "a" * 64)
            return self.rules

    store = Store()

    class Uow:
        @property
        def connections(self) -> Store:
            return store

        async def __aenter__(self) -> Self:
            nonlocal active
            assert not active
            active = True
            return self

        async def __aexit__(
            self,
            exc_type: type[BaseException] | None,
            exc: BaseException | None,
            traceback: TracebackType | None,
        ) -> None:
            nonlocal active
            active = False

        async def commit(self) -> None:
            commits.append(True)

        async def rollback(self) -> None:
            pass

    class Codec:
        def encrypt(
            self, token: str, *, installation_id: UUID, workspace_id: UUID, instance_origin: str
        ) -> CredentialEnvelope:
            assert token == "synthetic-token"
            return CredentialEnvelope("test-key", b"synthetic-envelope")

        def decrypt(
            self,
            credential: CredentialEnvelope,
            *,
            installation_id: UUID,
            workspace_id: UUID,
            instance_origin: str,
        ) -> str:
            return "synthetic-token"

        def webhook_digest(self, secret: str) -> str:
            return "b" * 64

    class Projects:
        async def authenticate(self, *, origin: str, token: str) -> None:
            assert not active

        async def fetch_project(
            self, *, origin: str, token: str, project_id: int
        ) -> RepositoryOnboardingInput:
            assert not active
            return RepositoryOnboardingInput(
                RepositorySnapshot(
                    42, "parent/nested/project", "main", "https://gitlab.com/parent/nested/project"
                ),
                {"TypeScript": 5010, "Python": 4990},
            )

    rules: Mapping[str, DefaultRuleSet] = {
        "backend": DefaultRuleSet(1, "backend", [{"name": "backend-rule"}]),
        "frontend": DefaultRuleSet(1, "frontend", [{"name": "frontend-rule"}]),
    }
    management = ManageGitLabConnection(
        uow_factory=Uow,
        codec=Codec(),
        projects=Projects(),
        origin_policy=GitLabOriginPolicy(frozenset({"https://gitlab.com"})),
        rule_sets=rules,
    )

    async def exercise() -> None:
        created = await management.create(
            installation_id=installation,
            workspace_id=workspace,
            instance_origin="https://gitlab.com",
            token="synthetic-token",
            webhook_secret="s" * 32,
        )
        assert created.installation_id == installation
        assert created.intake_enabled is False
        bound = await management.bind_project(installation_id=installation, project_id=42)
        assert bound.repository_id == repository
        assert bound.created is True
        assert bound.rules_created is True
        assert store.rules is not None
        assert store.rules.rules == [{"name": "frontend-rule"}]
        repeated = await management.bind_project(installation_id=installation, project_id=42)
        assert repeated.created is False
        assert repeated.rules_created is False
        assert len(commits) == 3

    asyncio.run(exercise())


def test_authenticated_project_reads_keep_nested_namespace_and_percentage_precision() -> None:
    import asyncio

    import httpx

    from app.modules.integrations.gitlab.infrastructure.project_details import GitLabProjectDetails

    paths: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        assert request.headers["PRIVATE-TOKEN"] == "synthetic-token"
        assert request.method == "GET"
        if request.url.path == "/api/v4/user":
            return httpx.Response(200, json={"id": 7, "state": "active"})
        if request.url.path == "/api/v4/projects/42":
            return httpx.Response(
                200,
                json={
                    "id": 42,
                    "path_with_namespace": "parent/nested/project",
                    "default_branch": "main",
                    "web_url": "https://gitlab.com/parent/nested/project",
                    "archived": False,
                    "empty_repo": False,
                },
            )
        return httpx.Response(200, json={"Python": 49.9, "TypeScript": 50.1})

    async def exercise() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(respond), follow_redirects=True
        ) as client:
            details = GitLabProjectDetails(
                client, origin_policy=GitLabOriginPolicy(frozenset({"https://gitlab.com"}))
            )
            project = await details.fetch_project(
                origin="https://gitlab.com", token="synthetic-token", project_id=42
            )
            assert project.snapshot.full_name == "parent/nested/project"
            assert project.languages == {"Python": 4990, "TypeScript": 5010}
        assert paths == ["/api/v4/user", "/api/v4/projects/42", "/api/v4/projects/42/languages"]

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "origin",
    [
        "https://GitLab.com",
        "https://gitlab.com/",
        "https://gitlab.com:443",
        "https://user@gitlab.com",
        "https://gitlab.com?x=1",
        "https://gitlab.com#x",
        "http://gitlab.com",
        "https://127.0.0.01",
        "https://gitlab.com:0444",
        "https://gitlab.com.",
        "https://[2001:0db8::1]",
    ],
)
def test_noncanonical_origins_are_rejected(origin: str) -> None:
    with pytest.raises(GitLabManagementError, match="invalid_origin"):
        GitLabOriginPolicy(frozenset({origin})).validate(origin)


@pytest.mark.parametrize(
    "origin",
    ["https://127.0.0.1:8443", "https://[::1]:8443", "https://localhost", "https://gitlab.local"],
)
def test_private_origins_require_separate_explicit_opt_in(origin: str) -> None:
    with pytest.raises(GitLabManagementError, match="private_origin_not_allowed"):
        GitLabOriginPolicy(frozenset({origin})).validate(origin)
    assert GitLabOriginPolicy(frozenset({origin}), frozenset({origin})).validate(origin) == origin


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"id": 43}, "project_identity_mismatch"),
        ({"id": True}, "project_identity_mismatch"),
        ({"path_with_namespace": "parent/../project"}, "invalid_project"),
        ({"path_with_namespace": "parent//project"}, "invalid_project"),
        ({"default_branch": None}, "invalid_project"),
        ({"default_branch": "main\n"}, "invalid_project"),
        ({"web_url": "https://other.example.com/parent/nested/project"}, "invalid_project"),
        ({"web_url": "https://gitlab.com/other/project"}, "invalid_project"),
        ({"web_url": "https://user@gitlab.com/parent/nested/project"}, "invalid_project"),
        ({"archived": True}, "project_unavailable"),
        ({"empty_repo": True}, "project_unavailable"),
    ],
)
def test_malformed_or_unavailable_project_is_rejected(
    payload: dict[str, object], expected: str
) -> None:
    _project_failure(project_patch=payload, expected=expected)


@pytest.mark.parametrize(
    "languages",
    [
        b'{"Python": -1}',
        b'{"Python": 101}',
        b'{"Python": NaN}',
        b'{"Python": Infinity}',
        b'{"Python": true}',
        b'{"Python": "50"}',
        b'{"Python": null}',
        b'{"Python": 1e-1000000}',
        b'{"": 50}',
        b"[]",
    ],
)
def test_malformed_language_percentages_are_rejected(languages: bytes) -> None:
    _project_failure(languages=languages, expected="invalid_languages")


def _project_failure(
    *,
    project_patch: dict[str, object] | None = None,
    languages: bytes = b'{"Python": 100}',
    expected: str,
) -> None:
    import asyncio

    import httpx

    from app.modules.integrations.gitlab.infrastructure.project_details import GitLabProjectDetails

    project: dict[str, object] = {
        "id": 42,
        "path_with_namespace": "parent/nested/project",
        "default_branch": "main",
        "web_url": "https://gitlab.com/parent/nested/project",
        "archived": False,
        "empty_repo": False,
    }
    project.update(project_patch or {})

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v4/user":
            return httpx.Response(200, json={"id": 7, "state": "active"})
        if request.url.path.endswith("/languages"):
            return httpx.Response(200, content=languages)
        return httpx.Response(200, json=project)

    async def exercise() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            details = GitLabProjectDetails(
                client, origin_policy=GitLabOriginPolicy(frozenset({"https://gitlab.com"}))
            )
            with pytest.raises(GitLabManagementError, match=expected):
                await details.fetch_project(
                    origin="https://gitlab.com", token="synthetic-token", project_id=42
                )

    asyncio.run(exercise())


@pytest.mark.parametrize("status", [301, 302, 307, 401, 403, 429, 500])
def test_invalid_authentication_cannot_use_public_project_or_follow_redirect(status: int) -> None:
    import asyncio

    import httpx

    from app.modules.integrations.gitlab.infrastructure.project_details import GitLabProjectDetails

    calls: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(
            status,
            headers={"Location": "https://elsewhere.example.com/stolen"},
            text="synthetic-secret-canary",
        )

    async def exercise() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(respond), follow_redirects=True
        ) as client:
            details = GitLabProjectDetails(
                client, origin_policy=GitLabOriginPolicy(frozenset({"https://gitlab.com"}))
            )
            with pytest.raises(GitLabManagementError) as failure:
                await details.fetch_project(
                    origin="https://gitlab.com", token="synthetic-secret-canary", project_id=42
                )
            assert "canary" not in str(failure.value)
        assert calls == ["/api/v4/user"]

    asyncio.run(exercise())


@pytest.mark.parametrize("origin", ["https://[fe80::1%25eth0]", "https://0x7f000001"])
def test_address_aliases_are_rejected_even_with_private_opt_in(origin: str) -> None:
    with pytest.raises(GitLabManagementError, match="invalid_origin"):
        GitLabOriginPolicy(frozenset({origin}), frozenset({origin})).validate(origin)


@pytest.mark.parametrize(
    "identity",
    [
        {"id": 1, "state": "blocked"},
        {"id": True, "state": "active"},
        {"id": 0, "state": "active"},
        {"id": 1},
    ],
)
def test_authenticated_identity_requires_active_numeric_user(identity: dict[str, object]) -> None:
    import asyncio

    import httpx

    from app.modules.integrations.gitlab.infrastructure.project_details import GitLabProjectDetails

    async def exercise() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json=identity))
        ) as client:
            details = GitLabProjectDetails(
                client, origin_policy=GitLabOriginPolicy(frozenset({"https://gitlab.com"}))
            )
            with pytest.raises(GitLabManagementError, match="invalid_token"):
                await details.authenticate(origin="https://gitlab.com", token="synthetic-token")

    asyncio.run(exercise())


def test_zero_unknown_and_precise_small_percentage_weights_remain_deterministic() -> None:
    import asyncio

    import httpx

    from app.modules.integrations.gitlab.infrastructure.project_details import GitLabProjectDetails

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/user"):
            return httpx.Response(200, json={"id": 1, "state": "active"})
        if request.url.path.endswith("/languages"):
            return httpx.Response(
                200, content=b'{"Unknown": 99.999, "Python": 0, "TypeScript": 0.001}'
            )
        return httpx.Response(
            200,
            json={
                "id": 42,
                "path_with_namespace": "parent/nested/project",
                "default_branch": "main",
                "web_url": "https://gitlab.com/parent/nested/project",
                "archived": False,
                "empty_repo": False,
            },
        )

    async def exercise() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            details = GitLabProjectDetails(
                client, origin_policy=GitLabOriginPolicy(frozenset({"https://gitlab.com"}))
            )
            project = await details.fetch_project(
                origin="https://gitlab.com", token="synthetic-token", project_id=42
            )
            assert project.languages == {"Unknown": 99999, "TypeScript": 1}

    asyncio.run(exercise())


@pytest.mark.parametrize("token", ["", "unsafe token", "unsafe\ncanary", "é", "t" * 4097])
def test_malformed_tokens_are_rejected_before_any_http(token: str) -> None:
    import asyncio

    import httpx

    from app.modules.integrations.gitlab.infrastructure.project_details import GitLabProjectDetails

    def unexpected(request: httpx.Request) -> httpx.Response:
        raise AssertionError("HTTP must not be entered for malformed credentials")

    async def exercise() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(unexpected)) as client:
            details = GitLabProjectDetails(
                client, origin_policy=GitLabOriginPolicy(frozenset({"https://gitlab.com"}))
            )
            with pytest.raises(GitLabManagementError, match="invalid_token"):
                await details.authenticate(origin="https://gitlab.com", token=token)

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "content",
    [
        b"<html>synthetic-secret-canary</html>",
        b'{"id":',
        b"null",
        b"[]",
        b'"synthetic-secret-canary"',
    ],
)
def test_malformed_identity_response_is_sanitized(content: bytes) -> None:
    import asyncio

    import httpx

    from app.modules.integrations.gitlab.infrastructure.project_details import GitLabProjectDetails

    async def exercise() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, content=content))
        ) as client:
            details = GitLabProjectDetails(
                client, origin_policy=GitLabOriginPolicy(frozenset({"https://gitlab.com"}))
            )
            with pytest.raises(GitLabManagementError) as failure:
                await details.authenticate(
                    origin="https://gitlab.com", token="synthetic-secret-canary"
                )
            assert "canary" not in str(failure.value)

    asyncio.run(exercise())


def test_transport_failure_and_secret_value_representations_are_sanitized() -> None:
    import asyncio
    from uuid import UUID

    import httpx

    from app.modules.integrations.gitlab.application.manage_connection import (
        ConnectionSnapshot,
        CredentialEnvelope,
    )
    from app.modules.integrations.gitlab.infrastructure.project_details import GitLabProjectDetails

    credential = CredentialEnvelope("synthetic-key-canary", b"synthetic-ciphertext-canary")
    snapshot = ConnectionSnapshot(UUID(int=1), UUID(int=2), "https://gitlab.com", 1, credential)
    assert "canary" not in repr(credential)
    assert "canary" not in repr(snapshot)

    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("synthetic-token-canary", request=request)

    async def exercise() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(fail)) as client:
            details = GitLabProjectDetails(
                client, origin_policy=GitLabOriginPolicy(frozenset({"https://gitlab.com"}))
            )
            with pytest.raises(GitLabManagementError, match="project_access_failed") as failure:
                await details.authenticate(
                    origin="https://gitlab.com", token="synthetic-token-canary"
                )
            assert "canary" not in str(failure.value)
            assert failure.value.__suppress_context__ is True

    asyncio.run(exercise())


def test_oversized_stream_stops_at_body_limit_and_always_closes_response() -> None:
    import asyncio
    from collections.abc import AsyncIterator

    import httpx

    from app.modules.integrations.gitlab.infrastructure.project_details import GitLabProjectDetails

    class OversizedBody(httpx.AsyncByteStream):
        reads = 0
        closed = False

        async def __aiter__(self) -> AsyncIterator[bytes]:
            for _ in range(100):
                self.reads += 1
                assert self.reads <= 17, "read beyond 1 MiB body limit"
                yield b"x" * 65536

        async def aclose(self) -> None:
            self.closed = True

    body = OversizedBody()

    async def exercise() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=body))
        ) as client:
            details = GitLabProjectDetails(
                client, origin_policy=GitLabOriginPolicy(frozenset({"https://gitlab.com"}))
            )
            with pytest.raises(GitLabManagementError, match="invalid_token"):
                await details.authenticate(origin="https://gitlab.com", token="synthetic-token")
        assert body.reads == 17
        assert body.closed is True

    asyncio.run(exercise())
