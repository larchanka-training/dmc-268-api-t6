"""The served OpenAPI document describes authentication and actual HTTP responses."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.routing import iter_route_contexts
from fastapi.testclient import TestClient

from app.main import app

PUBLIC_AUTH = {
    ("post", "/api/auth/github/callback"),
    ("post", "/api/auth/refresh"),
    ("post", "/api/auth/logout"),
}


@pytest.fixture
def document(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    # Exercise generation even when another test has populated FastAPI's schema cache.
    monkeypatch.setattr(app, "openapi_schema", None)
    response = TestClient(app).get("/openapi.json")
    assert response.status_code == 200
    result: dict[str, Any] = response.json()
    return result


def _protected_operations() -> list[tuple[str, str]]:
    return [
        (method.lower(), route.path_format)
        for route in iter_route_contexts(app.routes)
        if route.path_format is not None
        and route.path_format.startswith("/api/")
        and route.include_in_schema
        for method in sorted(route.methods or ())
        if (method.lower(), route.path_format) not in PUBLIC_AUTH
    ]


def test_runtime_openapi_product_title(document: dict[str, Any]) -> None:
    assert document["info"]["title"] == "AI Code Reviewer browser API"


def test_runtime_openapi_bearer_scheme(document: dict[str, Any]) -> None:
    bearer = document["components"]["securitySchemes"]["bearerAuth"]
    assert bearer["type"] == "http"
    assert bearer["scheme"] == "bearer"
    assert bearer["bearerFormat"] == "JWT"


@pytest.mark.parametrize(("method", "path"), _protected_operations())
def test_runtime_openapi_protected_operations_declare_bearer_and_401(
    document: dict[str, Any], method: str, path: str
) -> None:
    operation = document["paths"][path][method]
    assert operation["security"] == [{"bearerAuth": []}]
    assert {"401", "503"} <= operation["responses"].keys()


@pytest.mark.parametrize(
    ("path", "required_statuses"),
    [
        ("/webhooks/github", {"202", "400", "401", "413", "503"}),
        ("/api/auth/logout", {"204", "503"}),
        ("/api/auth/refresh", {"200", "401", "503"}),
        ("/api/auth/github/callback", {"200", "400", "502", "503"}),
    ],
)
def test_runtime_openapi_webhook_and_auth_responses(
    document: dict[str, Any], path: str, required_statuses: set[str]
) -> None:
    operation = document["paths"][path]["post"]
    assert operation.get("security", []) == []
    assert required_statuses <= operation["responses"].keys()
    if path == "/webhooks/github":
        assert "200" not in operation["responses"]
