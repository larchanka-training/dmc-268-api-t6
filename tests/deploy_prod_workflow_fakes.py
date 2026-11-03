"""Helpers of the Deploy prod workflow tests: the workflow steps, stub docker and the ssh script."""

from __future__ import annotations

import os
import re
import subprocess
import textwrap
from collections.abc import Mapping
from pathlib import Path

from tests.deploy_host_fakes import ENV_FILES, PROD_PROJECT, Host, _read_dotenv, _run_script

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "deploy-prod.yml"

AT_JOB_START = "Re-resolve the staging tag at job start"
REFUSE_MOVED = "Refuse a moved staging tag"
TARGET = "Resolve prod target"
REFUSE_SECRETS = "Refuse missing production secrets"
CHECKOUT = "Checkout"
BUNDLE = "Bundle application secrets"
VALIDATE = "Validate application bundle and GitLab configuration"
UPLOAD = "Upload deploy files"

DEPLOY = "Deploy image"
HEALTH = "Health check"
ROLLBACK = "Rollback on failed deploy or health check"
FAIL = "Fail the run after rollback"
ISOLATION = "Check storage isolation"
AFTER = "Read prod release and staging containers"
REPORT = "Report the prod run"

DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
REF_A = f"ghcr.io/owner/repo@{DIGEST_A}"
REF_B = f"ghcr.io/owner/repo@{DIGEST_B}"

# Answers the registry calls of the resolve step; the token may only arrive on stdin.
STUB_DOCKER = """#!/bin/bash
printf '%s\\n' "$*" >> "${STUB_LOG}"
case "$1" in
  login) cat >> "${STUB_LOG}.stdin"; exit "${STUB_LOGIN_STATUS:-0}" ;;
  logout) exit 0 ;;
  buildx) printf '%s' "${STUB_ANSWER}"; exit "${STUB_INSPECT_STATUS:-0}" ;;
  ps) printf '%s' "${STUB_PS_OUTPUT:-}"; exit "${STUB_PS_STATUS:-0}" ;;
esac
echo "unexpected docker call: $*" >&2
exit 99
"""


def _text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def _job(name: str) -> str:
    match = re.search(rf"(?m)^  {name}:\n((?:    .*\n|\n)*)", _text())
    assert match is not None, f"job {name} is missing"
    return match.group(1)


def _step_names(job: str) -> list[str]:
    return re.findall(r"(?m)^      - name: (.+)$", job)


def _step(name: str) -> str:
    """The step `name` (unique across the jobs), up to the next step or job."""
    text = _text()
    header = f"      - name: {name}\n"
    assert text.count(header) == 1, f"step {name!r} must exist exactly once"
    body = text.split(header, 1)[1]
    return header + re.split(r"(?m)^      - name: |^  [\w-]+:\n", body, maxsplit=1)[0]


def _ssh_script(step: str) -> str:
    """The ``script: |`` block an appleboy SSH step runs on the host, dedented."""
    match = re.search(r"(?m)^          script: \|\n((?:            .*\n|\n)*)", step)
    assert match is not None, "the step has no script block"
    return textwrap.dedent(match.group(1)).strip("\n")


def _run(
    name: str, tmp_path: Path, env: Mapping[str, str], *, script: str | None = None
) -> tuple[subprocess.CompletedProcess[str], str, str]:
    """Run the step's script as GitHub does; returns the result, GITHUB_OUTPUT and the summary.

    `script` replaces the step's ``run:`` block (an SSH step's ``script:``).
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    docker = bin_dir / "docker"
    docker.write_text(STUB_DOCKER)
    docker.chmod(0o755)
    output = tmp_path / "github-output"
    summary = tmp_path / "step-summary"
    output.write_text("")
    summary.write_text("")
    result = subprocess.run(
        [
            "bash",
            "--noprofile",
            "--norc",
            "-eo",
            "pipefail",
            "-c",
            _run_script(_step(name)) if script is None else script,
        ],
        cwd=tmp_path,
        env={
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "GITHUB_OUTPUT": str(output),
            "GITHUB_STEP_SUMMARY": str(summary),
            "STUB_LOG": str(tmp_path / "docker.log"),
            **env,
        },
        capture_output=True,
        text=True,
        check=False,
    )
    return result, output.read_text(), summary.read_text()


def _docker_calls(tmp_path: Path) -> list[str]:
    log = tmp_path / "docker.log"
    return log.read_text().splitlines() if log.exists() else []


CI_WORKFLOW = WORKFLOW.parent / "ci-cd.yml"

# Every host step logs in to the target the job resolved, with the organization password and the
# pinned host key; the target accepts only the course VPS.
VPS_LOGIN = (
    "        with:\n"
    "          host: ${{ steps.target.outputs.host }}\n"
    "          port: ${{ steps.target.outputs.port }}\n"
    "          username: ${{ secrets.VPS_DMC268_U }}\n"
    "          password: ${{ secrets.VPS_DMC268_P }}\n"
    "          fingerprint: ${{ vars.STAGING_SSH_FINGERPRINT }}\n"
)
STAGING_IDS = ("1" * 64, "2" * 64)
STAGING_PS = "ps -aq --no-trunc --filter label=com.docker.compose.project=dmc-268-api-staging"


def _forwarded(name: str, context: Mapping[str, str]) -> dict[str, str]:
    """What the SSH step `name` hands to the host: its ``envs:`` names, expressions rendered.

    An expression missing from `context` fails the test: the step reads something unplanned.
    """
    step = _step(name)
    envs = re.search(r"(?m)^          envs: (\S+)$", step)
    assert envs is not None, f"{name} forwards nothing"
    block = step.split("        env:\n", 1)[1].split("        with:\n", 1)[0]
    env = dict(re.findall(r"(?m)^          (\w+): (.+)$", block))
    names = envs.group(1).split(",")
    # Like ssh-action, a name set in env: but missing from envs: never reaches the host.
    assert sorted(names) == sorted(env), name
    # A literal empty string (`''`) is how a step sets a variable to empty.
    return {
        key: re.sub(
            r"\$\{\{ (.+?) \}\}",
            lambda match: context[match.group(1)],
            "" if env[key] == "''" else env[key],
        )
        for key in names
    }


def _host_context(host: Host, deploy_run: str) -> dict[str, str]:
    run_id, attempt = deploy_run.split("-")
    return {
        "secrets.GITHUB_TOKEN": "test-ghcr-token",
        "github.actor": "github",
        "github.run_id": run_id,
        "github.run_attempt": attempt,
        "steps.target.outputs.app_dir": str(host.app_dir),
        "steps.target.outputs.edge_alias": "api-prod",
        "vars.WORKER_REPLICAS || '1'": "1",
    }


def _deploy_step(
    host: Host,
    image: str,
    deploy_run: str,
    *,
    bundle: str = "",
    worker_replicas: str = "1",
    **stubs: str,
) -> subprocess.CompletedProcess[str]:
    """The "Deploy image" step of the run `deploy_run`; `image` is the resolved digest.

    `bundle` is the output of the bundle step; an unset Environment yields an empty string.
    `worker_replicas` is what the expression on the WORKER_REPLICAS variable renders to.
    """
    assert _ssh_script(_step(DEPLOY)).endswith('\n"${APP_DIR}/deploy.sh" "${IMAGE}"')
    context = {
        **_host_context(host, deploy_run),
        "vars.WORKER_REPLICAS || '1'": worker_replicas,
        "steps.digest.outputs.image": image,
        "steps.app_secrets.outputs.bundle": bundle,
    }
    env = _forwarded(DEPLOY, context)
    return host.run("deploy.sh", env["IMAGE"], env={**env, **stubs})


def _assert_prod_only(host: Host, *, bundle: str = "") -> None:
    calls = host.calls()
    assert not [call for call in calls if "api-staging" in call]  # project, volumes, alias
    compose = [call for call in calls if call.startswith("compose ")]
    assert compose
    assert all(call.startswith(f"compose -p {PROD_PROJECT} ") for call in compose)
    env = _read_dotenv(host.env_file)
    assert (env["NO_PREVIOUS_RELEASE"], env["EDGE_ALIAS"], env["POSTGRES_DB"]) == (
        "stop",
        "api-prod",
        "app",
    )
    # Without a bundle every role file stays empty.
    for name in ENV_FILES if not bundle else ():
        assert (host.app_dir / name).read_text(encoding="utf-8") == "", name
