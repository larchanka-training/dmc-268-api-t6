"""The offline measurement of the review prompt's input tokens (#317).

Nothing here spends money or needs a key: the script's transport stays in the process. The
expected counts come from the requests a live capture sends through this file's own transport,
counted here character by character, not from the script's arithmetic.
"""

from __future__ import annotations

import asyncio
import math
import os
import shutil
import socket
from dataclasses import replace
from pathlib import Path

import pytest

from app.modules.reviews.application.check_runs import RunProviderCheckpoint
from app.modules.reviews.infrastructure.llm.settings import LlmSettings
from app.modules.reviews.infrastructure.llm.transport import ChatRequest, ChatResponse
from review.scripts.eval_live import BACKEND_RULES, FRONTEND_RULES, REPO_ROOT, record_live
from review.scripts.eval_rag_rehearsal import DEFAULT_MODEL, RehearsalTransport, rehearsal_settings
from review.scripts.eval_tokens import main, measure
from review.scripts.validate_dataset import DEFAULT_ROOT

V3 = REPO_ROOT / "review/prompts/review.system.v3.md"
TINY = ("CLEAN-01", "SEC-01")


@pytest.fixture(scope="module")
def tiny_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Two gold cases, enough for everything but the gold smoke."""
    root = tmp_path_factory.mktemp("eval-tokens") / "dataset"
    for case_id in TINY:
        shutil.copytree(DEFAULT_ROOT / "cases" / case_id, root / "cases" / case_id)
    return root


@pytest.fixture
def offline(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    """No key in the environment and no connection; returns whatever tried anyway."""
    attempts: list[object] = []

    def refuse(*args: object, **_: object) -> None:
        attempts.append(args)
        raise OSError("the measurement tried the network or the LLM environment")

    for name in list(os.environ):
        if name.startswith("LLM_") or name.endswith("_API_KEY"):
            monkeypatch.delenv(name)
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    monkeypatch.setattr(LlmSettings, "from_env", refuse)
    return attempts


def _rows(stdout: str) -> dict[str, list[str]]:
    """The table of the output: the first cell of each row after the header, the other cells."""
    table = stdout[stdout.index("\ncase") + 1 :].splitlines()[1:]
    return {row.split()[0]: row.split()[1:] for row in table}


class _LiveCapture(RehearsalTransport):
    """What a live capture of the same inputs sends, kept by the test itself."""

    def __init__(self) -> None:
        self.requests: list[ChatRequest] = []

    async def complete(
        self, request: ChatRequest, *, checkpoint: RunProviderCheckpoint
    ) -> ChatResponse:
        self.requests.append(request)
        return await super().complete(request, checkpoint=checkpoint)


def _live_requests(
    root: Path, responses_dir: Path, arm: str, settings: LlmSettings
) -> list[ChatRequest]:
    capture = _LiveCapture()
    asyncio.run(
        record_live(
            root,
            settings,
            system_prompt=V3,
            backend_rules=BACKEND_RULES,
            frontend_rules=FRONTEND_RULES,
            transport=capture,
            policy_arm=arm,
            responses_dir=responses_dir,
        )
    )
    return capture.requests


@pytest.mark.parametrize(
    ("arm", "chars_per_token", "snapshot"),
    [("legacy", 3.0, False), ("none", 3.0, True), ("legacy", 2.0, False)],
)
def test_the_estimate_counts_the_envelope_a_live_capture_of_the_arm_sends(
    tiny_root: Path, tmp_path: Path, arm: str, chars_per_token: float, snapshot: bool
) -> None:
    default = rehearsal_settings(DEFAULT_MODEL)
    settings = LlmSettings(replace(default.primary, chars_per_token=chars_per_token), None)

    requests = _live_requests(tiny_root, tmp_path / "live", arm, settings)
    measured = measure(tiny_root, V3, arm, settings)

    for request in requests:
        assert [message.role for message in request.messages] == ["system", "user"]
        assert ("<service_defaults" in request.messages[1].content) is snapshot
    expected = [
        sum(math.ceil(len(message.content) / chars_per_token) for message in request.messages)
        for request in requests
    ]
    assert measured.by_case == dict(zip(TINY, expected, strict=True))
    assert measured.total == sum(expected)
    assert measured.prompt_path == "review/prompts/review.system.v3.md"
    assert measured.prompt_version == "v3"


def test_the_command_prints_every_case_and_the_sum_with_no_key_and_no_network(
    tiny_root: Path, offline: list[object], capsys: pytest.CaptureFixture[str]
) -> None:
    measured = measure(tiny_root, V3, "legacy", rehearsal_settings(DEFAULT_MODEL))

    assert main(["--root", str(tiny_root), "--system", str(V3)]) == 0

    stdout = capsys.readouterr().out
    assert _rows(stdout) == {
        **{case_id: [str(tokens)] for case_id, tokens in measured.by_case.items()},
        "total": [str(measured.total)],
    }
    assert "A: review/prompts/review.system.v3.md (v3)" in stdout
    assert "policy arm legacy" in stdout
    assert offline == []
