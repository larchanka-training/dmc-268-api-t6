from fastapi.testclient import TestClient

from app.main import app


def test_runs_list_uses_the_camel_case_contract() -> None:
    response = TestClient(app).get("/api/runs")

    assert response.status_code == 200
    assert response.json() == {"items": [], "nextCursor": None}
