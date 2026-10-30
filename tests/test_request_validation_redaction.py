"""Validation locations identify declared fields without exposing request-owned keys."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient
from pydantic import BaseModel, field_validator

from app.main import render_validation_error


class _NestedItem(BaseModel):
    count: int


class _NestedRequest(BaseModel):
    items: list[dict[str, _NestedItem]]


class _ContextRequest(BaseModel):
    token: str

    @field_validator("token")
    @classmethod
    def reject_token(cls, value: str) -> str:
        raise ValueError("sensitive reason")


def test_mapping_key_is_redacted_but_declared_fields_and_index_remain() -> None:
    app = FastAPI()
    app.exception_handler(RequestValidationError)(render_validation_error)

    @app.post("/api/workspaces/00000000-0000-0000-0000-000000000001/bitbucket/nested")
    async def submit(_body: _NestedRequest) -> dict[str, bool]:
        return {"ok": True}

    response = TestClient(app).post(
        "/api/workspaces/00000000-0000-0000-0000-000000000001/bitbucket/nested",
        json={"items": [{"SECRET-TOKEN-123": {"count": "invalid"}}]},
    )

    assert response.status_code == 422
    assert response.json()["detail"] == [
        {
            "type": "int_parsing",
            "loc": ["body", "items", 0, "<unknown field>", "count"],
            "msg": "Invalid input",
        }
    ]
    assert "SECRET-TOKEN-123" not in response.text


@pytest.mark.parametrize(
    "path",
    [
        "/api/workspaces/00000000-0000-0000-0000-000000000001/repositories",
        "/api/workspaces/00000000-0000-0000-0000-000000000001/bitbucketish/nested",
        "/webhooks/bitbucketish/nested",
    ],
)
def test_non_bitbucket_browser_api_validation_keeps_fastapi_error_fields(path: str) -> None:
    app = FastAPI()
    app.exception_handler(RequestValidationError)(render_validation_error)

    @app.post(path)
    async def submit(_body: _NestedRequest) -> dict[str, bool]:
        return {"ok": True}

    response = TestClient(app).post(
        path,
        json={"items": [{"public-field": {"count": "invalid"}}]},
    )

    assert response.status_code == 422
    assert response.json()["detail"] == [
        {
            "type": "int_parsing",
            "loc": ["body", "items", 0, "public-field", "count"],
            "msg": "Input should be a valid integer, unable to parse string as an integer",
            "input": "invalid",
        }
    ]


def test_gitlab_webhook_keeps_error_context_while_bitbucket_redacts_it() -> None:
    app = FastAPI()
    app.exception_handler(RequestValidationError)(render_validation_error)

    @app.post("/webhooks/gitlab/example")
    @app.post("/api/workspaces/00000000-0000-0000-0000-000000000001/bitbucket/example")
    async def submit(_body: _ContextRequest) -> dict[str, bool]:
        return {"ok": True}

    client = TestClient(app)
    github_gitlab = client.post("/webhooks/gitlab/example", json={"token": "sensitive-value"})
    bitbucket = client.post(
        "/api/workspaces/00000000-0000-0000-0000-000000000001/bitbucket/example",
        json={"token": "sensitive-value"},
    )

    assert github_gitlab.status_code == 422
    assert github_gitlab.json()["detail"] == [
        {
            "type": "value_error",
            "loc": ["body", "token"],
            "msg": "Value error, sensitive reason",
            "input": "sensitive-value",
            "ctx": {"error": {}},
        }
    ]
    assert bitbucket.status_code == 422
    assert bitbucket.json()["detail"] == [
        {"type": "value_error", "loc": ["body", "token"], "msg": "Invalid input"}
    ]
    assert "sensitive-value" not in bitbucket.text
