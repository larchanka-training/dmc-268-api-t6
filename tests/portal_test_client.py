"""A signed portal client for existing transport-contract tests."""

from __future__ import annotations

import os
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)
from fastapi import FastAPI
from fastapi.testclient import TestClient

_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_PRIVATE = _KEY.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
_PUBLIC = _KEY.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)


def authenticated_test_client(
    app: FastAPI,
    *,
    user_id: int = 42,
    workspace_ids: Sequence[str | UUID] | None = None,
    **kwargs: Any,
) -> TestClient:
    os.environ["AUTH_JWT_PUBLIC_KEY"] = _PUBLIC.decode()
    os.environ["AUTH_JWT_ISSUER"] = "portal-test"
    os.environ["AUTH_JWT_AUDIENCE"] = "portal-test-ui"
    ws = [str(w) for w in workspace_ids] if workspace_ids is not None else []
    token = jwt.encode(
        {
            "sub": str(user_id),
            "github_user_id": user_id,
            "workspaces": ws,
            "iss": "portal-test",
            "aud": "portal-test-ui",
            "exp": int((datetime.now(UTC) + timedelta(minutes=15)).timestamp()),
        },
        _PRIVATE,
        algorithm="RS256",
    )
    headers = kwargs.pop("headers", {})
    client_headers = {"Authorization": f"Bearer {token}", **headers}
    return TestClient(app, headers=client_headers, **kwargs)
