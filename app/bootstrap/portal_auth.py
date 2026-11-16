"""Verify bearer tokens at the portal transport boundary."""

from __future__ import annotations

import os

import jwt
from fastapi import HTTPException, Request

from app.modules.auth.application.agent_tokens import AgentTokens
from app.modules.auth.application.scope import AuthScope
from app.modules.auth.infrastructure.agent_tokens import SqlAlchemyAgentTokenUnitOfWork
from app.modules.auth.infrastructure.jwt_tokens import Rs256AccessTokenVerifier

_AGENT_OPERATIONS = frozenset(
    {
        ("GET", "/api/repos"),
        ("GET", "/api/repos/{repo_id}"),
        ("PATCH", "/api/repos/{repo_id}"),
        ("GET", "/api/repos/{repo_id}/pulls"),
        ("GET", "/api/repos/{repo_id}/rules-status"),
        ("GET", "/api/runs"),
        ("GET", "/api/runs/{run_id}"),
        ("GET", "/api/runs/{run_id}/comments"),
        ("GET", "/api/runs/{run_id}/actions"),
    }
)


def _agent_tokens_for_request(request: Request) -> AgentTokens:
    return get_agent_tokens(request)


def get_agent_tokens(request: Request) -> AgentTokens:
    resources = getattr(request.app.state, "reviews_api_resources", None)
    if resources is None:
        raise HTTPException(status_code=503, detail="agent authentication is not configured")
    return AgentTokens(lambda: SqlAlchemyAgentTokenUnitOfWork(resources.session_factory))


async def get_auth_scope(request: Request) -> AuthScope:
    authorization = request.headers.get("Authorization", "")
    scheme, separator, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not separator or not token or " " in token:
        raise HTTPException(status_code=401, detail="Bearer access token required")
    if token.startswith("acr_"):
        scope = await _agent_tokens_for_request(request).resolve(token)
        if scope is None:
            raise HTTPException(status_code=401, detail="invalid agent token")
        route = request.scope.get("route")
        if (request.method, getattr(route, "path", None)) not in _AGENT_OPERATIONS:
            raise HTTPException(status_code=403, detail="agent token cannot use this operation")
        return scope
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
