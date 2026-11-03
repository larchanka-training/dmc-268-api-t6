"""CI auto rollback of a failed staging deploy and its diagnostics (#35)."""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Mapping
from pathlib import Path

import pytest

from tests.deploy_host_fakes import (
    CI_SECRETS,
    IMAGE_A,
    IMAGE_B,
    IMAGE_BAD,
    IMAGE_C,
    WORKFLOW,
    Host,
    _bundle,
    _mode,
    _read,
    _read_dotenv,
    _release_unchanged_literal,
    _run_script,
    _workflow_step,
)
from tests.deploy_host_fakes import host as _host_fixture

host = _host_fixture

# rollback.sh's answer to a CI auto rollback whose deploy run never reached the release switch.
STOPPED_BEFORE_SWITCH = (
    "release unchanged: deploy run {} stopped before the release switch; skipping auto rollback\n"
)


def _ci_auto_rollback(
    host: Host,
    deploy_run: str,
    *,
    deploy_mode: str = "edge",
    stubs: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """The CI "Rollback on failed deploy or health check" step of the run `deploy_run`.

    Like ssh-action, only the names in the step's ``envs:`` reach the host.
    """
    step = _workflow_step("Rollback on failed deploy or health check")
    envs = re.search(r"^\s+envs: (\S+)$", step, re.MULTILINE)
    assert envs is not None
    assert 'ROLLBACK_MODE=auto "${APP_DIR}/rollback.sh"' in step
    values = {
        "GHCR_TOKEN": "test-ghcr-token",
        "GHCR_USER": "github",
        "APP_DIR": str(host.app_dir),
        "DEPLOY_MODE": deploy_mode,
        "EDGE_ALIAS": "api-staging",
        "DEPLOY_RUN": deploy_run,
    }
    forwarded = {name: values[name] for name in envs.group(1).split(",")}
    return host.run("rollback.sh", env={**forwarded, "ROLLBACK_MODE": "auto", **(stubs or {})})


@pytest.mark.parametrize("unchanged", [False, True], ids=["rolled-back", "release-unchanged"])
@pytest.mark.parametrize(
    ("deploy", "health", "rollback"),
    [
        ("failure", "skipped", "failure"),
        ("success", "failure", "failure"),
        ("failure", "skipped", "success"),
        ("success", "failure", "success"),
    ],
)
def test_failed_forward_deploy_never_promotes_and_reports_failed_auto_rollback(
    deploy: str, health: str, rollback: str, unchanged: bool, tmp_path: Path
) -> None:
    values = {"deploy": deploy, "health": health, "rollback": rollback}

    def render(script: str) -> str:
        for step, outcome in values.items():
            script = script.replace("${{ steps." + step + ".outcome }}", outcome)
        return script

    output = tmp_path / "github-output"
    environment = {"PATH": os.environ["PATH"], "GITHUB_OUTPUT": str(output)}
    promote = subprocess.run(
        [
            "bash",
            "--noprofile",
            "--norc",
            "-eo",
            "pipefail",
            "-c",
            render(_run_script(_workflow_step("Set promote flag"))),
        ],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert promote.returncode == 0
    assert output.read_text() == "promote=false\n"
    # Stdout captured by the Rollback step: rollback.sh's answer when deploy.sh of this run
    # stopped before the release switch, or the line of a real rollback.
    stdout = STOPPED_BEFORE_SWITCH.format("101-1") if unchanged else f"rolled back to {IMAGE_A}\n"
    # GitHub renders the env expression to "true" / "false" (contains() ignores case), so the
    # raw stdout never enters the script.
    release_unchanged = str(_release_unchanged_literal().lower() in stdout.lower()).lower()
    fail_script = _run_script(_workflow_step("Fail the pipeline after rollback"))
    assert "outputs.stdout" not in fail_script
    failed = subprocess.run(
        [
            "bash",
            "--noprofile",
            "--norc",
            "-eo",
            "pipefail",
            "-c",
            render(fail_script),
        ],
        env={**environment, "RELEASE_UNCHANGED": release_unchanged},
        capture_output=True,
        text=True,
        check=False,
    )
    assert failed.returncode == 1
    assert ("rollback failed" in failed.stderr) is (rollback == "failure")
    if deploy == "failure" and rollback == "success" and unchanged:
        message = (
            "staging deploy failed before the release switch; "
            "the running release was left unchanged"
        )
    elif deploy == "failure":
        message = "staging deploy failed; rollback was triggered"
    else:
        message = "staging health check failed; rollback was triggered"
    assert failed.stdout == message + "\n"
    auto = _workflow_step("Rollback on failed deploy or health check")
    assert "ROLLBACK_MODE=auto" in auto
    assert "\n          capture_stdout: true\n" in auto
    assert (
        "if: always() && (steps.deploy.outcome == 'failure' || steps.health.outcome == 'failure')"
        in auto
    )
    promotion = WORKFLOW.read_text().split("  promote-staging:", 1)[1]
    assert "needs.deploy-staging.outputs.promote == 'true'" in promotion


def test_fail_step_looks_for_the_release_unchanged_line_of_rollback_sh() -> None:
    # The Fail step tells an unchanged release apart by this text; rollback.sh must print it.
    literal = _release_unchanged_literal()
    echoes = [
        line
        for line in _read("deploy", "scripts", "rollback.sh").splitlines()
        if line.lstrip().startswith("echo ") and literal in line
    ]
    assert len(echoes) == 1, echoes
    assert "skipping auto rollback" in echoes[0]
    assert literal in STOPPED_BEFORE_SWITCH


def test_deploy_and_auto_rollback_steps_share_the_deploy_run() -> None:
    # The auto rollback undoes a deploy only once deploy.sh of the same run reached the switch.
    run_id = "          DEPLOY_RUN: ${{ github.run_id }}-${{ github.run_attempt }}\n"
    for name in ("Deploy image", "Rollback on failed deploy or health check"):
        step = _workflow_step(name)
        envs = re.search(r"^\s+envs: (\S+)$", step, re.MULTILINE)
        assert run_id in step, name
        assert envs is not None
        assert "DEPLOY_RUN" in envs.group(1).split(","), name


def test_failed_deploy_then_refused_auto_rollback_preserves_rollback_entry(host: Host) -> None:
    assert host.deploy(IMAGE_A, bundle=_bundle(CI_SECRETS)).returncode == 0
    assert host.deploy(IMAGE_B).returncode == 0
    entry = host.log.parent / "rollback-entry"
    calls_before = len(host.calls())
    result = host.deploy(
        IMAGE_BAD,
        env={
            "STUB_FAILING_IMAGE": IMAGE_BAD,
            "STUB_ROLLBACK_ENTRY": str(entry),
            "STUB_PROBE_STATUS": "1",
            "STUB_PROBE_OUTPUT": "rollback revision check: refused: database revision is unknown",
            "STUB_PROBE_STDERR": "Container Creating\nSECRET-CANARY",
        },
    )
    assert result.returncode != 0
    assert "compose up failed; rolling back" in result.stderr
    assert "rollback refused:" in result.stderr and "database revision is unknown" in result.stderr
    assert "SECRET-CANARY" not in result.stdout + result.stderr
    assert "rolled back" not in result.stdout and "environment keys:" not in result.stdout
    assert host.files() == {p.name: p.read_bytes() for p in entry.iterdir()}
    assert {p.name: _mode(p) for p in host.app_dir.iterdir()} == {
        p.name: _mode(p) for p in entry.iterdir()
    }
    calls = host.calls()[calls_before:]
    pull = calls.index(f"pull {IMAGE_B}")
    assert not any(set(c.split()) & {"up", "rm", "down", "exec"} for c in calls[pull:])
    assert list(host.tmp.iterdir()) == []


# CI runs deploy A, B and C as runs 101-1, 102-1 and 103-1 (DEPLOY_RUN); in the re-run case C is
# attempt 2 of B's run 102, whose attempt 1 crossed the switch. The DB probe stays compatible
# throughout: guard #126 never decides these cases.
@pytest.mark.parametrize(
    ("marked", "attempt"),
    [(True, "103-1"), (True, "102-2"), (False, "103-1")],
    ids=["marked-host", "re-run-attempt", "legacy-host"],
)
@pytest.mark.parametrize(
    ("deploy_mode", "bundle", "stubs", "reason"),
    [
        ("edge", _bundle({"PATH": "/tmp"}), {}, "PATH is not in the allowlist"),
        ("bogus", None, {}, "DEPLOY_MODE must be ports or edge, got: bogus"),
        (
            "edge",
            None,
            {"GHCR_TOKEN": "test-ghcr-token", "STUB_LOGIN_FAILURE": "registry login denied"},
            "registry login failed",
        ),
    ],
    ids=["invalid-bundle", "bad-deploy-mode", "registry-login"],
)
def test_ci_auto_rollback_keeps_the_release_when_deploy_stops_before_the_switch(
    host: Host,
    marked: bool,
    attempt: str,
    deploy_mode: str,
    bundle: str | None,
    stubs: dict[str, str],
    reason: str,
) -> None:
    # A legacy host was deployed before DEPLOY_RUN existed and has no switch marker yet.
    runs = ({"DEPLOY_RUN": "101-1"}, {"DEPLOY_RUN": "102-1"}) if marked else ({}, {})
    assert host.deploy(IMAGE_A, bundle=_bundle(CI_SECRETS), env=runs[0]).returncode == 0
    assert host.deploy(IMAGE_B, env=runs[1]).returncode == 0
    before = host.files()
    calls_before = len(host.calls())

    deploy = host.deploy(
        IMAGE_C, bundle=bundle, env={"DEPLOY_MODE": deploy_mode, "DEPLOY_RUN": attempt, **stubs}
    )
    assert deploy.returncode != 0
    assert reason in deploy.stderr
    rollback_calls = len(host.calls())
    result = _ci_auto_rollback(host, attempt, deploy_mode=deploy_mode)

    assert result.returncode == 0, result.stderr
    assert result.stdout == STOPPED_BEFORE_SWITCH.format(attempt)
    assert host.calls()[rollback_calls:] == []
    calls = host.calls()[calls_before:]
    assert not any(set(c.split()) & {"pull", "run", "up", "down", "rm"} for c in calls)
    # Pre-switch refusal leaves the previous-release record untouched too.
    assert host.files() == before
    state = _read_dotenv(host.app_dir / ".deploy-state")
    assert state["current_image"] == IMAGE_B and "rolled_back" not in state
    assert list(host.tmp.iterdir()) == []


def test_ci_auto_rollback_keeps_the_only_release_when_deploy_stops_before_the_switch(
    host: Host,
) -> None:
    first = host.deploy(IMAGE_A, bundle=_bundle(CI_SECRETS), env={"DEPLOY_RUN": "101-1"})
    assert first.returncode == 0
    before = host.files()
    assert ".deploy-state" in before and ".deploy-state.previous" not in before
    calls_before = len(host.calls())

    deploy = host.deploy(IMAGE_C, bundle=_bundle({"PATH": "/tmp"}), env={"DEPLOY_RUN": "102-1"})
    assert deploy.returncode != 0
    assert "PATH is not in the allowlist" in deploy.stderr
    result = _ci_auto_rollback(host, "102-1")

    assert result.returncode == 0, result.stderr
    assert result.stdout == STOPPED_BEFORE_SWITCH.format("102-1")
    # No compose down and no bootstrap container: the running release A stays.
    calls = host.calls()[calls_before:]
    assert not any(set(c.split()) & {"pull", "run", "up", "down", "rm"} for c in calls)
    assert host.files() == before


def test_failed_stack_start_rolls_back_once_and_the_ci_rollback_is_idempotent(host: Host) -> None:
    assert host.deploy(IMAGE_A, bundle="", env={"DEPLOY_RUN": "101-1"}).returncode == 0
    assert host.deploy(IMAGE_B, env={"DEPLOY_RUN": "102-1"}).returncode == 0
    state_b = (host.app_dir / ".deploy-state").read_bytes()

    deploy = host.deploy(IMAGE_C, env={"DEPLOY_RUN": "103-1", "STUB_FAILING_IMAGE": IMAGE_C})

    assert deploy.returncode != 0
    assert "compose up failed; rolling back" in deploy.stderr
    assert f"rolled back to {IMAGE_B}" in deploy.stdout
    after_deploy = host.files()
    state_after_deploy = _read_dotenv(host.app_dir / ".deploy-state")
    assert state_after_deploy["current_image"] == IMAGE_B
    assert state_after_deploy["rolled_back"] == "true"

    result = _ci_auto_rollback(host, "103-1")

    assert result.returncode == 0, result.stderr
    assert f"rolled back to {IMAGE_B}" in result.stdout
    state = _read_dotenv(host.app_dir / ".deploy-state")
    assert state == {**state_after_deploy, "deployed_at": state["deployed_at"]}
    assert _read_dotenv(host.env_file)["IMAGE"] == IMAGE_B
    assert (host.app_dir / ".deploy-state.previous").read_bytes() == state_b
    after = host.files()
    del after[".deploy-state"], after_deploy[".deploy-state"]
    assert after == after_deploy


def test_ci_auto_rollback_after_a_failed_image_pull_keeps_the_working_release(host: Host) -> None:
    assert host.deploy(IMAGE_A, bundle="", env={"DEPLOY_RUN": "101-1"}).returncode == 0
    assert host.deploy(IMAGE_B, env={"DEPLOY_RUN": "102-1"}).returncode == 0
    before = host.files()
    calls_before = len(host.calls())
    deploy = host.deploy(IMAGE_C, env={"DEPLOY_RUN": "103-1", "STUB_PULL_FAILURE": IMAGE_C})
    assert deploy.returncode != 0
    assert f"pull {IMAGE_C}" in host.calls()[calls_before:]
    assert host.files() == before
    rollback_calls = len(host.calls())
    result = _ci_auto_rollback(host, "103-1")
    assert result.returncode == 0, result.stderr
    assert result.stdout == STOPPED_BEFORE_SWITCH.format("103-1")
    assert host.calls()[rollback_calls:] == []
    assert host.files() == before


def test_ci_auto_rollback_after_a_failed_health_check_restores_the_previous_release(
    host: Host,
) -> None:
    assert host.deploy(IMAGE_A, bundle="", env={"DEPLOY_RUN": "101-1"}).returncode == 0
    assert host.deploy(IMAGE_B, env={"DEPLOY_RUN": "102-1"}).returncode == 0
    assert host.deploy(IMAGE_C, env={"DEPLOY_RUN": "103-1"}).returncode == 0
    previous = (host.app_dir / ".deploy-state.previous").read_bytes()
    calls_before = len(host.calls())

    result = _ci_auto_rollback(host, "103-1")  # the health check of run 103-1 failed

    assert result.returncode == 0, result.stderr
    assert f"rolled back to {IMAGE_B}" in result.stdout
    calls = host.calls()[calls_before:]
    pull = calls.index(f"pull {IMAGE_B}")
    probe = next(i for i, call in enumerate(calls) if " run " in call)
    up = next(i for i, call in enumerate(calls) if " up " in call)
    assert pull < probe < up
    assert _read_dotenv(host.env_file)["IMAGE"] == IMAGE_B
    state = _read_dotenv(host.app_dir / ".deploy-state")
    assert state["current_image"] == IMAGE_B and state["rolled_back"] == "true"
    assert (host.app_dir / ".deploy-state.previous").read_bytes() == previous


def test_ci_auto_rollback_after_a_failed_health_check_keeps_the_revision_guard(
    host: Host,
) -> None:
    assert host.deploy(IMAGE_A, bundle="", env={"DEPLOY_RUN": "101-1"}).returncode == 0
    assert host.deploy(IMAGE_B, env={"DEPLOY_RUN": "102-1"}).returncode == 0
    assert host.deploy(IMAGE_C, env={"DEPLOY_RUN": "103-1"}).returncode == 0
    before = host.files()
    calls_before = len(host.calls())

    result = _ci_auto_rollback(
        host,
        "103-1",
        stubs={
            "STUB_PROBE_STATUS": "1",
            "STUB_PROBE_OUTPUT": "rollback revision check: refused: database revision is unknown",
        },
    )

    assert result.returncode != 0
    assert "rollback refused:" in result.stderr
    assert "database revision is unknown" in result.stderr
    assert "rolled back" not in result.stdout
    assert host.files() == before
    assert not any(" up " in call for call in host.calls()[calls_before:])


# The gate is for CI auto rollbacks only: a DEPLOY_RUN in an operator's shell (or a future
# rollback.yml) with no matching switch marker must not turn a manual rollback into a no-op.
@pytest.mark.parametrize("mode", [{"ROLLBACK_MODE": "manual"}, {}], ids=["explicit", "default"])
def test_manual_rollback_ignores_the_deploy_run_and_restores_the_previous_release(
    host: Host, mode: dict[str, str]
) -> None:
    assert host.deploy(IMAGE_A, bundle="", env={"DEPLOY_RUN": "101-1"}).returncode == 0
    assert host.deploy(IMAGE_B, env={"DEPLOY_RUN": "102-1"}).returncode == 0
    state_b = (host.app_dir / ".deploy-state").read_bytes()

    result = host.run("rollback.sh", env={**mode, "DEPLOY_RUN": "999-1"})

    assert result.returncode == 0, result.stderr
    assert f"rolled back to {IMAGE_A}" in result.stdout
    assert STOPPED_BEFORE_SWITCH.format("999-1") not in result.stdout
    assert _read_dotenv(host.env_file)["IMAGE"] == IMAGE_A
    state = _read_dotenv(host.app_dir / ".deploy-state")
    assert state["current_image"] == IMAGE_A and state["rolled_back"] == "true"
    assert (host.app_dir / ".deploy-state.previous").read_bytes() == state_b


@pytest.mark.parametrize("mode", ["manual", "auto"])
def test_environment_key_protocol_ignores_secret_bearing_exec_stderr(host: Host, mode: str) -> None:
    assert host.deploy(IMAGE_A, bundle="").returncode == 0
    assert host.deploy(IMAGE_B).returncode == 0
    result = host.run(
        "rollback.sh",
        env={"ROLLBACK_MODE": mode, "STUB_EXEC_STDERR": "Compose progress\nEXEC-SECRET-CANARY"},
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.count("environment keys:") == 3
    assert f"rolled back to {IMAGE_A}" in result.stdout
    assert "Compose progress" not in result.stdout + result.stderr
    assert "EXEC-SECRET-CANARY" not in result.stdout + result.stderr


@pytest.mark.parametrize(
    "status, output", [(1, '["A_KEY"]'), (0, '["A_KEY"]\n'), (0, '["A_KEY"]\n[]')]
)
def test_environment_key_protocol_rejects_failed_exit_and_extra_stdout(
    host: Host, status: int, output: str
) -> None:
    assert host.deploy(IMAGE_A, bundle="").returncode == 0
    assert host.deploy(IMAGE_B).returncode == 0
    result = host.run(
        "rollback.sh",
        env={
            "STUB_EXEC_FAILURE_SERVICE": "api",
            "STUB_EXEC_STATUS": str(status),
            "STUB_EXEC_ERROR": output,
        },
    )
    assert result.returncode == 1
    assert "api environment key diagnostics failed" in result.stderr
    assert "environment keys:" not in result.stdout
    assert "rolled back" not in result.stdout
    assert _read_dotenv(host.app_dir / ".deploy-state")["current_image"] == IMAGE_A


@pytest.mark.parametrize("status", [1, 125, 126, 127])
def test_generic_probe_failure_reports_only_controlled_numeric_exit(
    host: Host, status: int
) -> None:
    assert host.deploy(IMAGE_A, bundle="").returncode == 0
    assert host.deploy(IMAGE_B).returncode == 0
    before = host.files()
    result = host.run(
        "rollback.sh",
        env={
            "STUB_PROBE_STATUS": str(status),
            "STUB_PROBE_OUTPUT": "PROBE-SECRET-CANARY",
            "STUB_PROBE_STDERR": "DRIVER-SECRET-CANARY",
        },
    )
    assert result.returncode == 1
    assert f"rollback refused: target revision probe failed (exit {status})\n" in result.stderr
    assert "PROBE-SECRET-CANARY" not in result.stdout + result.stderr
    assert "DRIVER-SECRET-CANARY" not in result.stdout + result.stderr
    assert host.files() == before
