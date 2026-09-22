from app.main import app
from fastapi.testclient import TestClient

client = TestClient(app)


def test_healthcheck() -> None:
    response = client.get("/healthcheck")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
