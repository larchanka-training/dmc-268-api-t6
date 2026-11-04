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
from review.scripts.eval_live import (
    BACKEND_RULES,
    FRONTEND_RULES,
    REPO_ROOT,
    RecorderError,
    record_live,
)
from review.scripts.eval_rag_rehearsal import DEFAULT_MODEL, RehearsalTransport, rehearsal_settings
from review.scripts.eval_tokens import PromptTokens, format_table, main, measure
from review.scripts.validate_dataset import DEFAULT_ROOT

V3 = REPO_ROOT / "review/prompts/review.system.v3.md"
V5 = REPO_ROOT / "review/prompts/review.system.v5.md"
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
    root: Path, responses_dir: Path, system_prompt: Path, arm: str, settings: LlmSettings
) -> list[ChatRequest]:
    capture = _LiveCapture()
    asyncio.run(
        record_live(
            root,
            settings,
            system_prompt=system_prompt,
            backend_rules=BACKEND_RULES,
            frontend_rules=FRONTEND_RULES,
            transport=capture,
            policy_arm=arm,
            responses_dir=responses_dir,
        )
    )
    return capture.requests


def _recount(requests: list[ChatRequest], chars_per_token: float = 3.0) -> list[int]:
    """The expected estimates: each request's messages, counted here by their length."""
    return [
        sum(math.ceil(len(message.content) / chars_per_token) for message in request.messages)
        for request in requests
    ]


@pytest.mark.parametrize(
    ("arm", "chars_per_token", "snapshot"),
    [("legacy", 3.0, False), ("none", 3.0, True), ("legacy", 2.0, False)],
)
def test_the_estimate_counts_the_envelope_a_live_capture_of_the_arm_sends(
    tiny_root: Path, tmp_path: Path, arm: str, chars_per_token: float, snapshot: bool
) -> None:
    default = rehearsal_settings(DEFAULT_MODEL)
    settings = LlmSettings(replace(default.primary, chars_per_token=chars_per_token), None)

    requests = _live_requests(tiny_root, tmp_path / "live", V3, arm, settings)
    measured = measure(tiny_root, V3, arm, settings)

    for request in requests:
        assert [message.role for message in request.messages] == ["system", "user"]
        assert ("<service_defaults" in request.messages[1].content) is snapshot
    expected = _recount(requests, chars_per_token)
    assert measured.by_case == dict(zip(TINY, expected, strict=True))
    assert measured.total == sum(expected)
    assert measured.prompt_path == "review/prompts/review.system.v3.md"
    assert measured.prompt_version == "v3"


def test_the_command_prints_every_case_and_the_sum_with_no_key_and_no_network(
    tiny_root: Path, tmp_path: Path, offline: list[object], capsys: pytest.CaptureFixture[str]
) -> None:
    settings = rehearsal_settings(DEFAULT_MODEL)
    expected = _recount(_live_requests(tiny_root, tmp_path / "live", V3, "legacy", settings))

    assert main(["--root", str(tiny_root), "--system", str(V3)]) == 0

    stdout = capsys.readouterr().out
    assert _rows(stdout) == {
        **{case_id: [str(tokens)] for case_id, tokens in zip(TINY, expected, strict=True)},
        "total": [str(sum(expected))],
    }
    assert "A: review/prompts/review.system.v3.md (v3)" in stdout
    assert "policy arm legacy" in stdout
    assert offline == []


@pytest.mark.parametrize(
    ("system", "refusal"),
    [
        (V5, "--policy-arm legacy renders no policy snapshot, which review prompt v5 needs"),
        (Path("/etc/hostname"), "static input must be inside the repository"),
        (REPO_ROOT / "review/prompts/no-such-prompt.md", "missing digest file"),
    ],
)
def test_a_recorder_refusal_is_exit_1_with_no_table(
    tiny_root: Path, capsys: pytest.CaptureFixture[str], system: Path, refusal: str
) -> None:
    assert main(["--root", str(tiny_root), "--system", str(system)]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert refusal in captured.err


def test_a_case_that_cannot_fit_the_window_is_refused_not_counted_as_zero(tiny_root: Path) -> None:
    default = rehearsal_settings(DEFAULT_MODEL).primary
    tiny_window = replace(default, context_window=default.max_output_tokens + 10)

    with pytest.raises(RecorderError, match="CLEAN-01.*SEC-01"):
        measure(tiny_root, V3, "legacy", LlmSettings(tiny_window, None))


def test_a_case_that_needed_a_repair_call_is_refused_not_counted_twice(
    tiny_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    honest = RehearsalTransport.complete
    calls = 0

    async def invalid_first_answer(
        self: RehearsalTransport, request: ChatRequest, *, checkpoint: RunProviderCheckpoint
    ) -> ChatResponse:
        nonlocal calls
        calls += 1
        response = await honest(self, request, checkpoint=checkpoint)
        return replace(response, content='{"findings":[]}') if calls == 1 else response

    monkeypatch.setattr(RehearsalTransport, "complete", invalid_first_answer)

    with pytest.raises(RecorderError, match=r"CLEAN-01 \(accepted, 2 calls\)") as refused:
        measure(tiny_root, V3, "legacy", rehearsal_settings(DEFAULT_MODEL))
    assert "SEC-01" not in str(refused.value)


@pytest.mark.parametrize("arm", ["legacy", "none"])
def test_the_gold_corpus_prints_its_24_cases_and_their_sum(
    arm: str, offline: list[object], capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["--system", str(V3), "--policy-arm", arm]) == 0

    stdout = capsys.readouterr().out
    # Relative, so the output of two clones of the repository is the same.
    assert f"eval_tokens: root test-prs-dataset, policy arm {arm}," in stdout
    rows = _rows(stdout)
    total = rows.pop("total")
    assert len(rows) == 24
    assert total == [str(sum(int(cells[0]) for cells in rows.values()))]
    assert offline == []


@pytest.mark.parametrize(
    "arguments",
    [
        ["--system", str(V3), "--policy-arm", "valid"],
        ["--system", str(V3), "--policy-arm", "malicious"],
        ["--system", str(V3), "--system", str(V3), "--system", str(V3)],
    ],
)
def test_what_the_command_cannot_measure_is_an_argument_error(arguments: list[str]) -> None:
    with pytest.raises(SystemExit) as refused:
        main(arguments)
    assert refused.value.code == 2


def test_two_prompt_files_print_the_delta_of_each_case_and_of_the_total() -> None:
    first = PromptTokens("p/a.md", "v3", {"CASE-1": 1000, "CASE-2": 200})
    second = PromptTokens("p/b.md", "v5", {"CASE-1": 1215, "CASE-2": 160})

    table = format_table(
        [first, second],
        root=Path("corpus"),
        policy_arm="none",
        settings=rehearsal_settings(DEFAULT_MODEL),
    )

    assert table.splitlines() == [
        "eval_tokens: root corpus, policy arm none, model mistral-small-4, chars_per_token 3.0",
        "A: p/a.md (v3)",
        "B: p/b.md (v5)",
        "case       A     B  B vs A",
        "CASE-1  1000  1215  +21.5%",
        "CASE-2   200   160  -20.0%",
        "total   1200  1375  +14.6%",
    ]


def test_two_prompt_files_print_what_a_live_capture_of_each_sends(
    tiny_root: Path, tmp_path: Path, offline: list[object], capsys: pytest.CaptureFixture[str]
) -> None:
    settings = rehearsal_settings(DEFAULT_MODEL)
    older = _recount(_live_requests(tiny_root, tmp_path / "v3", V3, "none", settings))
    newer = _recount(_live_requests(tiny_root, tmp_path / "v5", V5, "none", settings))
    arguments = ["--root", str(tiny_root), "--policy-arm", "none"]

    assert main([*arguments, "--system", str(V3), "--system", str(V5)]) == 0

    rows = _rows(capsys.readouterr().out)
    assert {case_id: cells[:2] for case_id, cells in rows.items()} == {
        **{case_id: [str(a), str(b)] for case_id, a, b in zip(TINY, older, newer, strict=True)},
        "total": [str(sum(older)), str(sum(newer))],
    }
    # v5 is the longer prompt, so every row grows.
    assert all(cells[2].startswith("+") for cells in rows.values())
    assert offline == []


def test_two_runs_on_the_same_inputs_print_the_same_bytes(
    tiny_root: Path, offline: list[object], capsys: pytest.CaptureFixture[str]
) -> None:
    arguments = ["--root", str(tiny_root), "--policy-arm", "none", "--system", str(V5)]

    assert main(arguments) == 0
    first = capsys.readouterr().out
    assert main(arguments) == 0

    assert capsys.readouterr().out == first
    assert first != ""
    assert offline == []
