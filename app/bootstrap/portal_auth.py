"""Verify bearer tokens at the portal transport boundary."""

from __future__ import annotations

import os

import jwt
from fastapi import HTTPException, Request

from app.modules.auth.application.scope import AuthScope
from app.modules.auth.infrastructure.jwt_tokens import Rs256AccessTokenVerifier


def get_auth_scope(request: Request) -> AuthScope:
    authorization = request.headers.get("Authorization", "")
    scheme, separator, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not separator or not token or " " in token:
        raise HTTPException(status_code=401, detail="Bearer access token required")
    public_key = os.environ.get("AUTH_JWT_PUBLIC_KEY")
    issuer = os.environ.get("AUTH_JWT_ISSUER")
    audience = os.environ.get("AUTH_JWT_AUDIENCE")
    if not public_key or not issuer or not audience:
        raise HTTPException(status_code=503, detail="access token verification is not configured")
    try:
        return Rs256AccessTokenVerifier(public_key, issuer=issuer, audience=audience).verify_token(
            token
        )
    except jwt.InvalidTokenError as exc:
        raise HTTPException(status_code=401, detail="invalid access token") from exc
