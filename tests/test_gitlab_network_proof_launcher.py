"""Offline supervisor regressions: every process, identity and signal is synthetic."""

from __future__ import annotations

import importlib.util
import json
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/gitlab_network_proof_launcher.py"
SPEC = importlib.util.spec_from_file_location("gitlab_network_proof_launcher", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
launcher = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(launcher)


@pytest.mark.parametrize("leader_alive", [False, True])
def test_stop_owned_reaps_retained_group_after_leader_exit(
    monkeypatch: pytest.MonkeyPatch, leader_alive: bool
) -> None:
    living = {101: "helper-born"}
    if leader_alive:
        living[100] = "leader-born"
    signals: list[int] = []

    def send(pgid: int, sig: int) -> None:
        assert pgid == 100
        signals.append(sig)
        living.clear()

    def fake_run(*args: Any, **kwargs: Any) -> Any:
        return subprocess.CompletedProcess(
            args, 0, "".join(f"{pid} {100 if pid == 101 else 1} 100\n" for pid in living)
        )

    monkeypatch.setattr(launcher, "identity", lambda pid, deadline=None: living.get(pid, ""))
    monkeypatch.setattr(launcher.subprocess, "run", fake_run)
    monkeypatch.setattr(launcher.os, "getpgid", lambda pid: 100)
    monkeypatch.setattr(launcher.os, "killpg", send)
    launcher.stop_owned(
        {
            "pid": 100,
            "pgid": 100,
            "identity": "leader-born",
            "members": {"100": "leader-born", "101": "helper-born"},
        }
    )
    assert signals == [signal.SIGINT]
    assert not living


def test_stop_owned_refuses_reused_member_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        launcher, "identity", lambda pid, deadline=None: "reused" if pid == 101 else ""
    )
    monkeypatch.setattr(
        launcher.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, "101 1 100\n")
    )
    monkeypatch.setattr(launcher.os, "getpgid", lambda pid: 100)
    monkeypatch.setattr(
        launcher.os, "killpg", lambda *a: pytest.fail("must not signal reused group")
    )
    with pytest.raises(RuntimeError, match="identity"):
        launcher.stop_owned(
            {"pid": 100, "pgid": 100, "identity": "leader", "members": {"101": "original"}}
        )


@pytest.mark.parametrize("marker_is_file", [False, True])
def test_main_rejects_scratch_in_any_git_tree_before_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, marker_is_file: bool
) -> None:
    checkout = tmp_path / "other-checkout"
    checkout.mkdir()
    marker = checkout / ".git"
    if marker_is_file:
        marker.write_text("gitdir: /synthetic/worktree\n")
    else:
        marker.mkdir()
    scratch = checkout / "private-proof"
    monkeypatch.setattr(
        sys,
        "argv",
        ["launcher", "--scratch", str(scratch), "--source-manifest", str(tmp_path / "scope.json")],
    )
    with pytest.raises((AssertionError, ValueError), match="outside.*Git"):
        launcher.main()
    assert not scratch.exists()


@pytest.mark.parametrize(
    ("runtime_survives", "audit_seconds", "fault"),
    [
        (False, 0.0, None),
        (True, 0.0, None),
        (False, 6.0, None),
        (False, 0.0, "runtime-identity"),
        (False, 0.0, "setup-identity"),
        (False, 0.0, "audit-interrupt"),
        (False, 0.0, "audit-before-parent"),
    ],
)
def test_main_keeps_timeout_and_audits_only_after_actual_exit_despite_peer_cleanup_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    runtime_survives: bool,
    audit_seconds: float,
    fault: str | None,
) -> None:
    scratch = tmp_path / "private"
    scope = tmp_path / "scope.json"
    scope.write_text(json.dumps({"checkout": str(launcher.ROOT), "hashes": {}}))
    monkeypatch.setattr(
        sys, "argv", ["launcher", "--scratch", str(scratch), "--source-manifest", str(scope)]
    )
    monkeypatch.setenv("TEST_DATABASE_URL", "postgresql://synthetic@127.0.0.1:49323/dmc169_test")
    monkeypatch.setenv("TEST_RABBITMQ_URL", "amqp://synthetic@127.0.0.1:58447/dmc169-test")
    now = 0.0
    fault_seen = False
    audit_pid = 0
    spawned: list[str] = []
    living: dict[int, str] = {200: "changed-peer"}
    processes: dict[int, Any] = {}
    groups: dict[int, int] = {}
    audit_module: Any = None

    class AuditReached(Exception):
        pass

    if fault == "audit-before-parent":
        monkeypatch.setitem(sys.modules, "gitlab_network_proof_launcher", launcher)
        audit_spec = importlib.util.spec_from_file_location(
            "offline_audit", SCRIPT.with_name("gitlab_network_proof_audit.py")
        )
        assert audit_spec is not None and audit_spec.loader is not None
        audit_module = importlib.util.module_from_spec(audit_spec)
        audit_spec.loader.exec_module(audit_module)

    class FakeProcess:
        def __init__(self, argv: list[str], **kwargs: Any) -> None:
            nonlocal audit_pid
            self.role = next((Path(arg).name for arg in argv if arg.endswith(".py")), "openssl")
            if argv[0] == "ps":
                self.role = "ps"
            self.pid = 100 + len(spawned)
            groups[self.pid] = self.pid if kwargs.get("start_new_session") else audit_pid
            if self.role == "gitlab_network_proof_audit.py":
                audit_pid = self.pid
            self.started = now
            self.finished: float | None = None
            self.returncode: int | None = None
            spawned.append(self.role)
            living[self.pid] = f"born-{self.pid}"
            processes[self.pid] = self
            if self.role == "gitlab_network_proof.py":
                launcher.write_private(
                    scratch / "processes.json",
                    [{"pid": 200, "pgid": 200, "identity": "original-peer"}],
                )
                launcher.write_private(scratch / "results.json", {"schema": "synthetic"})
            if self.role == "gitlab_network_proof_audit.py" and fault == "audit-before-parent":
                assert len(json.loads((scratch / "command.json").read_text())["children"]) == 1
                child_argv = argv[
                    next(
                        index
                        for index, value in enumerate(argv)
                        if value.endswith("gitlab_network_proof_audit.py")
                    ) :
                ]
                with monkeypatch.context() as child_patch:
                    child_patch.setattr(sys, "argv", child_argv)
                    child_patch.setattr(audit_module.os, "getpid", lambda: self.pid)
                    child_patch.setattr(audit_module.os, "getpgrp", lambda: self.pid)
                    child_patch.setattr(
                        audit_module.socket,
                        "socket",
                        lambda *a, **k: pytest.fail("no socket allowed"),
                    )
                    child_patch.setattr(
                        audit_module.psycopg,
                        "connect",
                        lambda *a, **k: pytest.fail("no DB allowed"),
                    )
                    try:
                        audit_module.main()
                    except AuditReached:
                        self.consumer_observed = True
                    else:
                        pytest.fail("expected to stop after synthetic observer metadata")

        def communicate(self, timeout: float) -> tuple[bytes, bytes]:
            self.returncode = 0
            if self.role == "ps" and fault == "audit-before-parent":
                living.pop(self.pid, None)
                raise AuditReached("metadata consumer reached before parent registration")
            living.pop(self.pid, None)
            return b"", b""

        def poll(self) -> int | None:
            return self.returncode

        def wait(self, timeout: float) -> int:
            nonlocal now, fault_seen
            if self.role == "gitlab_network_proof.py" and self.returncode is None:
                now += timeout
                raise subprocess.TimeoutExpired(self.role, timeout)
            if self.role == "gitlab_network_proof_audit.py" and self.returncode is None:
                if fault == "audit-interrupt" and not fault_seen:
                    fault_seen = True
                    living[300] = "born-300"
                    launcher.write_private(
                        scratch / "audit-processes.json",
                        [
                            {"pid": 300, "pgid": self.pid, "identity": "born-300"},
                        ],
                    )
                    raise KeyboardInterrupt("synthetic audit interruption")
                now += min(timeout, max(0.0, self.started + audit_seconds - now))
                if now < self.started + audit_seconds:
                    raise subprocess.TimeoutExpired(self.role, timeout)
                self.finished = now
                launcher.write_private(
                    scratch / "final-independent-cleanup.json",
                    {
                        "groups": {},
                        "no_owned_runtime_work_remains": True,
                    },
                )
            self.returncode = self.returncode or 0
            living.pop(self.pid, None)
            return self.returncode

    def send(pgid: int, sig: int) -> None:
        if pgid == 200:
            pytest.fail("must not signal identity-mismatched peer")
        if not runtime_survives:
            if processes[pgid].returncode is None:
                processes[pgid].returncode = -sig
            living.pop(pgid, None)

    def advance(seconds: float) -> None:
        nonlocal now
        now += seconds

    def fake_identity(pid: int, deadline: float | None = None) -> str:
        nonlocal fault_seen
        role = processes[pid].role if pid in processes else "helper"
        selected = (fault == "runtime-identity" and role == "gitlab_network_proof.py") or (
            fault == "setup-identity" and role == "openssl"
        )
        if selected and not fault_seen:
            fault_seen = True
            raise subprocess.TimeoutExpired("synthetic initial identity", 2)
        return living.get(pid, "")

    monkeypatch.setattr(launcher.subprocess, "Popen", FakeProcess)
    monkeypatch.setattr(
        launcher.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(
            a,
            0,
            "".join(
                f"{pid} "
                f"{audit_pid if pid == 300 or groups.get(pid, pid) != pid else 1} "
                f"{audit_pid if pid == 300 else groups.get(pid, pid)}\n"
                for pid in living
            ),
        ),
    )
    monkeypatch.setattr(launcher, "identity", fake_identity)
    monkeypatch.setattr(
        launcher.os, "getpgid", lambda pid: audit_pid if pid == 300 else groups.get(pid, pid)
    )
    monkeypatch.setattr(launcher.os, "killpg", send)
    monkeypatch.setattr(launcher.time, "monotonic", lambda: now)
    monkeypatch.setattr(launcher.time, "sleep", advance)
    if fault == "setup-identity":
        with pytest.raises(subprocess.TimeoutExpired):
            launcher.main()
        setup = json.loads((scratch / "setup-processes.json").read_text())
        assert setup[0]["exit"] == -signal.SIGINT and setup[0]["group_empty"]
        assert not spawned.count("gitlab_network_proof.py")
        return
    try:
        outcome = launcher.main()
    except BaseException as error:
        pytest.fail(f"registered child cleanup escaped: {type(error).__name__}")
    assert outcome == 124
    command = json.loads((scratch / "command.json").read_text())
    child = command["children"][0]
    assert child["timeout"] is True
    assert child["cleanup_errors"]
    assert command["runtime_actual_end"] is not runtime_survives
    assert spawned.count("gitlab_network_proof_audit.py") == (0 if runtime_survives else 1)
    if runtime_survives:
        assert "actual_end" not in child and "actual_end" not in command
        assert child["exit"] is None
    else:
        assert child["exit"] == -signal.SIGINT
        assert command["runtime_exit"] == 124
        audit = command["children"][1]
        if fault == "audit-interrupt":
            assert command["audit_exit"] == 130
            assert audit["exit"] == -signal.SIGINT and not audit["group_empty"]
            assert "actual_end" not in command
            assert 300 in living
            return
        assert command["audit_exit"] == 0
        assert "actual_end" in command
        assert audit["exit"] == 0 and not audit.get("timeout", False)
        assert audit["timeout_seconds"] == 30 and audit["cleanup_reserve_seconds"] == 5
        observed = processes[audit["pid"]]
        if fault == "audit-before-parent":
            assert observed.consumer_observed
        assert observed.finished - observed.started == pytest.approx(audit_seconds)


def test_stop_owned_escalates_for_helper_after_parent_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    living = {100: "leader", 101: "helper"}
    signals: list[int] = []

    class Parent:
        def poll(self) -> int | None:
            return None if 100 in living else 0

        def wait(self, timeout: float) -> int:
            living.pop(100, None)
            return 0

    def send(pgid: int, sig: int) -> None:
        assert pgid == 100
        signals.append(sig)
        living.pop(100, None)
        if sig == signal.SIGTERM:
            living.clear()

    clock = iter(n / 10 for n in range(10000))
    monkeypatch.setattr(launcher, "identity", lambda pid, deadline=None: living.get(pid, ""))
    monkeypatch.setattr(
        launcher.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(
            a, 0, "".join(f"{pid} {100 if pid == 101 else 1} 100\n" for pid in living)
        ),
    )
    monkeypatch.setattr(launcher.os, "getpgid", lambda pid: 100)
    monkeypatch.setattr(launcher.os, "killpg", send)
    monkeypatch.setattr(launcher.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(launcher.time, "sleep", lambda seconds: None)
    launcher.stop_owned(
        {"pid": 100, "pgid": 100, "identity": "leader", "members": {100: "leader", 101: "helper"}},
        Parent(),
    )
    assert signals == [signal.SIGINT, signal.SIGTERM]
    assert not living


def test_startup_timeout_reaps_attributable_unready_helper(monkeypatch: pytest.MonkeyPatch) -> None:
    living = {100: "leader", 101: "original-child"}
    signalled: list[int] = []

    def send(pgid: int, sig: int) -> None:
        signalled.append(pgid)
        living.clear()

    monkeypatch.setattr(launcher, "identity", lambda pid, deadline=None: living.get(pid, ""))
    monkeypatch.setattr(
        launcher.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(
            a, 0, "100 1 100\n101 100 100\n" if living else ""
        ),
    )
    monkeypatch.setattr(launcher.os, "getpgid", lambda pid: 100)
    monkeypatch.setattr(launcher.os, "killpg", send)
    record: dict[str, Any] = {"pid": 100, "pgid": 100, "identity": "leader"}
    launcher.stop_owned(record)
    assert signalled == [100] and not living
    assert record["members"][101] == "original-child"


def test_delayed_probe_exhausts_cleanup_budget_before_any_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 0.0
    signalled: list[int] = []

    def delayed_probe(*args: Any, **kwargs: Any) -> Any:
        nonlocal now
        assert kwargs["timeout"] <= 2
        now += 30
        return subprocess.CompletedProcess(args, 0, "100 1 100\n")

    monkeypatch.setattr(launcher.time, "monotonic", lambda: now)
    monkeypatch.setattr(launcher.subprocess, "run", delayed_probe)
    monkeypatch.setattr(launcher, "identity", lambda pid, deadline=None: "leader")
    monkeypatch.setattr(launcher.os, "getpgid", lambda pid: 100)
    monkeypatch.setattr(launcher.os, "killpg", lambda pgid, sig: signalled.append(pgid))
    with pytest.raises(TimeoutError):
        launcher.stop_owned({"pid": 100, "pgid": 100, "identity": "leader"})
    assert not signalled


def test_write_private_reader_sees_complete_snapshot_during_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "command.json"
    old = {"children": [{"pid": 100}], "runtime_actual_end": True}
    new = {"children": [{"pid": 100}, {"pid": 200}], "runtime_actual_end": True}
    target.write_text(json.dumps(old))
    snapshots: list[str] = []

    def scheduled_write(path: Path, text: str, *args: Any, **kwargs: Any) -> int:
        with path.open("w") as stream:
            split = len(text) // 2
            stream.write(text[:split])
            stream.flush()
            snapshots.append(target.read_text())
            stream.write(text[split:])
        return len(text)

    monkeypatch.setattr(Path, "write_text", scheduled_write)
    launcher.write_private(target, new)
    assert snapshots and all(json.loads(value) == old for value in snapshots)
    assert json.loads(target.read_text()) == new
    assert target.stat().st_mode & 0o777 == 0o600
