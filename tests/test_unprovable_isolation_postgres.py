"""A changed file whose isolation cannot be proven fails the run (RFS §11, #497 part 2).

The composition of ``tests/test_snapshot_path_e2e_postgres.py`` with one change: next to an
allowed ``src/ok.py`` the pull request carries a name the run cannot store unchanged — a
NUL or more than 1024 characters, as the new or the old name. The source has no stored
identity to withhold, so a v4 run fails with ``policy_snapshot_integrity_error`` on its
first attempt: nothing of the diff is stored and no model is asked (CVS §9.1). A lone
surrogate cannot be stored either, but the GitHub answer model rejects it before this check.
"""

from __future__ import annotations

import asyncio
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

_OK_SHA = "7" * 40
_SHA = "8" * 40
_LONG = "d/" * 549 + "x.py"
_ALLOWED = {"filename": "src/ok.py", "status": "added", "sha": _OK_SHA}


@pytest.mark.parametrize(
    ("change", "name"),
    [
        ({"filename": "a\x00b.py", "status": "added"}, "a\x00b.py"),
        (
            {"filename": "src/new.py", "status": "renamed", "previous_filename": "a\x00b.py"},
            "a\x00b.py",
        ),
        ({"filename": _LONG, "status": "added"}, _LONG),
        ({"filename": "src/new.py", "status": "renamed", "previous_filename": _LONG}, _LONG),
    ],
    ids=["nul-new-name", "nul-old-name", "long-new-name", "long-old-name"],
)
@pytest.mark.integration
def test_a_name_the_run_cannot_store_fails_the_v4_run_before_any_model_call(
    env: Env, monkeypatch: pytest.MonkeyPatch, change: dict[str, str], name: str
) -> None:
    files = [_ALLOWED, {"sha": _SHA, **change}]

    @dataclass
    class UnstorableNameGitHub(e2e.GitHubHttp):
        """PR 3 of octo/a: an allowed file and a file under a name the run cannot store."""

        def handle(self, request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.endswith("/pulls/3"):
                self.calls.append(f"{request.method} {path}")
                payload = _pull_payload(changed_files=len(files), head_sha=e2e._HEAD)
                payload["number"] = 3
                return httpx.Response(200, json=payload)
            if path.endswith("/pulls/3/files"):
                self.calls.append(f"{request.method} {path}")
                line = {"additions": 1, "deletions": 0, "changes": 1, "patch": "@@ -0,0 +1 @@\n+x"}
                return httpx.Response(200, json=[{**line, **item} for item in files])
            for sha in (_OK_SHA, _SHA):
                if path.endswith(f"/git/blobs/{sha}"):
                    self.calls.append(f"{request.method} {path}")
                    return e2e._blob(sha, b"x\n")
            return super().handle(request)

    # The shared scenario builds its GitHub transport by this name.
    monkeypatch.setattr(e2e, "GitHubHttp", UnstorableNameGitHub)
    observed = e2e._observe(env, version=4)

    async def stored() -> tuple[int, int, str]:
        engine = env.engine()
        try:
            async with async_sessionmaker(engine)() as session:
                params: dict[str, Any] = {"id": observed.run_id}
                usage: int = await session.scalar(
                    text("SELECT count(*) FROM usage_events WHERE run_id=:id"), params
                )
                diffs: int = await session.scalar(
                    text("SELECT count(*) FROM code_change_diffs WHERE run_id=:id"), params
                )
                message: str = await session.scalar(
                    text("SELECT error_message FROM runs WHERE id=:id"), params
                )
                return usage, diffs, message
        finally:
            await engine.dispose()

    usage_events, diff_rows, error_message = asyncio.run(stored())
    # The first attempt ends the run: the code is not retried.
    assert observed.outcomes == ["ack"]
    assert observed.state == ("failed", "policy_snapshot_integrity_error", 1)
    assert observed.tools == ["policy.load", "vcs.fetch_diff"]
    assert observed.manifests == []
    assert observed.bodies == []
    assert usage_events == 0
    # Neither the allowed file nor the other one is stored; the message names neither.
    assert diff_rows == 0
    assert "isolation" in error_message
    assert name not in error_message
    assert "src/ok.py" not in error_message
