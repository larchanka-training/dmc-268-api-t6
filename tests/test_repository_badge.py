"""The public SVG badge of a repository's latest review, over a fake port (#454)."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml  # type: ignore[import-untyped]
from fastapi.testclient import TestClient

from app.bootstrap.reviews_api import get_repository_badge
from app.main import app
from app.modules.reviews.application.repository_badge import (
    BadgeRun,
    BadgeStatus,
    GetRepositoryBadge,
)
from app.modules.reviews.application.run_coverage import unavailable_run_coverage
from app.modules.reviews.application.saved_review_result import SavedReviewResult

BADGE_PATH = "/api/badges/github/{owner}/{repo}.svg"
BADGE_URL = "/api/badges/github/octo-org/hello.world.svg"
CACHE_CONTROL = "public, max-age=300"
SPEC = Path(__file__).resolve().parents[1] / "contracts" / "openapi.yaml"


def _saved(verdict: Any) -> SavedReviewResult:
    return SavedReviewResult(
        verdict=verdict, coverage=unavailable_run_coverage([], "a" * 40), summary={}
    )


class _Queries:
    def __init__(self, run: BadgeRun | None = None) -> None:
        self.run = run
        self.names: list[str] = []

    async def latest_succeeded_run(self, full_name: str) -> BadgeRun | None:
        self.names.append(full_name)
        return self.run


@pytest.fixture
def queries() -> Iterator[_Queries]:
    fake = _Queries()
    app.dependency_overrides[get_repository_badge] = lambda: GetRepositoryBadge(fake)
    yield fake
    app.dependency_overrides.pop(get_repository_badge)


@pytest.mark.parametrize(
    ("run", "text"),
    [
        (BadgeRun(False, (), _saved("clean")), "passed"),
        (BadgeRun(False, ("low",), _saved("attention")), "attention"),
        (BadgeRun(False, ("medium",), None), "attention"),
        (BadgeRun(False, ("critical",), _saved("blocking")), "blocking"),
        (BadgeRun(False, ("high",), None), "blocking"),
    ],
)
def test_a_verdict_is_served_without_authorization(
    queries: _Queries, run: BadgeRun, text: str
) -> None:
    queries.run = run

    response = TestClient(app).get(BADGE_URL)

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/svg+xml"
    assert response.headers["cache-control"] == CACHE_CONTROL
    assert f"<title>AI review: {text}</title>" in response.text
    assert f'aria-label="AI review: {text}"' in response.text
    assert f">{text}</text>" in response.text
    assert "octo-org" not in response.text
    assert "hello" not in response.text
    assert queries.names == ["octo-org/hello.world"]


@pytest.mark.parametrize(
    ("run", "url"),
    [
        pytest.param(None, BADGE_URL, id="unknown-private-or-no-runs"),
        pytest.param(BadgeRun(True, ("high",), _saved("blocking")), BADGE_URL, id="summary-only"),
        pytest.param(BadgeRun(False, (), None), BADGE_URL, id="clean-without-coverage"),
        pytest.param(BadgeRun(False, (), _saved(None)), BADGE_URL, id="saved-without-verdict"),
        pytest.param(None, "/api/badges/github/octo%00org/hello.svg", id="nul-byte"),
        pytest.param(None, "/api/badges/github/octo.org/hello.svg", id="dot-in-owner"),
        pytest.param(None, "/api/badges/github/octo-org/%3Cscript%3E.svg", id="markup"),
        pytest.param(None, "/api/badges/github/octo-org/...svg", id="only-dots"),
        pytest.param(None, f"/api/badges/github/octo-org/{'a' * 512}.svg", id="too-long"),
    ],
)
def test_every_no_data_answer_is_the_same_response(
    queries: _Queries, run: BadgeRun | None, url: str
) -> None:
    queries.run = run

    response = TestClient(app).get(url)

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/svg+xml"
    assert response.headers["cache-control"] == CACHE_CONTROL
    assert "<title>AI review: no data</title>" in response.text
    assert 'aria-label="AI review: no data"' in response.text
    assert ">no data</text>" in response.text
    assert "script" not in response.text
    assert "octo" not in response.text
    # Byte for byte the one neutral answer, whichever way it was reached.
    queries.run = None
    assert response.content == TestClient(app).get(BADGE_URL).content


@pytest.mark.parametrize(
    ("owner", "repo"),
    [
        ("octo\x00org", "hello"),
        ("octo.org", "hello"),
        ("octo-org", ".."),
        ("octo-org", "hello\n"),
        ("octo-org", "a" * 512),
    ],
)
def test_an_invalid_name_never_reaches_the_query(owner: str, repo: str) -> None:
    queries = _Queries(BadgeRun(False, ("high",), None))

    status = asyncio.run(GetRepositoryBadge(queries).execute(owner, repo))

    assert status is BadgeStatus.NO_DATA
    assert queries.names == []


def test_the_spec_describes_the_badge_without_bearer(queries: _Queries) -> None:
    spec = yaml.safe_load(SPEC.read_text(encoding="utf-8"))
    operation = spec["paths"][BADGE_PATH]["get"]
    answer = operation["responses"]["200"]

    assert operation["security"] == []
    assert "image/svg+xml" in answer["content"]
    assert answer["headers"]["Cache-Control"]["schema"]["const"] == CACHE_CONTROL
    assert "security" not in app.openapi()["paths"][BADGE_PATH]["get"]
    assert TestClient(app).get(BADGE_URL).headers["cache-control"] == CACHE_CONTROL
