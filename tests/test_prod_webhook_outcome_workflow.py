"""The read-only lookup of a prod webhook outcome (#308 Part 2).

Nobody on the team has SSH to the course VPS, so a manual workflow prints the ``webhook-worker``
outcome line of one GitHub delivery id from the prod project's log. It reads only: no Environment,
no VPS lock, no deploy. Text contracts on the workflow file, and its shell steps run as scripts with
a stub ``docker`` on PATH.
"""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Mapping
from pathlib import Path

import pytest

from tests.test_deploy_prod_workflow import (
    VPS_LOGIN,
    _ssh_script,
)
from tests.test_deploy_prod_workflow import (
    WORKFLOW as DEPLOY_PROD,
)
from tests.test_deploy_staging_services import _run_script, _workflow_step
from tests.test_monitoring_acceptance import DRONE_SSH_BANNER

WORKFLOW = DEPLOY_PROD.parent / "prod-webhook-outcome.yml"

VALIDATE = "Validate the delivery id"
TARGET = "Resolve prod target"
LOOKUP = "Read the webhook-worker outcome line"
SHOW = "Show the outcome"

DELIVERY = "72d3162e-cc78-11e3-81ab-4c9367dc0958"
WORKER = "a" * 64
LINE = (
    f"GitHub webhook delivery {DELIVERY} event=pull_request status=projected_pr"
    " detail=ok retry_at=none"
)

# Answers the two docker calls of the lookup. Logs go to stdout and stderr, as a container's do.
STUB_DOCKER = """#!/bin/bash
printf '%s\\n' "$*" >> "${STUB_LOG}"
case "$1" in
  ps) printf '%s' "${STUB_PS_OUTPUT:-}"; exit 0 ;;
  logs) printf '%s\\n' "${STUB_LOG_STDOUT:-}"; printf '%s\\n' "${STUB_LOG_STDERR:-}" >&2; exit 0 ;;
esac
echo "unexpected docker call: $*" >&2
exit 99
"""


def _text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def _step(name: str) -> str:
    return _workflow_step(name, WORKFLOW)


def _script(name: str) -> str:
    return _run_script(_step(name))


def _bash(
    script: str, tmp_path: Path, env: Mapping[str, str]
) -> tuple[subprocess.CompletedProcess[str], str]:
    """Run `script` as GitHub does; returns the result and the step summary."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    docker = bin_dir / "docker"
    docker.write_text(STUB_DOCKER)
    docker.chmod(0o755)
    summary = tmp_path / "step-summary"
    summary.write_text("")
    result = subprocess.run(
        ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", script],
        cwd=tmp_path,
        env={
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "GITHUB_STEP_SUMMARY": str(summary),
            "STUB_LOG": str(tmp_path / "docker.log"),
            **env,
        },
        capture_output=True,
        text=True,
        check=False,
    )
    return result, summary.read_text()


def _docker_calls(tmp_path: Path) -> list[str]:
    log = tmp_path / "docker.log"
    return log.read_text().splitlines() if log.exists() else []


def test_only_a_manual_dispatch_with_a_required_delivery_id_starts_it() -> None:
    text = _text()
    assert text.startswith("name: Prod webhook outcome\n")
    trigger = text.split("\non:\n", 1)[1].split("\npermissions:", 1)[0]
    assert re.findall(r"(?m)^  (\w+):", trigger) == ["workflow_dispatch"]
    assert re.findall(r"(?m)^      (\w+):$", trigger) == ["delivery_id"]
    assert re.search(
        r'(?m)^      delivery_id:\n        description: "[^"\n]+"\n'
        r"        required: true\n        type: string\n(?!        default:)",
        trigger,
    )


def test_it_only_reads_and_stays_out_of_the_environment_and_the_lock() -> None:
    text = _text()
    assert "\npermissions:\n  contents: read\n\n" in text
    assert text.count("permissions:") == 1
    assert re.search(r":\s*write\b", text) is None
    assert re.search(r"(?m)^\s*(environment|concurrency):", text) is None
    assert "staging-deploy" not in text
    jobs = re.findall(r"(?m)^  ([\w-]+):\n", text.split("\njobs:\n", 1)[1])
    assert len(jobs) == 1
    assert "    if: github.ref == 'refs/heads/main'\n" in text
    assert "    runs-on: ubuntu-latest\n" in text


def test_the_steps_run_in_order() -> None:
    names = re.findall(r"(?m)^      - name: (.+)$", _text())

    assert names == [VALIDATE, TARGET, LOOKUP, SHOW]


def test_the_target_is_the_one_deploy_prod_resolves() -> None:
    def code(script: str) -> str:
        # Comments differ by design: the lookup job has no Environment.
        return "\n".join(line for line in script.splitlines() if not line.lstrip().startswith("#"))

    ours, theirs = _step(TARGET), _workflow_step(TARGET, DEPLOY_PROD)

    assert code(_run_script(ours)) == code(_run_script(theirs))
    assert ours.split("        run: |\n", 1)[0] == theirs.split("        run: |\n", 1)[0]
    assert "        id: target\n" in ours


def test_expressions_reach_the_shell_only_through_env() -> None:
    text = _text()
    bodies = re.findall(r"(?m)^        (?:run|script): \|\n((?:          .*\n|\n)*)", text)
    assert len(bodies) >= 3
    for body in bodies:
        assert "${{" not in body
    assert "${{" not in _ssh_script(_step(LOOKUP))
    assert [line.strip() for line in text.splitlines() if "inputs." in line] == [
        "DELIVERY_ID: ${{ inputs.delivery_id }}",
    ] * 3


@pytest.mark.parametrize(
    "delivery_id",
    [
        "",
        "latest",
        "72d3162e-cc78-11e3-81ab-4c9367dc095",  # one digit short
        "72d3162e-cc78-11e3-81ab-4c9367dc0958x",
        "72d3162e-cc78-11e3-81ab-4c9367dc0958\n",
        f"{DELIVERY}\nGitHub webhook delivery 00000000-0000-0000-0000-000000000000 ",
        "72d3162e-cc78-11e3-81ab-4c9367dc0958 extra",
        "$(id)",
        "72d3162e-cc78-11e3-81ab-4c9367dc095g",
        DELIVERY.upper(),  # the log match is case-sensitive: an uppercase id would find nothing
    ],
)
def test_a_delivery_id_that_is_not_one_guid_is_refused(tmp_path: Path, delivery_id: str) -> None:
    result, _ = _bash(_script(VALIDATE), tmp_path, {"DELIVERY_ID": delivery_id})

    assert result.returncode == 1
    assert result.stderr.startswith("::error::delivery_id must be one GUID")
    assert _docker_calls(tmp_path) == []


def test_a_guid_is_accepted(tmp_path: Path) -> None:
    result, _ = _bash(_script(VALIDATE), tmp_path, {"DELIVERY_ID": DELIVERY})

    assert result.returncode == 0, result.stderr


def test_the_ssh_step_logs_in_like_deploy_prod_and_forwards_only_the_id() -> None:
    step = _step(LOOKUP)
    prod = _workflow_step("Prepare host", DEPLOY_PROD)
    uses = re.search(r"(?m)^        (uses: appleboy/ssh-action@.+)$", step)
    pin = re.search(r"(?m)^        (uses: appleboy/ssh-action@.+)$", prod)

    assert uses is not None and pin is not None and uses.group(1) == pin.group(1)
    assert VPS_LOGIN in step
    assert "          debug: false\n" in step
    assert "          capture_stdout: true\n" in step
    assert "          envs: DELIVERY_ID\n" in step
    assert "        env:\n          DELIVERY_ID: ${{ inputs.delivery_id }}\n" in step
    assert "        id: lookup\n" in step
    assert _ssh_script(step).startswith("set -euo pipefail\nset +o xtrace\n")


def _lookup(tmp_path: Path, **env: str) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    result, _ = _bash(_ssh_script(_step(LOOKUP)), tmp_path, {"DELIVERY_ID": DELIVERY, **env})
    return result, _docker_calls(tmp_path)


def test_the_lookup_prints_only_the_outcome_line_of_the_delivery(tmp_path: Path) -> None:
    other = LINE.replace(DELIVERY, "00000000-0000-0000-0000-000000000000")
    unrelated = f"processing receipt for {DELIVERY} (no outcome yet)"
    # One line per stream: the worker's logging goes to stderr, other output to stdout.
    result, calls = _lookup(
        tmp_path,
        STUB_PS_OUTPUT=f"{WORKER}\n",
        STUB_LOG_STDOUT=f"{other}\n{unrelated}",
        STUB_LOG_STDERR=f"{LINE}\nGitHub webhook delivery {DELIVERY}X event=x status=y detail=z",
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == f"{LINE}\n"
    assert result.stderr == ""
    assert calls == [
        "ps -aq --no-trunc"
        " --filter label=com.docker.compose.project=dmc-268-api-prod"
        " --filter label=com.docker.compose.service=webhook-worker",
        f"logs --since 168h {WORKER}",
    ]


def test_the_lookup_reads_every_worker_replica(tmp_path: Path) -> None:
    second = "b" * 64

    result, calls = _lookup(tmp_path, STUB_PS_OUTPUT=f"{WORKER}\n{second}\n", STUB_LOG_STDERR=LINE)

    assert result.returncode == 0, result.stderr
    assert result.stdout == f"{LINE}\n{LINE}\n"
    assert calls[1:] == [f"logs --since 168h {WORKER}", f"logs --since 168h {second}"]


def test_a_delivery_without_an_outcome_prints_nothing_and_succeeds(tmp_path: Path) -> None:
    result, _ = _lookup(tmp_path, STUB_PS_OUTPUT=f"{WORKER}\n", STUB_LOG_STDERR="other line")

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""


def test_the_lookup_fails_when_prod_runs_no_webhook_worker(tmp_path: Path) -> None:
    result, calls = _lookup(tmp_path, STUB_PS_OUTPUT="")

    assert result.returncode == 1
    assert result.stdout == ""
    assert "no webhook-worker container" in result.stderr
    assert len(calls) == 1


def test_the_host_refuses_an_id_that_is_not_one_guid(tmp_path: Path) -> None:
    result, calls = _lookup(tmp_path, DELIVERY_ID="x; docker rm -f all", STUB_PS_OUTPUT=WORKER)

    assert result.returncode == 1
    assert result.stdout == ""
    assert calls == []


def test_the_summary_shows_the_outcome_line_without_the_ssh_banner(tmp_path: Path) -> None:
    stdout = f"{LINE}\n{DRONE_SSH_BANNER}"

    result, summary = _bash(
        _script(SHOW), tmp_path, {"DELIVERY_ID": DELIVERY, "LOOKUP_STDOUT": stdout}
    )

    assert result.returncode == 0, result.stderr
    assert LINE in summary
    assert "Successfully executed" not in summary


def test_the_summary_says_when_there_is_no_outcome_line(tmp_path: Path) -> None:
    result, summary = _bash(
        _script(SHOW), tmp_path, {"DELIVERY_ID": DELIVERY, "LOOKUP_STDOUT": DRONE_SSH_BANNER}
    )

    assert result.returncode == 0, result.stderr
    assert DELIVERY in summary
    assert "No outcome line" in summary
    assert "recreated by a later deploy" in summary
    assert "```" not in summary


def test_the_summary_cannot_be_forged_with_a_closing_fence(tmp_path: Path) -> None:
    forged = f"{LINE} detail=```\n## forged"

    _, summary = _bash(
        _script(SHOW), tmp_path, {"DELIVERY_ID": DELIVERY, "LOOKUP_STDOUT": forged + "\n"}
    )

    assert summary.count("```") == 2
    assert "\n## forged" not in summary
