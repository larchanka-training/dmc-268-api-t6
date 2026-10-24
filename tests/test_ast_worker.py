"""Parser worker: the OS CPU limit follows each request and stays behind the parent's deadlines."""

from __future__ import annotations

import asyncio
import json
import math
import os
import resource
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from app.modules.reviews.application.ast_symbols import (
    MAX_PARSE_CPU_SECONDS_PER_FILE,
    AstFacts,
    AstLanguage,
    ParseRequest,
)
from app.modules.reviews.infrastructure import ast_worker, subprocess_source_parser
from app.modules.reviews.infrastructure.subprocess_source_parser import SubprocessSourceParser

_PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(("used", "soft"), [(0.0, 21), (0.2, 22), (7.0, 28), (7.01, 29)])
def test_soft_limit_is_used_cpu_rounded_up_plus_one_file_and_the_slack(
    used: float, soft: int
) -> None:
    assert ast_worker._cpu_soft_limit(used) == soft


def test_os_limit_stays_behind_the_longest_wait_of_the_parent() -> None:
    longest_wait = (
        MAX_PARSE_CPU_SECONDS_PER_FILE
        + subprocess_source_parser._WALL_SLACK_SECONDS
        + subprocess_source_parser._STARTUP_SECONDS
    )
    # The least CPU a request may spend; a single-threaded worker spends no more CPU than
    # wall time, so the parent's kill always comes first.
    assert ast_worker._cpu_soft_limit(0.0) > longest_wait


def _patch_limits(monkeypatch: pytest.MonkeyPatch, hard: int) -> list[tuple[int, tuple[int, int]]]:
    calls: list[tuple[int, tuple[int, int]]] = []
    current = {resource.RLIMIT_CPU: (5, hard)}
    monkeypatch.setattr(time, "process_time", lambda: 7.2)
    monkeypatch.setattr(resource, "getrlimit", lambda which: current[which])
    monkeypatch.setattr(resource, "setrlimit", lambda which, limits: calls.append((which, limits)))
    return calls


def test_limit_keeps_an_unlimited_hard_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_limits(monkeypatch, resource.RLIM_INFINITY)

    ast_worker._limit_cpu()

    assert calls == [(resource.RLIMIT_CPU, (29, resource.RLIM_INFINITY))]


@pytest.mark.parametrize(("hard", "soft"), [(100, 29), (29, 29), (12, 12)])
def test_limit_keeps_a_finite_hard_limit_and_never_goes_above_it(
    monkeypatch: pytest.MonkeyPatch, hard: int, soft: int
) -> None:
    calls = _patch_limits(monkeypatch, hard)

    ast_worker._limit_cpu()

    assert calls == [(resource.RLIMIT_CPU, (soft, hard))]


def test_no_limit_where_the_platform_has_no_cpu_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_limits(monkeypatch, resource.RLIM_INFINITY)
    monkeypatch.delattr(resource, "RLIMIT_CPU")

    ast_worker._limit_cpu()

    assert calls == []


def _patch_setrlimit(monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, tuple[int, int]]]:
    calls: list[tuple[int, tuple[int, int]]] = []
    monkeypatch.setattr(resource, "setrlimit", lambda which, limits: calls.append((which, limits)))
    return calls


def test_core_dumps_are_disabled_for_good(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_setrlimit(monkeypatch)

    ast_worker._disable_core_dumps()

    assert calls == [(resource.RLIMIT_CORE, (0, 0))]


def test_no_core_limit_where_the_platform_has_none(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_setrlimit(monkeypatch)
    monkeypatch.delattr(resource, "RLIMIT_CORE")

    ast_worker._disable_core_dumps()

    assert calls == []


def _run_cpu_bound(*burns: float) -> tuple[subprocess.CompletedProcess[str], float]:
    """Run the fake worker directly: the parent's wall-clock kill would come first.

    Returns the finished process and the CPU seconds of that worker alone. Every request
    burns a finite time, so reading the pipes to the end cannot hang.
    """
    lines = "".join(
        json.dumps({"key": f"{number}.py", "burn": burn}) + "\n"
        for number, burn in enumerate(burns)
    )
    args = [sys.executable, "-m", "tests.ast_workers.cpu_bound"]
    with subprocess.Popen(
        args,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=_PROJECT_ROOT,
    ) as process:
        assert process.stdin is not None
        assert process.stdout is not None and process.stderr is not None
        process.stdin.write(lines)
        process.stdin.close()
        stdout, stderr = process.stdout.read(), process.stderr.read()
        # wait4, not wait: RUSAGE_CHILDREN would also count any child reaped meanwhile.
        _, status, usage = os.wait4(process.pid, 0)
        # CONTRACT: wait4 reaped the worker, so Popen must not wait for it again.
        process.returncode = os.waitstatus_to_exitcode(status)
    completed = subprocess.CompletedProcess(args, process.returncode, stdout, stderr)
    return completed, usage.ru_utime + usage.ru_stime


def test_request_over_its_cpu_allowance_is_killed_by_sigxcpu() -> None:
    completed, spent = _run_cpu_bound(30.0)

    assert completed.returncode == -signal.SIGXCPU
    assert completed.stdout == ""
    # The kernel stops the worker as soon as its CPU time reaches the armed soft limit.
    armed = int(completed.stderr.split()[-1])
    assert armed - 0.1 < spent < armed + 1.0


def test_cheap_requests_outlive_the_limit_armed_for_the_first_one() -> None:
    completed, _ = _run_cpu_bound(*[0.5] * 6)

    assert completed.returncode == 0
    answers = [json.loads(line) for line in completed.stdout.splitlines()]
    assert [answer["key"] for answer in answers] == [f"{number}.py" for number in range(6)]
    # A limit armed once, at the first request, would have killed the worker.
    assert answers[-1]["cpu"] > answers[0]["soft"]
    assert answers[-1]["soft"] > answers[0]["soft"]


def _proc_limit(pid: int, name: str) -> tuple[str, str]:
    for line in Path(f"/proc/{pid}/limits").read_text(encoding="ascii").splitlines():
        if line.startswith(name):
            soft, hard = line[len(name) :].split()[:2]
            return soft, hard
    raise AssertionError(f"no {name!r} in /proc")


def _cpu_seconds(pid: int) -> float:
    fields = Path(f"/proc/{pid}/stat").read_text(encoding="ascii").rsplit(")", 1)[1].split()
    ticks = os.sysconf("SC_CLK_TCK")
    # utime and stime, truncated to whole clock ticks: one tick covers the truncation.
    return (int(fields[11]) + int(fields[12]) + 1) / ticks


def _read_real_worker[T](read: Callable[[int], T]) -> T:
    """Parse one file with the real worker, then ``read`` it by pid while it still runs."""

    async def run() -> T:
        parser = SubprocessSourceParser()
        try:
            source = b"def f():\n    return 1\n"
            results = await parser.parse_many((ParseRequest("a.py", AstLanguage.PYTHON, source),))
            assert isinstance(results["a.py"], AstFacts)
            assert parser._process is not None
            return read(parser._process.pid)
        finally:
            await parser.aclose()

    return asyncio.run(run())


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="reads /proc")
def test_real_worker_arms_the_cpu_limit_before_a_request() -> None:
    (soft, hard), cpu = _read_real_worker(
        lambda pid: (_proc_limit(pid, "Max cpu time"), _cpu_seconds(pid))
    )

    inherited = resource.getrlimit(resource.RLIMIT_CPU)[1]
    assert hard == ("unlimited" if inherited == resource.RLIM_INFINITY else str(inherited))
    assert soft != "unlimited"
    # One file and the slack over the CPU the worker had used, rounded up to whole seconds.
    assert 1 <= int(soft) - 21 <= math.ceil(cpu)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="reads /proc")
def test_real_worker_writes_no_core_dump() -> None:
    # A worker killed by SIGXCPU or a crash would dump the repository sources it holds.
    assert _read_real_worker(lambda pid: _proc_limit(pid, "Max core file size")) == ("0", "0")
