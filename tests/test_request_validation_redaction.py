"""Validation locations identify declared fields without exposing request-owned keys."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient
from pydantic import BaseModel

from app.main import render_validation_error


class _NestedItem(BaseModel):
    count: int


class _NestedRequest(BaseModel):
    items: list[dict[str, _NestedItem]]


def test_mapping_key_is_redacted_but_declared_fields_and_index_remain() -> None:
    app = FastAPI()
    app.exception_handler(RequestValidationError)(render_validation_error)

    @app.post("/nested")
    async def submit(_body: _NestedRequest) -> dict[str, bool]:
        return {"ok": True}

    response = TestClient(app).post(
        "/nested", json={"items": [{"SECRET-TOKEN-123": {"count": "invalid"}}]}
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
