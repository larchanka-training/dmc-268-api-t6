"""The Deploy prod workflow: isolation evidence and the run report (#308 Part 1)."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import pytest

from tests.deploy_host_fakes import PROD_PROJECT, _release_unchanged_literal
from tests.deploy_prod_workflow_fakes import (
    AFTER,
    FAIL,
    HEALTH,
    ISOLATION,
    REF_A,
    REF_B,
    REPO_ROOT,
    REPORT,
    ROLLBACK,
    STAGING_IDS,
    STAGING_PS,
    _docker_calls,
    _forwarded,
    _job,
    _run,
    _ssh_script,
    _step,
    _step_names,
)
from tests.monitoring_acceptance_workflow import DRONE_SSH_BANNER
from tests.test_check_isolation import (
    FAKE_DOCKER,
    INSPECT_FORMAT,
    NETWORK_FORMAT,
    STAGING,
    _find,
    _host,
    _join,
)

# --- evidence in the run summary: isolated stores, staging untouched (AC1, AC3) ----------------

ISOLATED = "isolation: pass (6 stores on their own network only, no host ports)"
ISOLATION_PASS = (
    "| container | networks | host ports | verdict |\n"
    "|---|---|---|---|\n"
    f"| {PROD_PROJECT}-postgres-1 | {PROD_PROJECT}_default | none | ok |\n"
    "\n"
    f"{ISOLATED}\n"
    "check-isolation exit: 0\n"
)
NOT_RUN = "not run: the check runs only after a passing health check"
STOPPED_OUTCOME = (
    "stopped: no earlier prod release, the edge proxy answers 503 for the prod API host"
)
UNCHANGED_OUTCOME = (
    "deploy failed before the release switch; the running release was left unchanged"
)
CANCELLED_OUTCOME = "cancelled during the deploy; no rollback ran, prod may be half-switched"


def _listed(when: str, ids: Iterable[str]) -> str:
    """The line Prepare host ("before") or the host read after the deploy ("after") prints."""
    return f"staging containers {when}:" + "".join(f" {container}" for container in ids)


def _report(tmp_path: Path, **captured: str) -> tuple[subprocess.CompletedProcess[str], str]:
    """The report step of a deployed run; `captured` replaces outcomes or stdouts.

    Every stdout ends with the drone-ssh banner, as ssh-action hands it over.
    """
    env = {
        "DEPLOY_OUTCOME": "success",
        "HEALTH_OUTCOME": "success",
        "ROLLBACK_OUTCOME": "skipped",
        "RELEASE_UNCHANGED": "false",
        "ISOLATION_OUTCOME": "success",
        "ISOLATION_STDOUT": ISOLATION_PASS + DRONE_SSH_BANNER,
        "BEFORE_STDOUT": "provisioned dmc268-edge\n"
        + _listed("before", STAGING_IDS)
        + "\n"
        + DRONE_SSH_BANNER,
        "AFTER_OUTCOME": "success",
        "AFTER_STDOUT": f"prod current image: {REF_A}\n"
        + _listed("after", reversed(STAGING_IDS))
        + "\n"
        + DRONE_SSH_BANNER,
        **captured,
    }
    result, output, summary = _run(REPORT, tmp_path, env)
    assert output == ""
    # Only the prefixed lines of the scripts reach the summary.
    for noise in ("Successfully executed", "=====", "provisioned", "check-isolation exit"):
        assert noise not in summary, noise
    return result, summary


def test_the_isolation_check_runs_only_while_the_new_release_is_healthy() -> None:
    # After a failed health check the rollback restores the previous release or stops the prod
    # stack, whose stores are then gone: the check runs on the success path only.
    step = _step(ISOLATION)
    assert (
        "        id: isolation\n"
        "        if: steps.health.outcome == 'success'\n"
        "        continue-on-error: true\n"
    ) in step
    assert "          capture_stdout: true\n" in step
    assert _forwarded(ISOLATION, {"steps.target.outputs.app_dir": "/opt/x"}) == {
        "APP_DIR": "/opt/x"
    }
    order = _step_names(_job("deploy-prod"))
    assert order.index(HEALTH) < order.index(ISOLATION) < order.index(ROLLBACK)


def test_the_staging_list_and_the_report_follow_every_path_that_reached_the_host() -> None:
    # Deployed, rolled back or stopped: whenever Prepare host listed the staging containers.
    after = _step(AFTER)
    assert (
        "        id: after\n"
        "        if: always() && steps.prepare.outcome == 'success'\n"
        "        continue-on-error: true\n"
    ) in after
    assert "          capture_stdout: true\n" in after
    report = _step(REPORT)
    assert "        if: always() && steps.prepare.outcome == 'success'\n" in report
    block = report.split("        env:\n", 1)[1].split("        run: |\n", 1)[0]
    assert dict(re.findall(r"(?m)^          (\w+): (.+)$", block)) == {
        "DEPLOY_OUTCOME": "${{ steps.deploy.outcome }}",
        "HEALTH_OUTCOME": "${{ steps.health.outcome }}",
        "ROLLBACK_OUTCOME": "${{ steps.rollback.outcome }}",
        # The Fail step's test pins the same expression.
        "RELEASE_UNCHANGED": '"${{ contains(steps.rollback.outputs.stdout, '
        f"'{_release_unchanged_literal()}') }}}}\"",
        "ISOLATION_OUTCOME": "${{ steps.isolation.outcome }}",
        "ISOLATION_STDOUT": "${{ steps.isolation.outputs.stdout }}",
        "BEFORE_STDOUT": "${{ steps.prepare.outputs.stdout }}",
        "AFTER_OUTCOME": "${{ steps.after.outcome }}",
        "AFTER_STDOUT": "${{ steps.after.outputs.stdout }}",
    }


def test_no_evidence_step_can_start_the_rollback() -> None:
    # Rollback and Fail read only the deploy and the health check; no condition reads the evidence
    # steps, and the SSH ones continue on error: only the report can fail the job on a finding.
    for name in (ROLLBACK, FAIL):
        condition = re.search(r"(?m)^        if: (.+)$", _step(name))
        assert condition is not None, name
        assert set(re.findall(r"steps\.(\w+)\.", condition.group(1))) == {"deploy", "health"}
    conditions = re.findall(r"(?m)^        if: (.+)$", _job("deploy-prod"))
    assert not [line for line in conditions if re.search(r"steps\.(isolation|after)\.", line)]
    for name in (ISOLATION, AFTER):
        assert "        continue-on-error: true\n" in _step(name), name


def _isolation_step(
    tmp_path: Path, containers: list[dict[str, Any]]
) -> subprocess.CompletedProcess[str]:
    """The isolation step on a host whose docker answers from `containers` (read-only fake)."""
    app_dir = tmp_path / "opt" / PROD_PROJECT
    app_dir.mkdir(parents=True)
    shutil.copy2(REPO_ROOT / "deploy" / "scripts" / "check-isolation.sh", app_dir)
    networks = sorted(
        {
            network
            for container in containers
            for network in container["NetworkSettings"]["Networks"]
        }
    )
    state = tmp_path / "docker-state.json"
    state.write_text(json.dumps({"networks": networks, "containers": containers}), encoding="utf-8")
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    docker = fake_bin / "docker"
    docker.write_text(f"#!{sys.executable}\n{FAKE_DOCKER}", encoding="utf-8")
    docker.chmod(0o755)
    env = _forwarded(ISOLATION, {"steps.target.outputs.app_dir": str(app_dir)})
    result, _, _ = _run(
        ISOLATION,
        tmp_path,
        {
            **env,
            "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
            "STUB_STATE": str(state),
            "STUB_NETWORK_FORMAT": NETWORK_FORMAT,
            "STUB_INSPECT_FORMAT": INSPECT_FORMAT,
        },
        script=_ssh_script(_step(ISOLATION)),
    )
    return result


@pytest.mark.parametrize(
    ("fault", "code", "verdict", "detail", "annotation"),
    [
        (None, 0, "pass", "isolation: pass", None),
        ("violation", 1, "FAIL", "isolation: FAIL", "::error::"),
        (
            "store-down",
            2,
            "not checked (exit 2)",
            f"check-isolation: no running redis container in compose project {STAGING}",
            "::warning::",
        ),
    ],
    ids=["isolated", "violation", "store-down"],
)
def test_only_an_isolation_violation_fails_the_job(
    tmp_path: Path, fault: str | None, code: int, verdict: str, detail: str, annotation: str | None
) -> None:
    # AC1: the real check on a fake docker. Exit 2 (a store not running, a docker query failed)
    # means "not checked": neither a violation nor a failed prod deploy.
    containers = _host()
    if fault == "violation":
        _join(_find(containers, PROD_PROJECT, "postgres"), f"{STAGING}_default")
    elif fault == "store-down":
        containers.remove(_find(containers, STAGING, "redis"))

    step = _isolation_step(tmp_path, containers)

    # The SSH step succeeds whatever the verdict, so its stdout always reaches the report.
    assert step.returncode == 0, step.stderr
    assert step.stdout.splitlines()[-1] == f"check-isolation exit: {code}"

    result, summary = _report(tmp_path, ISOLATION_STDOUT=step.stdout + DRONE_SSH_BANNER)

    assert result.returncode == (1 if code == 1 else 0), result.stdout
    assert f"- Storage isolation ({PROD_PROJECT} / {STAGING}): {verdict}\n" in summary
    assert detail in summary
    if annotation is None:
        assert "::" not in result.stdout
    else:
        assert annotation in result.stdout
    # The table of the check, as markdown, whenever the check printed one.
    table = [line for line in step.stdout.splitlines() if line.startswith("|")]
    assert bool(table) is (code != 2)
    for line in table:
        assert f"\n{line}\n" in summary


@pytest.mark.parametrize(
    ("isolation", "verdict", "code"),
    [
        ({"ISOLATION_OUTCOME": "skipped", "ISOLATION_STDOUT": ""}, NOT_RUN, 0),
        ({"ISOLATION_OUTCOME": "failure", "ISOLATION_STDOUT": ""}, "not checked (no report)", 0),
        (
            {
                "ISOLATION_STDOUT": "/opt/x/check-isolation.sh: Permission denied\n"
                "check-isolation exit: 126\n" + DRONE_SSH_BANNER
            },
            "check failed (exit 126)",
            1,
        ),
        (
            # An unforeseen error under set -e also exits 1: no verdict line, no violation.
            {
                "ISOLATION_STDOUT": "| container | networks | host ports | verdict |\n"
                "|---|---|---|---|\n"
                "/opt/x/check-isolation.sh: line 99: ROWS[@]: unbound variable\n"
                "check-isolation exit: 1\n" + DRONE_SSH_BANNER
            },
            "check failed (exit 1)",
            1,
        ),
    ],
    ids=["not-run", "ssh-failed", "broken-check", "crashed-check"],
)
def test_an_isolation_check_without_a_verdict_is_reported_as_such(
    tmp_path: Path, isolation: dict[str, str], verdict: str, code: int
) -> None:
    # An SSH failure says nothing about the networks; a check that cannot start is a defect.
    result, summary = _report(tmp_path, **isolation)

    assert result.returncode == code, result.stdout
    assert f"- Storage isolation ({PROD_PROJECT} / {STAGING}): {verdict}\n" in summary
    assert "| container |" not in summary
    assert ("::error::" in result.stdout) is (code == 1)


@pytest.mark.parametrize(
    ("before", "after", "code", "staging"),
    [
        (
            STAGING_IDS,
            tuple(reversed(STAGING_IDS)),
            0,
            "unchanged: the same 2 container IDs before and after this run",
        ),
        ((), (), 0, "unchanged: the same 0 container IDs before and after this run"),
        (
            STAGING_IDS,
            (STAGING_IDS[0], "3" * 64),
            1,
            "CHANGED (AC3): 2 container IDs before, 2 after;"
            f" removed: {'2' * 12}; added: {'3' * 12}",
        ),
        (
            STAGING_IDS,
            (STAGING_IDS[0],),
            1,
            f"CHANGED (AC3): 2 container IDs before, 1 after; removed: {'2' * 12}; added: none",
        ),
        (
            (),
            (STAGING_IDS[0],),
            1,
            f"CHANGED (AC3): 0 container IDs before, 1 after; removed: none; added: {'1' * 12}",
        ),
    ],
    ids=["reordered", "fresh-vps", "recreated", "removed", "created"],
)
def test_the_report_compares_the_staging_containers_before_and_after(
    tmp_path: Path, before: tuple[str, ...], after: tuple[str, ...], code: int, staging: str
) -> None:
    # AC3: staging is not recreated by the prod deploy or its rollback; same IDs in any order.
    result, summary = _report(
        tmp_path,
        BEFORE_STDOUT=_listed("before", before) + "\n" + DRONE_SSH_BANNER,
        AFTER_STDOUT=f"prod current image: {REF_A}\n"
        + _listed("after", after)
        + "\n"
        + DRONE_SSH_BANNER,
    )

    assert result.returncode == code, result.stdout
    assert f"- Staging containers ({STAGING}): {staging}\n" in summary
    assert ("::error::" in result.stdout) is (code == 1)


def test_a_missing_host_read_leaves_staging_not_compared(tmp_path: Path) -> None:
    result, summary = _report(tmp_path, AFTER_OUTCOME="failure", AFTER_STDOUT="")

    assert result.returncode == 0, result.stdout
    assert f"- Staging containers ({STAGING}): not compared (host read: failure)\n" in summary
    assert "- Prod release now: unknown (the host could not be read)\n" in summary
    assert "::warning::" in result.stdout


@pytest.mark.parametrize(
    ("outcomes", "current", "outcome", "release"),
    [
        ({}, REF_A, "deployed", f"`{REF_A}`"),
        (
            {"HEALTH_OUTCOME": "failure", "ROLLBACK_OUTCOME": "success"},
            REF_B,
            "rolled back to the release that ran before this run",
            f"`{REF_B}`",
        ),
        (
            {
                "DEPLOY_OUTCOME": "failure",
                "HEALTH_OUTCOME": "skipped",
                "ROLLBACK_OUTCOME": "success",
            },
            "none",
            STOPPED_OUTCOME,
            "none (the prod stack is down)",
        ),
        (
            # rollback.sh did nothing: deploy.sh of this run stopped before the release switch.
            {
                "DEPLOY_OUTCOME": "failure",
                "HEALTH_OUTCOME": "skipped",
                "ROLLBACK_OUTCOME": "success",
                "RELEASE_UNCHANGED": "true",
            },
            REF_B,
            UNCHANGED_OUTCOME,
            f"`{REF_B}`",
        ),
        (
            {
                "DEPLOY_OUTCOME": "failure",
                "HEALTH_OUTCOME": "skipped",
                "ROLLBACK_OUTCOME": "success",
                "RELEASE_UNCHANGED": "true",
            },
            "none",
            UNCHANGED_OUTCOME,
            "none (the prod stack is down)",
        ),
        (
            {"HEALTH_OUTCOME": "failure", "ROLLBACK_OUTCOME": "failure"},
            REF_B,
            "rollback failed",
            f"`{REF_B}`",
        ),
        (
            {"DEPLOY_OUTCOME": "skipped", "HEALTH_OUTCOME": "skipped"},
            REF_B,
            "not deployed",
            f"`{REF_B}`",
        ),
        # A cancel or timeout starts no rollback (it reacts to failures only): .env may already
        # name the new image while .deploy-state still names the old one.
        (
            {"DEPLOY_OUTCOME": "cancelled", "HEALTH_OUTCOME": "skipped"},
            REF_A,
            CANCELLED_OUTCOME,
            f"`{REF_A}`",
        ),
        ({"HEALTH_OUTCOME": "cancelled"}, REF_B, CANCELLED_OUTCOME, f"`{REF_B}`"),
    ],
    ids=[
        "deployed",
        "rolled-back",
        "stopped",
        "release-unchanged",
        "first-release-unchanged",
        "rollback-failed",
        "not-deployed",
        "deploy-cancelled",
        "health-cancelled",
    ],
)
def test_the_report_names_the_outcome_and_the_release_running_now(
    tmp_path: Path, outcomes: dict[str, str], current: str, outcome: str, release: str
) -> None:
    deployed = not outcomes
    isolation = {} if deployed else {"ISOLATION_OUTCOME": "skipped", "ISOLATION_STDOUT": ""}
    result, summary = _report(
        tmp_path,
        **outcomes,
        **isolation,
        AFTER_STDOUT=f"prod current image: {current}\n"
        + _listed("after", STAGING_IDS)
        + "\n"
        + DRONE_SSH_BANNER,
    )

    # The Fail step fails a rolled-back run; the report fails only on its own findings.
    assert result.returncode == 0, result.stdout
    assert f"- Outcome: {outcome}\n- Prod release now: {release}\n" in summary
    assert ("::warning::" in result.stdout) is (outcome == CANCELLED_OUTCOME), result.stdout
    if not deployed:
        assert f"- Storage isolation ({PROD_PROJECT} / {STAGING}): {NOT_RUN}\n" in summary


@pytest.mark.parametrize(
    ("state", "listed", "lines"),
    [
        (
            f"current_image={REF_A}\nrolled_back=true\n",
            "\n".join(STAGING_IDS) + "\n",
            [f"prod current image: {REF_A}", _listed("after", STAGING_IDS)],
        ),
        (
            None,
            "\n".join(STAGING_IDS) + "\n",
            ["prod current image: none", _listed("after", STAGING_IDS)],
        ),
        (None, "", ["prod current image: none", "staging containers after:"]),
    ],
    ids=["release-running", "stopped", "fresh-vps"],
)
def test_the_host_read_names_the_prod_release_and_lists_staging_like_prepare(
    tmp_path: Path, state: str | None, listed: str, lines: list[str]
) -> None:
    # A stop removes .deploy-state: no prod release runs, the edge proxy answers 503.
    app_dir = tmp_path / "opt" / PROD_PROJECT
    app_dir.mkdir(parents=True)
    if state is not None:
        (app_dir / ".deploy-state").write_text(state, encoding="utf-8")
    env = _forwarded(AFTER, {"steps.target.outputs.app_dir": str(app_dir)})

    result, _, _ = _run(
        AFTER, tmp_path, {**env, "STUB_PS_OUTPUT": listed}, script=_ssh_script(_step(AFTER))
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == lines
    # The same read-only query as Prepare host.
    assert _docker_calls(tmp_path) == [STAGING_PS]
