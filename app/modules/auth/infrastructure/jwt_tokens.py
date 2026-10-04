"""RS256 access JWT signing and strict verification."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import jwt

from app.modules.auth.application.scope import AuthScope


class Rs256AccessTokenIssuer:
    def __init__(
        self,
        private_key: str,
        *,
        issuer: str,
        audience: str,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if not private_key or not issuer or not audience:
            raise ValueError("JWT signing settings are required")
        self._private_key = private_key
        self._issuer = issuer
        self._audience = audience
        self._now = now

    def issue(self, user_id: int, workspace_ids: tuple[UUID, ...]) -> str:
        now = self._now()
        claims: dict[str, Any] = {
            "sub": str(user_id),
            "github_user_id": user_id,
            "workspaces": [str(workspace_id) for workspace_id in workspace_ids],
            "iss": self._issuer,
            "aud": self._audience,
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(minutes=15)).timestamp()),
        }
        return jwt.encode(claims, self._private_key, algorithm="RS256")


class Rs256AccessTokenVerifier:
    def __init__(self, public_key: str, *, issuer: str, audience: str) -> None:
        if not public_key or not issuer or not audience:
            raise ValueError("JWT verification settings are required")
        self._public_key = public_key
        self._issuer = issuer
        self._audience = audience

    def verify_token(self, token: str) -> AuthScope:
        claims = jwt.decode(
            token,
            self._public_key,
            algorithms=["RS256"],
            issuer=self._issuer,
            audience=self._audience,
            options={"require": ["exp", "iss", "aud", "sub", "workspaces"]},
        )
        subject = claims["sub"]
        workspaces = claims["workspaces"]
        if type(claims["exp"]) is not int:
            raise jwt.InvalidTokenError("invalid access token expiry")
        if not isinstance(subject, str) or not subject.isdecimal() or int(subject) <= 0:
            raise jwt.InvalidTokenError("invalid GitHub user subject")
        if type(claims.get("github_user_id")) is not int or claims["github_user_id"] != int(
            subject
        ):
            raise jwt.InvalidTokenError("GitHub user identity mismatch")
        if not isinstance(workspaces, list) or any(
            not isinstance(item, str) for item in workspaces
        ):
            raise jwt.InvalidTokenError("invalid Workspace claim")
        try:
            return AuthScope(
                int(subject),
                tuple(UUID(item) for item in workspaces),
                expires_at=claims["exp"],
            )
        except ValueError as exc:
            raise jwt.InvalidTokenError("invalid Workspace ID") from exc

    def verify(self, token: str) -> tuple[int, tuple[UUID, ...]]:
        scope = self.verify_token(token)
        return scope.user_id, scope.workspace_ids
