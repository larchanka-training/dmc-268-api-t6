from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.bootstrap import reviews_api
from app.main import app, get_run_repository


class EmptyRunRepository:
    async def list_runs(self, **_: Any) -> list[object]:
        return []


def test_api_lifecycle_owns_and_disposes_its_database_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeEngine:
        def __init__(self) -> None:
            self.disposed = False

        async def dispose(self) -> None:
            self.disposed = True

    engine = FakeEngine()
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://example.test/backend")
    monkeypatch.setattr(reviews_api, "create_async_engine", lambda *_args, **_kwargs: engine)
    app.dependency_overrides[get_run_repository] = EmptyRunRepository
    try:
        with TestClient(app) as client:
            response = client.get("/api/runs")
            assert response.status_code == 200
            assert engine.disposed is False
    finally:
        app.dependency_overrides.clear()

    assert engine.disposed is True
