"""GitHub App callback issues a durable, scoped local session."""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime, timedelta
from types import TracebackType
from uuid import UUID, uuid4

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)
from fastapi.testclient import TestClient

from app.main import app
from app.modules.auth.application.exchange_github_code import (
    AuthenticatedUser,
    ExchangeGitHubCode,
    GitHubProviderUnavailable,
    InvalidGitHubCode,
)
from app.modules.auth.infrastructure.github_oauth import (
    HttpGitHubOAuthClient,
    HttpGitHubUserProfile,
)
from app.modules.auth.infrastructure.jwt_tokens import (
    Rs256AccessTokenIssuer,
    Rs256AccessTokenVerifier,
)


class FakeOAuth:
    def __init__(self, *, invalid: bool = False) -> None:
        self.invalid = invalid
        self.codes: list[str] = []

    async def exchange_code(self, code: str) -> str:
        self.codes.append(code)
        if self.invalid:
            raise InvalidGitHubCode
        return "github-user-token"


class FakeProfile:
    def __init__(self) -> None:
        self.tokens: list[str] = []

    async def get_user(self, token: str) -> AuthenticatedUser:
        self.tokens.append(token)
        return AuthenticatedUser(42, "octocat", "Octo Cat", "https://github.test/avatar.png")


class FakeLinker:
    def __init__(self, workspace_ids: tuple[UUID, ...]) -> None:
        self.workspace_ids = workspace_ids
        self.tokens: list[str] = []
        self.expected_user_ids: list[int | None] = []

    async def execute(self, token: str, *, expected_user_id: int | None = None) -> tuple[UUID, ...]:
        self.tokens.append(token)
        self.expected_user_ids.append(expected_user_id)
        return self.workspace_ids


class FakeSessions:
    def __init__(self) -> None:
        self.saved: list[tuple[AuthenticatedUser, str, UUID, datetime]] = []
        self.committed = False

    async def create(
        self, user: AuthenticatedUser, token_hash: str, family_id: UUID, expires_at: datetime
    ) -> None:
        self.saved.append((user, token_hash, family_id, expires_at))


class FakeUow:
    def __init__(self, sessions: FakeSessions) -> None:
        self.sessions = sessions

    async def __aenter__(self) -> FakeUow:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None

    async def commit(self) -> None:
        self.sessions.committed = True

    async def rollback(self) -> None:
        return None


def _keys() -> tuple[str, str]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private = key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()).decode()
    public = key.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo).decode()
    return private, public


def test_callback_use_case_links_before_committing_hashed_refresh_session() -> None:
    workspace = UUID("00000000-0000-0000-0000-000000000011")
    private, public = _keys()
    now = datetime(2026, 9, 28, 10, 0, tzinfo=UTC)
    oauth, profile, linker, sessions = (
        FakeOAuth(),
        FakeProfile(),
        FakeLinker((workspace,)),
        FakeSessions(),
    )
    use_case = ExchangeGitHubCode(
        oauth=oauth,
        profile=profile,
        linker=linker,
        uow_factory=lambda: FakeUow(sessions),
        issuer=Rs256AccessTokenIssuer(
            private, issuer="dmc-268-api", audience="dmc-268-ui", now=lambda: now
        ),
        now=lambda: now,
        new_refresh_token=lambda: "opaque-refresh-secret",
        new_family_id=lambda: UUID("00000000-0000-0000-0000-000000000099"),
    )

    result = asyncio.run(use_case.execute("one-time-code"))

    assert oauth.codes == ["one-time-code"]
    assert profile.tokens == linker.tokens == ["github-user-token"]
    assert linker.expected_user_ids == [42]
    assert sessions.committed
    assert sessions.saved == [
        (
            result.user,
            hashlib.sha256(b"opaque-refresh-secret").hexdigest(),
            UUID("00000000-0000-0000-0000-000000000099"),
            now + timedelta(days=30),
        )
    ]
    assert result.refresh_token == "opaque-refresh-secret"
    claims = jwt.decode(
        result.access_token,
        public,
        algorithms=["RS256"],
        issuer="dmc-268-api",
        audience="dmc-268-ui",
        options={"verify_exp": False},
    )
    assert claims == {
        "sub": "42",
        "github_user_id": 42,
        "workspaces": [str(workspace)],
        "iss": "dmc-268-api",
        "aud": "dmc-268-ui",
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=15)).timestamp()),
    }


def test_invalid_github_code_never_creates_a_session() -> None:
    private, _ = _keys()
    sessions = FakeSessions()
    use_case = ExchangeGitHubCode(
        oauth=FakeOAuth(invalid=True),
        profile=FakeProfile(),
        linker=FakeLinker(()),
        uow_factory=lambda: FakeUow(sessions),
        issuer=Rs256AccessTokenIssuer(private, issuer="dmc-268-api", audience="dmc-268-ui"),
    )
    with pytest.raises(InvalidGitHubCode):
        asyncio.run(use_case.execute("expired"))
    assert sessions.saved == [] and not sessions.committed


def test_callback_http_contract_sets_refresh_cookie_and_accepts_zero_installations() -> None:
    from app.bootstrap.auth_api import get_exchange_github_code

    private, public = _keys()
    now = datetime.now(UTC)
    sessions = FakeSessions()
    use_case = ExchangeGitHubCode(
        oauth=FakeOAuth(),
        profile=FakeProfile(),
        linker=FakeLinker(()),
        uow_factory=lambda: FakeUow(sessions),
        issuer=Rs256AccessTokenIssuer(
            private, issuer="dmc-268-api", audience="dmc-268-ui", now=lambda: now
        ),
        now=lambda: now,
        new_refresh_token=lambda: "opaque-refresh-secret",
        new_family_id=uuid4,
    )
    app.dependency_overrides[get_exchange_github_code] = lambda: use_case
    try:
        response = TestClient(app).post("/api/auth/github/callback", json={"code": "valid"})
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 200
    assert set(response.json()) == {"accessToken", "tokenType", "expiresIn", "user"}
    assert response.json()["tokenType"] == "Bearer"
    assert response.json()["expiresIn"] == 900
    assert response.json()["user"] == {
        "id": 42,
        "login": "octocat",
        "name": "Octo Cat",
        "avatarUrl": "https://github.test/avatar.png",
    }
    cookie = response.headers["set-cookie"]
    for fragment in (
        "refresh_token=opaque-refresh-secret",
        "Max-Age=2592000",
        "Path=/api/auth",
        "HttpOnly",
        "Secure",
        "SameSite=strict",
    ):
        assert fragment in cookie
    claims = jwt.decode(
        response.json()["accessToken"],
        public,
        algorithms=["RS256"],
        issuer="dmc-268-api",
        audience="dmc-268-ui",
    )
    assert claims["workspaces"] == []


def test_github_oauth_adapter_posts_credentials_off_url_and_reads_profile() -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/login/oauth/access_token":
            return httpx.Response(
                200, json={"access_token": "ghu_transient", "token_type": "bearer"}
            )
        assert request.url.path == "/user"
        return httpx.Response(
            200,
            json={
                "id": 42,
                "login": "octocat",
                "name": None,
                "avatar_url": "https://github.test/avatar.png",
            },
        )

    async def call() -> AuthenticatedUser:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(respond), base_url="https://github.com"
        ) as client:
            token = await HttpGitHubOAuthClient(
                client, client_id="client-id", client_secret="client-secret"
            ).exchange_code("one-time")
            assert token == "ghu_transient"
            return await HttpGitHubUserProfile(client).get_user(token)

    assert asyncio.run(call()) == AuthenticatedUser(
        42, "octocat", None, "https://github.test/avatar.png"
    )
    assert requests[0].method == "POST"
    assert requests[0].headers["Accept"] == "application/json"
    assert requests[0].url.query == b""
    assert json.loads(requests[0].content) == {
        "client_id": "client-id",
        "client_secret": "client-secret",
        "code": "one-time",
    }
    assert requests[1].headers["Authorization"] == "Bearer ghu_transient"


def test_github_rejects_used_code_without_profile_or_local_session() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"error": "bad_verification_code"})

    async def call() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(respond), base_url="https://github.com"
        ) as client:
            with pytest.raises(InvalidGitHubCode):
                await HttpGitHubOAuthClient(
                    client, client_id="client-id", client_secret="client-secret"
                ).exchange_code("used")

    asyncio.run(call())


def test_github_rejects_bad_client_secret_as_provider_failure() -> None:
    async def call() -> None:
        transport = httpx.MockTransport(
            lambda request: httpx.Response(200, json={"error": "incorrect_client_credentials"})
        )
        async with httpx.AsyncClient(transport=transport, base_url="https://github.com") as client:
            with pytest.raises(GitHubProviderUnavailable):
                await HttpGitHubOAuthClient(
                    client, client_id="client-id", client_secret="bad-secret"
                ).exchange_code("valid")

    asyncio.run(call())


def test_github_network_failure_is_a_provider_failure() -> None:
    def unavailable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("unavailable")

    async def call() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(unavailable), base_url="https://github.com"
        ) as client:
            with pytest.raises(GitHubProviderUnavailable):
                await HttpGitHubOAuthClient(
                    client, client_id="client-id", client_secret="client-secret"
                ).exchange_code("one-time")

    asyncio.run(call())


@pytest.mark.parametrize(
    ("status", "error", "expected"),
    [
        (400, "bad_verification_code", InvalidGitHubCode),
        (400, "redirect_uri_mismatch", GitHubProviderUnavailable),
        (400, "unverified_user_email", GitHubProviderUnavailable),
        (400, "incorrect_client_credentials", GitHubProviderUnavailable),
        (400, "new_error", GitHubProviderUnavailable),
        (400, None, GitHubProviderUnavailable),
        (200, "new_error", GitHubProviderUnavailable),
    ],
)
def test_oauth_error_response_classification(
    status: int, error: str | None, expected: type[Exception]
) -> None:
    payload = {"error": error} if error is not None else {"message": "unknown"}

    async def call() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(status, json=payload)),
            base_url="https://github.com",
        ) as client:
            with pytest.raises(expected):
                await HttpGitHubOAuthClient(
                    client, client_id="client-id", client_secret="client-secret"
                ).exchange_code("one-time")

    asyncio.run(call())


def test_callback_http_rejects_invalid_code_without_cookie() -> None:
    from app.bootstrap.auth_api import get_exchange_github_code

    private, _ = _keys()
    sessions = FakeSessions()
    use_case = ExchangeGitHubCode(
        oauth=FakeOAuth(invalid=True),
        profile=FakeProfile(),
        linker=FakeLinker(()),
        uow_factory=lambda: FakeUow(sessions),
        issuer=Rs256AccessTokenIssuer(private, issuer="dmc-268-api", audience="dmc-268-ui"),
    )
    app.dependency_overrides[get_exchange_github_code] = lambda: use_case
    try:
        response = TestClient(app).post("/api/auth/github/callback", json={"code": "used"})
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 400
    assert response.json() == {"detail": "invalid GitHub authorization code"}
    assert "set-cookie" not in response.headers
    assert not sessions.saved


def test_callback_http_provider_outage_does_not_issue_cookie_or_session() -> None:
    from app.bootstrap.auth_api import get_exchange_github_code

    class UnavailableOAuth(FakeOAuth):
        async def exchange_code(self, code: str) -> str:
            raise GitHubProviderUnavailable

    private, _ = _keys()
    sessions = FakeSessions()
    use_case = ExchangeGitHubCode(
        oauth=UnavailableOAuth(),
        profile=FakeProfile(),
        linker=FakeLinker(()),
        uow_factory=lambda: FakeUow(sessions),
        issuer=Rs256AccessTokenIssuer(private, issuer="dmc-268-api", audience="dmc-268-ui"),
    )
    app.dependency_overrides[get_exchange_github_code] = lambda: use_case
    try:
        response = TestClient(app).post("/api/auth/github/callback", json={"code": "valid"})
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 502
    assert response.json() == {"detail": "GitHub authentication is unavailable"}
    assert "set-cookie" not in response.headers
    assert not sessions.saved


def test_access_token_verifier_accepts_empty_workspaces_and_rejects_missing_claim() -> None:
    private, public = _keys()
    token = Rs256AccessTokenIssuer(private, issuer="dmc-268-api", audience="dmc-268-ui").issue(
        42, ()
    )
    verifier = Rs256AccessTokenVerifier(public, issuer="dmc-268-api", audience="dmc-268-ui")
    assert verifier.verify(token) == (42, ())
    claims = jwt.decode(
        token, public, algorithms=["RS256"], issuer="dmc-268-api", audience="dmc-268-ui"
    )
    del claims["workspaces"]
    with pytest.raises(jwt.InvalidTokenError):
        verifier.verify(jwt.encode(claims, private, algorithm="RS256"))
