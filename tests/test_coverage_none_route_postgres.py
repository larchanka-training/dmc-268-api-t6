"""A technical-only change through the worker composition on PostgreSQL (#130 F5, DD2-DD6).

The composition of ``tests/test_snapshot_path_e2e_postgres.py`` with one change: the pull
request adds only ``a\\b.txt``, a name outside the path contract (``unsupported_path``).
A v4 run records its context and is finished with the backend result, coverage none,
without a single model request; a v3 run in the same composition still calls conventions
and the review, as before #130 F5.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from tests import test_snapshot_path_e2e_postgres as e2e
from tests.portal_postgres import Env
from tests.test_github_vcs_provider import _pull_payload
from tests.test_snapshot_path_e2e_postgres import env as env

_NAME = "a\\b.txt"
_SHA = "7" * 40
_MERGE_BASE = "c" * 40


@dataclass
class TechnicalOnlyGitHub(e2e.GitHubHttp):
    """PR 3 of octo/a, whose only change is a text file under an unsupported name."""

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/pulls/3"):
            self.calls.append(f"{request.method} {path}")
            payload = _pull_payload(changed_files=1, head_sha=e2e._HEAD)
            payload["number"] = 3
            return httpx.Response(200, json=payload)
        if path.endswith("/pulls/3/files"):
            self.calls.append(f"{request.method} {path}")
            file = {
                "filename": _NAME,
                "status": "added",
                "sha": _SHA,
                "additions": 1,
                "deletions": 0,
                "changes": 1,
                "patch": "@@ -0,0 +1 @@\n+text",
            }
            return httpx.Response(200, json=[file])
        if path.endswith(f"/git/blobs/{_SHA}"):
            self.calls.append(f"{request.method} {path}")
            return e2e._blob(_SHA, b"text\n")
        return super().handle(request)


def _observe(env: Env, monkeypatch: pytest.MonkeyPatch, *, v4: bool) -> tuple[e2e.Observed, int]:
    # The shared scenario builds its GitHub transport by this name. The conventions model
    # lists every changed file it is shown; a legacy run shows it none here.
    monkeypatch.setattr(e2e, "GitHubHttp", TechnicalOnlyGitHub)
    monkeypatch.setattr(e2e, "_CONVENTIONS_ANSWER", {**e2e._CONVENTIONS_ANSWER, "files": []})
    observed = e2e._observe(env, version=4 if v4 else 3)

    async def usage() -> int:
        engine = env.engine()
        try:
            async with async_sessionmaker(engine)() as session:
                count: int = await session.scalar(
                    text("SELECT count(*) FROM usage_events WHERE run_id=:id"),
                    {"id": observed.run_id},
                )
                return count
        finally:
            await engine.dispose()

    return observed, asyncio.run(usage())


@pytest.mark.integration
def test_a_technical_only_v4_run_is_published_without_any_model_call(
    env: Env, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING):
        observed, usage_events = _observe(env, monkeypatch, v4=True)

    assert observed.outcomes == ["ack"]
    assert observed.state == ("publishing", None, 1)
    assert observed.tools == [
        "policy.load",
        "vcs.fetch_diff",
        "context.build",
        "review.postprocess",
    ]
    assert observed.manifests == []
    assert observed.bodies == []
    assert usage_events == 0
    [(_, build)] = observed.actions["context.build"]
    assert build["status"] == "no_reviewable_text"
    assert build["diff"]["diff_old_sha"] == _MERGE_BASE
    assert build["diff"]["file_list_complete"] is True
    assert build["coverage"]["status"] == "none"
    assert build["coverage"]["reason_counts"] == {"unsupported_path": 1}
    # The attempt's one context.build is also its context_payloads row (schema 2).
    assert observed.payloads == [(2, build)]
    [(_, postprocess)] = observed.actions["review.postprocess"]
    # Restored from the context.build row, not the classification_unavailable fallback.
    assert postprocess["coverage"] == build["coverage"]
    assert postprocess["verdict"] is None
    assert postprocess["inline"] == 0
    assert postprocess["summary"]["problem"] == (
        "Подтверждённых замечаний нет; Текстовые изменения не проверены: неподдерживаемый путь (1)."
    )
    assert "Invalid saved context coverage" not in caplog.text
    [pointer] = observed.queue.published
    assert pointer.run_id == observed.run_id
    assert pointer.review_event == "COMMENT"


@pytest.mark.integration
def test_the_same_change_on_a_v3_run_still_calls_conventions_and_the_review(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    observed, usage_events = _observe(env, monkeypatch, v4=False)

    assert observed.outcomes == ["ack"]
    assert observed.state == ("publishing", None, 1)
    calls: list[dict[str, Any]] = [request for request, _ in observed.actions["llm.call"]]
    assert [call["operation"] for call in calls] == ["conventions", "review"]
    assert len(observed.bodies) == 2
    assert usage_events == 2
    [(_, build)] = observed.actions["context.build"]
    assert build["status"] == "summary_only"
    assert build["coverage"]["status"] == "partial"
    assert "llm.review_output" in observed.tools
