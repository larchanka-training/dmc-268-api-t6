"""Refresh rotation, family replay revocation and idempotent logout."""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import Self
from uuid import UUID, uuid4

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
    ExchangedSession,
)
from app.modules.auth.application.refresh_session import (
    InvalidRefreshToken,
    LogoutLocalSession,
    RefreshLocalSession,
    RefreshSessionRecord,
    RefreshTokenFamily,
)
from app.modules.auth.application.refresh_token_hash import hash_refresh_token
from app.modules.auth.infrastructure.jwt_tokens import Rs256AccessTokenIssuer


@pytest.mark.parametrize(
    ("token", "expected"),
    [
        ("abc", "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"),
        ("é", "4a99557e4033c3539de2eb65472017cad5f9557f7a0625a09f1c3f6e2ba69c4c"),
    ],
)
def test_refresh_token_hash_matches_sha256_vectors(token: str, expected: str) -> None:
    assert hash_refresh_token(token) == expected


@dataclass
class FakeSession:
    id: UUID
    family_id: UUID
    user_id: int
    token_hash: str
    expires_at: datetime
    created_at: datetime
    rotated_at: datetime | None = None
    revoked_at: datetime | None = None


@dataclass
class FakeFamily:
    id: UUID
    expires_at: datetime
    revoked_at: datetime | None = None


class FakeRefreshDatabase:
    def __init__(self, now: datetime) -> None:
        self.now = now
        self.family = FakeFamily(uuid4(), now + timedelta(days=30))
        self.lock = asyncio.Lock()
        self.sessions: dict[str, FakeSession] = {}
        self.user = AuthenticatedUser(42, "octocat", "Octo Cat", None)
        self.workspace_ids: tuple[UUID, ...] = ()
        self.commits = 0
        self.revoke_calls = 0

    def seed(self, token: str, *, expires_at: datetime | None = None) -> None:
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        self.sessions[token_hash] = FakeSession(
            uuid4(),
            self.family.id,
            42,
            token_hash,
            expires_at or self.family.expires_at,
            self.now,
        )

    def uow(self) -> FakeRefreshUow:
        return FakeRefreshUow(self)


class FakeRefreshUow:
    def __init__(self, database: FakeRefreshDatabase) -> None:
        self._database = database
        self._locked = False

    @property
    def sessions(self) -> FakeRefreshUow:
        return self

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._locked:
            self._database.lock.release()

    async def commit(self) -> None:
        self._database.commits += 1

    async def rollback(self) -> None:
        pass

    async def find_family_id(self, token_hash: str) -> UUID | None:
        session = self._database.sessions.get(token_hash)
        return session.family_id if session else None

    async def lock_family(self, family_id: UUID) -> RefreshTokenFamily | None:
        await self._database.lock.acquire()
        self._locked = True
        family = self._database.family
        if family.id != family_id:
            return None
        return RefreshTokenFamily(family.id, family.expires_at, family.revoked_at)

    async def get_session(self, token_hash: str) -> RefreshSessionRecord | None:
        session = self._database.sessions.get(token_hash)
        if session is None:
            return None
        return RefreshSessionRecord(
            session.id,
            session.family_id,
            session.user_id,
            session.expires_at,
            session.rotated_at,
            session.revoked_at,
        )

    async def current_identity(self, user_id: int) -> tuple[AuthenticatedUser, tuple[UUID, ...]]:
        assert user_id == self._database.user.id
        return self._database.user, self._database.workspace_ids

    async def count_family_sessions(
        self,
        family_id: UUID,
        *,
        since: datetime | None = None,
        exclude_session_id: UUID | None = None,
    ) -> int:
        return sum(
            1
            for session in self._database.sessions.values()
            if session.family_id == family_id
            and session.revoked_at is None
            and (since is None or session.created_at >= since)
            and (exclude_session_id is None or session.id != exclude_session_id)
        )

    async def rotate(
        self,
        session_id: UUID,
        *,
        new_token_hash: str,
        at: datetime,
        expires_at: datetime,
    ) -> None:
        prior = next(item for item in self._database.sessions.values() if item.id == session_id)
        prior.rotated_at = at
        self._database.family.expires_at = expires_at
        self._database.sessions[new_token_hash] = FakeSession(
            uuid4(), prior.family_id, prior.user_id, new_token_hash, expires_at, at
        )

    async def add_session(
        self,
        family_id: UUID,
        user_id: int,
        token_hash: str,
        expires_at: datetime,
        *,
        created_at: datetime | None = None,
    ) -> None:
        self._database.family.expires_at = expires_at
        self._database.sessions[token_hash] = FakeSession(
            uuid4(),
            family_id,
            user_id,
            token_hash,
            expires_at,
            created_at or self._database.now,
        )

    async def revoke_family(self, family_id: UUID, at: datetime) -> None:
        assert self._locked and self._database.family.id == family_id
        self._database.family.revoked_at = at
        self._database.revoke_calls += 1
        for session in self._database.sessions.values():
            if session.family_id == family_id:
                session.revoked_at = at


def _keys() -> tuple[str, str]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return (
        key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()).decode(),
        key.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo).decode(),
    )


def _use_cases(
    database: FakeRefreshDatabase,
    private_key: str,
    new_token: str = "replacement-secret",
) -> tuple[RefreshLocalSession, LogoutLocalSession]:
    refresh = RefreshLocalSession(
        uow_factory=database.uow,
        issuer=Rs256AccessTokenIssuer(
            private_key, issuer="dmc-268-api", audience="dmc-268-ui", now=lambda: database.now
        ),
        now=lambda: database.now,
        new_refresh_token=lambda: new_token,
    )
    return refresh, LogoutLocalSession(uow_factory=database.uow, now=lambda: database.now)


def test_refresh_rotates_once_and_uses_current_workspace_memberships() -> None:
    now = datetime(2026, 9, 28, 12, tzinfo=UTC)
    database = FakeRefreshDatabase(now)
    database.seed("original-secret")
    database.workspace_ids = (UUID("00000000-0000-0000-0000-000000000077"),)
    private, public = _keys()
    refresh, _ = _use_cases(database, private)

    result = asyncio.run(refresh.execute("original-secret"))

    original_hash = "90acf9a86740a5b3ff03c71cdbbbae591773691219be3165497378cdc1a14ed4"
    replacement_hash = "d0f82d558a622b0eb49f832c76ba3ad848b4f3d585eebc46b091e94c980125bf"
    assert database.sessions[original_hash].rotated_at == now
    assert database.sessions[replacement_hash].expires_at == database.family.expires_at
    assert result.refresh_token == "replacement-secret"
    assert database.commits == 1
    assert result.user == database.user
    claims = jwt.decode(
        result.access_token,
        public,
        algorithms=["RS256"],
        issuer="dmc-268-api",
        audience="dmc-268-ui",
        options={"verify_exp": False},
    )
    assert claims["workspaces"] == ["00000000-0000-0000-0000-000000000077"]

    # Replay after the grace period expires revokes the entire family
    database.now = now + timedelta(seconds=16)
    with pytest.raises(InvalidRefreshToken):
        asyncio.run(refresh.execute("original-secret"))
    assert database.family.revoked_at == database.now
    with pytest.raises(InvalidRefreshToken):
        asyncio.run(refresh.execute("replacement-secret"))


def test_refresh_renews_thirty_day_family_expiry_near_boundary() -> None:
    now = datetime(2026, 9, 28, 12, tzinfo=UTC)
    database = FakeRefreshDatabase(now)
    database.family.expires_at = now + timedelta(seconds=1)
    database.seed("near-expiry")
    private, _ = _keys()
    refresh, _ = _use_cases(database, private)

    result = asyncio.run(refresh.execute("near-expiry"))

    replacement_hash = hashlib.sha256(result.refresh_token.encode()).hexdigest()
    expected_expiry = now + timedelta(days=30)
    assert database.family.expires_at == expected_expiry
    assert database.sessions[replacement_hash].expires_at == expected_expiry
    database.now = now + timedelta(days=29)
    refresh_again, _ = _use_cases(database, private, new_token="second-replacement")
    assert asyncio.run(refresh_again.execute(result.refresh_token)).refresh_token == (
        "second-replacement"
    )


def test_missing_unknown_expired_and_revoked_refresh_are_rejected() -> None:
    now = datetime(2026, 9, 28, 12, tzinfo=UTC)
    database = FakeRefreshDatabase(now)
    database.seed("expired-secret", expires_at=now - timedelta(seconds=1))
    private, _ = _keys()
    refresh, _ = _use_cases(database, private)
    for token in ("", "unknown-secret", "expired-secret"):
        with pytest.raises(InvalidRefreshToken):
            asyncio.run(refresh.execute(token))
    assert database.commits == 0

    database.sessions[hashlib.sha256(b"expired-secret").hexdigest()].expires_at = now + timedelta(
        days=1
    )
    database.family.revoked_at = now
    with pytest.raises(InvalidRefreshToken):
        asyncio.run(refresh.execute("expired-secret"))


def test_concurrent_same_token_refresh_within_grace_period_does_not_revoke() -> None:
    now = datetime(2026, 9, 28, 12, tzinfo=UTC)
    database = FakeRefreshDatabase(now)
    database.seed("original-secret")
    private, _ = _keys()
    refresh, _ = _use_cases(database, private)

    async def race() -> list[object]:
        return list(
            await asyncio.gather(
                refresh.execute("original-secret"),
                refresh.execute("original-secret"),
                return_exceptions=True,
            )
        )

    outcomes = asyncio.run(race())
    assert all(isinstance(item, ExchangedSession) for item in outcomes)
    assert database.family.revoked_at is None

    # True replay after the 15-second grace period revokes the whole family
    database.now = now + timedelta(seconds=16)
    with pytest.raises(InvalidRefreshToken):
        asyncio.run(refresh.execute("original-secret"))
    assert database.family.revoked_at == database.now


def test_refresh_grace_window_enforces_max_session_limit() -> None:
    now = datetime(2026, 9, 28, 12, tzinfo=UTC)
    database = FakeRefreshDatabase(now)
    database.seed("original-secret")
    private, _ = _keys()
    counter = 0

    def next_token() -> str:
        nonlocal counter
        counter += 1
        return f"token-{counter}"

    refresh = RefreshLocalSession(
        uow_factory=database.uow,
        issuer=Rs256AccessTokenIssuer(
            private, issuer="dmc-268-api", audience="dmc-268-ui", now=lambda: database.now
        ),
        now=lambda: database.now,
        new_refresh_token=next_token,
    )

    # Initial rotation creates token-1 (count of reissues since rotation = 1)
    asyncio.run(refresh.execute("original-secret"))
    assert database.commits == 1

    # Grace period re-refreshes:
    # 2nd call -> count=1 < 5 -> creates token-2
    asyncio.run(refresh.execute("original-secret"))
    # 3rd call -> count=2 < 5 -> creates token-3
    asyncio.run(refresh.execute("original-secret"))
    # 4th call -> count=3 < 5 -> creates token-4
    asyncio.run(refresh.execute("original-secret"))
    # 5th call -> count=4 < 5 -> creates token-5
    asyncio.run(refresh.execute("original-secret"))
    assert database.commits == 5

    # 6th call: count is 5 >= MAX_GRACE_REFRESH_SESSIONS (5) ->
    # revokes family, commits the revocation, and raises InvalidRefreshToken
    with pytest.raises(InvalidRefreshToken):
        asyncio.run(refresh.execute("original-secret"))
    assert database.family.revoked_at == database.now
    assert database.commits == 6


def test_sequential_rotations_do_not_exhaust_grace_session_limit() -> None:
    now = datetime(2026, 9, 28, 12, tzinfo=UTC)
    database = FakeRefreshDatabase(now)
    database.seed("token-0")
    private, _ = _keys()
    counter = 0

    def next_token() -> str:
        nonlocal counter
        counter += 1
        return f"token-{counter}"

    refresh = RefreshLocalSession(
        uow_factory=database.uow,
        issuer=Rs256AccessTokenIssuer(
            private, issuer="dmc-268-api", audience="dmc-268-ui", now=lambda: database.now
        ),
        now=lambda: database.now,
        new_refresh_token=next_token,
    )

    # 4 sequential rotations spaced 15 minutes apart:
    # token-0 -> token-1 -> token-2 -> token-3 -> token-4
    for i in range(4):
        database.now += timedelta(minutes=15)
        asyncio.run(refresh.execute(f"token-{i}"))

    # Now token-3 was rotated at t3 (last rotation).
    # Replay token-3 within the 15-second grace window (e.g. at t3 + 5s).
    # Even though the family has 5 total sessions (token-0, 1, 2, 3, 4),
    # only token-4 was created since token-3's rotation.
    # Therefore, grace count is 1 < 5, and replay MUST succeed!
    database.now += timedelta(seconds=5)
    result = asyncio.run(refresh.execute("token-3"))
    assert result.user.id == 42
    assert database.family.revoked_at is None


def test_refresh_grace_window_exact_boundaries() -> None:
    now = datetime(2026, 9, 28, 12, tzinfo=UTC)
    database = FakeRefreshDatabase(now)
    database.seed("original-secret")
    private, _ = _keys()
    counter = 0

    def next_token() -> str:
        nonlocal counter
        counter += 1
        return f"token-{counter}"

    refresh = RefreshLocalSession(
        uow_factory=database.uow,
        issuer=Rs256AccessTokenIssuer(
            private, issuer="dmc-268-api", audience="dmc-268-ui", now=lambda: database.now
        ),
        now=lambda: database.now,
        new_refresh_token=next_token,
    )

    # Initial rotation at t0
    asyncio.run(refresh.execute("original-secret"))

    # Replay at +14 seconds (inside grace window) -> succeeds
    database.now = now + timedelta(seconds=14)
    res_14 = asyncio.run(refresh.execute("original-secret"))
    assert res_14.user.id == 42

    # Replay at +15 seconds (exact boundary: delta <= grace_period) -> succeeds
    database.now = now + timedelta(seconds=15)
    res_15 = asyncio.run(refresh.execute("original-secret"))
    assert res_15.user.id == 42

    # Replay at +16 seconds (outside grace window) -> revokes family and raises InvalidRefreshToken
    database.now = now + timedelta(seconds=16)
    with pytest.raises(InvalidRefreshToken):
        asyncio.run(refresh.execute("original-secret"))
    assert database.family.revoked_at == database.now


def test_replay_racing_current_token_revokes_whole_family() -> None:
    now = datetime(2026, 9, 28, 12, tzinfo=UTC)
    database = FakeRefreshDatabase(now)
    database.seed("old-secret")
    database.seed("current-secret")
    # Old token rotated outside the grace period (e.g. 20s ago)
    database.sessions[hashlib.sha256(b"old-secret").hexdigest()].rotated_at = now - timedelta(
        seconds=20
    )
    private, _ = _keys()
    refresh, _ = _use_cases(database, private, new_token="next-secret")

    async def race() -> None:
        await asyncio.gather(
            refresh.execute("old-secret"),
            refresh.execute("current-secret"),
            return_exceptions=True,
        )

    asyncio.run(race())
    assert database.family.revoked_at == now
    with pytest.raises(InvalidRefreshToken):
        asyncio.run(refresh.execute("next-secret"))


def test_logout_revokes_known_family_and_is_idempotent() -> None:
    now = datetime(2026, 9, 28, 12, tzinfo=UTC)
    database = FakeRefreshDatabase(now)
    database.seed("current-secret")
    private, _ = _keys()
    _, logout = _use_cases(database, private)

    asyncio.run(logout.execute(None))
    asyncio.run(logout.execute("unknown-secret"))
    assert database.family.revoked_at is None
    asyncio.run(logout.execute("current-secret"))
    asyncio.run(logout.execute("current-secret"))
    assert database.family.revoked_at == now
    assert database.revoke_calls == 1


def test_refresh_and_logout_http_cookie_contract() -> None:
    from app.bootstrap.auth_api import get_logout_local_session, get_refresh_local_session

    now = datetime.now(UTC)
    database = FakeRefreshDatabase(now)
    database.seed("original-secret")
    private, _ = _keys()
    refresh, logout = _use_cases(database, private)
    app.dependency_overrides[get_refresh_local_session] = lambda: refresh
    app.dependency_overrides[get_logout_local_session] = lambda: logout
    try:
        client = TestClient(app, base_url="https://testserver")
        missing = client.post("/api/auth/refresh")
        rotated = client.post(
            "/api/auth/refresh", headers={"Cookie": "refresh_token=original-secret"}
        )
        client.cookies.clear()
        database.now = now + timedelta(seconds=16)
        replay = client.post(
            "/api/auth/refresh", headers={"Cookie": "refresh_token=original-secret"}
        )
        client.cookies.clear()
        revoked = client.post(
            "/api/auth/refresh", headers={"Cookie": "refresh_token=replacement-secret"}
        )
        signed_out = client.post(
            "/api/auth/logout", headers={"Cookie": "refresh_token=replacement-secret"}
        )
        no_cookie = client.post("/api/auth/logout")
    finally:
        app.dependency_overrides.clear()
    assert missing.status_code == replay.status_code == revoked.status_code == 401
    assert rotated.status_code == 200
    assert set(rotated.json()) == {"accessToken", "tokenType", "expiresIn", "user"}
    assert rotated.json()["tokenType"] == "Bearer" and rotated.json()["expiresIn"] == 900
    for fragment in (
        "refresh_token=replacement-secret",
        "Max-Age=2592000",
        "Path=/api/auth",
        "HttpOnly",
        "Secure",
        "SameSite=strict",
    ):
        assert fragment in rotated.headers["set-cookie"]
    assert signed_out.status_code == no_cookie.status_code == 204
    assert signed_out.content == no_cookie.content == b""
    assert "Max-Age=0" in signed_out.headers["set-cookie"]
    assert "Path=/api/auth" in no_cookie.headers["set-cookie"]
