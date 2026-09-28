"""A signed portal client for existing transport-contract tests."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import Any

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


def authenticated_test_client(app: FastAPI, **kwargs: Any) -> TestClient:
    os.environ["AUTH_JWT_PUBLIC_KEY"] = _PUBLIC.decode()
    os.environ["AUTH_JWT_ISSUER"] = "portal-test"
    os.environ["AUTH_JWT_AUDIENCE"] = "portal-test-ui"
    token = jwt.encode(
        {
            "sub": "42",
            "github_user_id": 42,
            "workspaces": [],
            "iss": "portal-test",
            "aud": "portal-test-ui",
            "exp": int((datetime.now(UTC) + timedelta(minutes=15)).timestamp()),
        },
        _PRIVATE,
        algorithm="RS256",
    )
    return TestClient(app, headers={"Authorization": f"Bearer {token}"}, **kwargs)
