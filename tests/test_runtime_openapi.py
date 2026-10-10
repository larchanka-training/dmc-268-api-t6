"""The served OpenAPI document describes authentication and actual HTTP responses."""

from __future__ import annotations

from typing import Annotated, Any

import pytest
from fastapi import APIRouter, Depends, FastAPI
from fastapi.routing import iter_route_contexts
from fastapi.testclient import TestClient

from app.bootstrap.portal_auth import get_auth_scope
from app.main import app, build_openapi
from app.modules.auth.application.scope import AuthScope

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


def _workspace_count(scope: Annotated[AuthScope, Depends(get_auth_scope)]) -> int:
    return len(scope.workspace_ids)


def _synthetic_app() -> FastAPI:
    """Routes under ``/api/`` whose only difference is how, if at all, they authorize."""
    synthetic = FastAPI()
    guarded = APIRouter(prefix="/api", dependencies=[Depends(get_auth_scope)])
    open_router = APIRouter(prefix="/api")

    @guarded.get("/router-guarded")
    async def router_guarded() -> None: ...

    @open_router.get("/parameter-guarded")
    async def parameter_guarded(scope: Annotated[AuthScope, Depends(get_auth_scope)]) -> None: ...

    @open_router.get("/nested-guarded")
    async def nested_guarded(count: Annotated[int, Depends(_workspace_count)]) -> None: ...

    @open_router.get("/public")
    async def public() -> None: ...

    @open_router.post("/mixed")
    async def mixed_guarded(scope: Annotated[AuthScope, Depends(get_auth_scope)]) -> None: ...

    @open_router.get("/mixed")
    async def mixed_public() -> None: ...

    synthetic.include_router(guarded)
    synthetic.include_router(open_router)
    return synthetic


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", "/api/router-guarded"),
        ("get", "/api/parameter-guarded"),
        ("get", "/api/nested-guarded"),
        ("post", "/api/mixed"),
    ],
)
def test_runtime_openapi_security_follows_the_auth_dependency(method: str, path: str) -> None:
    operation = build_openapi(_synthetic_app())["paths"][path][method]
    assert operation["security"] == [{"bearerAuth": []}]
    assert {"401", "503"} <= operation["responses"].keys()


@pytest.mark.parametrize("path", ["/api/public", "/api/mixed"])
def test_runtime_openapi_public_api_route_declares_no_auth(path: str) -> None:
    operation = build_openapi(_synthetic_app())["paths"][path]["get"]
    assert "security" not in operation
    assert not {"401", "503"} & operation["responses"].keys()


def test_runtime_openapi_matches_what_the_synthetic_routes_enforce() -> None:
    client = TestClient(_synthetic_app())
    assert client.get("/api/public").status_code == 200
    assert client.get("/api/mixed").status_code == 200
    for path in ("/api/router-guarded", "/api/parameter-guarded", "/api/nested-guarded"):
        assert client.get(path).status_code == 401
    assert client.post("/api/mixed").status_code == 401
