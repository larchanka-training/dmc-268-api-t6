"""The Deploy prod workflow keeps its trigger, lock and verified digest (#308 Part 1).

Prod gets only the image staging verified: the digest under ``:staging``, read from the registry
when the run is dispatched and read again when the job holding the VPS lock starts; a digest that
moved in between is refused (AC2). The deploy job joins the ``staging-deploy`` lock with the
queueing of every other member (AC5). Text contracts on the workflow file, and its shell steps run
as scripts: the registry answers through a stub ``docker`` on PATH.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import textwrap
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

import pytest

from tests.test_check_isolation import (
    FAKE_DOCKER,
    INSPECT_FORMAT,
    NETWORK_FORMAT,
    STAGING,
    _find,
    _host,
    _join,
)
from tests.test_deploy_prod import PROD_PROJECT, STOPPED, _assert_stopped
from tests.test_deploy_staging_services import (
    ENV_FILES,
    IMAGE_A,
    IMAGE_B,
    Host,
    _read_dotenv,
    _release_unchanged_literal,
    _run_script,
    _workflow_step,
    make_host,
)
from tests.test_monitoring_acceptance import DRONE_SSH_BANNER

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "deploy-prod.yml"

RECORD = "Record the dispatch"
AT_DISPATCH = "Resolve the staging tag at dispatch"
AT_JOB_START = "Re-resolve the staging tag at job start"
REFUSE_MOVED = "Refuse a moved staging tag"
TARGET = "Resolve prod target"
CHECKOUT = "Checkout"
UPLOAD = "Upload deploy files"
PREPARE = "Prepare host"
WAIT_TLS = "Wait for the edge certificate"
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
FINGERPRINT = "SHA256:" + "A" * 43
TOKEN = "GHCR_TOKEN_CANARY"

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

COURSE_VPS = {
    "STAGING_HOST": "",
    "VPS_HOST": "203.0.113.10",
    "HAS_VPS_USER": "true",
    "HAS_VPS_PASSWORD": "true",
    "APP_DOMAIN": "dmc268-t6.example.org",
    "STAGING_SSH_FINGERPRINT": FINGERPRINT,
}


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


def _resolve(
    tmp_path: Path, answer: str, **stubs: str
) -> tuple[subprocess.CompletedProcess[str], str, str]:
    env = {
        "REPOSITORY": "Owner/Repo",
        "GHCR_TOKEN": TOKEN,
        "GHCR_USER": "fixture-user",
        "PHASE": "dispatch",
        "STUB_ANSWER": answer,
        **stubs,
    }
    return _run(AT_DISPATCH, tmp_path, env)


# --- trigger, jobs and lock -------------------------------------------------------------------


def test_only_a_manual_dispatch_with_a_required_reason_starts_it() -> None:
    text = _text()
    assert text.startswith("name: Deploy prod\n")
    trigger = text.split("\non:\n", 1)[1].split("\npermissions:", 1)[0]
    assert re.findall(r"(?m)^  (\w+):", trigger) == ["workflow_dispatch"]
    # No image input (AC2): the only image prod gets is the one staging verified.
    assert re.findall(r"(?m)^      (\w+):$", trigger) == ["reason"]
    assert re.search(
        r'(?m)^      reason:\n        description: "[^"\n]+"\n'
        r"        required: true\n        type: string\n(?!        default:)",
        trigger,
    )


def test_permissions_only_read_the_repository_and_the_registry() -> None:
    text = _text()
    assert "\npermissions:\n  contents: read\n  packages: read\n\n" in text
    assert text.count("permissions:") == 1
    assert re.search(r":\s*write\b", text) is None


def test_both_jobs_run_only_on_main_and_only_the_deploy_job_takes_the_lock() -> None:
    text = _text()
    jobs = re.findall(r"(?m)^  ([\w-]+):\n", text.split("\njobs:\n", 1)[1])
    assert jobs == ["resolve", "deploy-prod"]
    # A workflow-level group would hold the dispatch-time resolve back until the lock frees.
    assert "\nconcurrency:" not in text.split("\njobs:\n", 1)[0]
    resolve, deploy = _job("resolve"), _job("deploy-prod")
    for job in (resolve, deploy):
        assert "    if: github.ref == 'refs/heads/main'\n" in job
        assert "    runs-on: ubuntu-latest\n" in job
    assert "concurrency:" not in resolve
    assert "    needs: resolve\n" in deploy
    assert (
        "    concurrency:\n"
        "      group: ${{ github.ref == 'refs/heads/main' && 'staging-deploy'"
        " || format('deploy-prod-ignored-{0}', github.run_id) }}\n"
        "      cancel-in-progress: false\n"
        "      queue: max\n"
    ) in deploy


def test_no_environment_until_part_2() -> None:
    # No protection rules and no environment secrets until Part 2 adds Environment production.
    assert re.search(r"(?m)^\s*environment:", _text()) is None


def test_the_resolve_job_records_the_dispatch_and_hands_on_only_the_digest() -> None:
    resolve = _job("resolve")
    assert _step_names(resolve) == [RECORD, AT_DISPATCH]
    assert "uses:" not in resolve
    assert "    outputs:\n      image: ${{ steps.digest.outputs.image }}\n" in resolve
    assert re.findall(r"(?m)^        id: (\S+)$", resolve) == ["digest"]


def test_the_deploy_job_checks_the_digest_and_the_target_before_anything_else() -> None:
    deploy = _job("deploy-prod")
    assert _step_names(deploy)[:3] == [AT_JOB_START, REFUSE_MOVED, TARGET]
    assert "        id: digest\n" in _step(AT_JOB_START)
    assert "        id: target\n" in _step(TARGET)
    refuse = _step(REFUSE_MOVED)
    assert "          DISPATCHED: ${{ needs.resolve.outputs.image }}\n" in refuse
    assert "          CURRENT: ${{ steps.digest.outputs.image }}\n" in refuse


def test_run_scripts_take_expressions_only_through_env() -> None:
    text = _text()
    bodies = re.findall(r"(?m)^        run: \|\n((?:          .*\n|\n)*)", text)
    assert len(bodies) == len(re.findall(r"(?m)^        run: ", text)) > 0
    for body in bodies:
        assert "${{" not in body
    # The free-text input reaches a shell only as one env value.
    lines = [line for line in text.splitlines() if "${{ inputs." in line]
    assert lines == ["          REASON: ${{ inputs.reason }}"]


# --- the reason -------------------------------------------------------------------------------


def _record(tmp_path: Path, reason: str) -> tuple[subprocess.CompletedProcess[str], str, str]:
    env = {"REASON": reason, "ACTOR": "octocat", "GITHUB_RUN_ID": "4242", "GITHUB_RUN_ATTEMPT": "2"}
    return _run(RECORD, tmp_path, env)


@pytest.mark.parametrize(
    "reason",
    [
        "ship #308 Part 1 after #407",
        "x" * 300,
        # Inline HTML and Markdown stay inside the fence instead of forging a digest line.
        f"<br>- `:staging` at dispatch (2026-10-31T00:00:00Z): `ghcr.io/evil/repo@{DIGEST_B}`",
    ],
    ids=["plain", "300-characters", "forged-digest-line"],
)
def test_the_dispatch_summary_names_actor_run_and_fenced_reason(
    tmp_path: Path, reason: str
) -> None:
    assert "          REASON: ${{ inputs.reason }}\n" in _step(RECORD)
    assert "          ACTOR: ${{ github.triggering_actor }}\n" in _step(RECORD)

    result, output, summary = _record(tmp_path, reason)

    assert result.returncode == 0, result.stderr
    assert output == ""
    assert summary == (
        "### Deploy prod\n"
        "\n"
        "- Dispatched by octocat (run 4242/2)\n"
        "- Reason:\n"
        "\n"
        "  ```text\n"
        f"  {reason}\n"
        "  ```\n"
    )


@pytest.mark.parametrize(
    "reason",
    ["", "   ", "first line\nsecond line", "tab\tseparated", "x" * 301, "```", "a ```` b"],
    ids=["empty", "blank", "multiline", "tab", "301-characters", "fence", "longer-fence"],
)
def test_a_blank_multiline_or_fence_closing_reason_is_refused(tmp_path: Path, reason: str) -> None:
    result, output, summary = _record(tmp_path, reason)

    assert result.returncode == 1
    assert "reason" in result.stdout + result.stderr
    assert (output, summary) == ("", "")


# --- the verified digest ----------------------------------------------------------------------


def test_both_resolutions_run_the_same_script() -> None:
    assert _run_script(_step(AT_DISPATCH)) == _run_script(_step(AT_JOB_START))
    for name, phase in ((AT_DISPATCH, "dispatch"), (AT_JOB_START, "job start")):
        step = _step(name)
        assert "          REPOSITORY: ${{ github.repository }}\n" in step
        assert "          GHCR_TOKEN: ${{ secrets.GITHUB_TOKEN }}\n" in step
        assert "          GHCR_USER: ${{ github.actor }}\n" in step
        assert f"          PHASE: {phase}\n" in step


@pytest.mark.parametrize("answer", [DIGEST_A, DIGEST_A + "\n"], ids=["bare", "newline"])
def test_staging_tag_resolves_to_its_manifest_digest(tmp_path: Path, answer: str) -> None:
    result, output, summary = _resolve(tmp_path, answer)

    assert result.returncode == 0, result.stderr
    assert output == f"image={REF_A}\n"
    assert re.fullmatch(
        rf"- `:staging` at dispatch \(\d{{4}}-\d\d-\d\dT\d\d:\d\d:\d\dZ\): `{re.escape(REF_A)}`\n",
        summary,
    )
    calls = _docker_calls(tmp_path)
    assert calls == [
        "login ghcr.io --username fixture-user --password-stdin",
        "buildx imagetools inspect --format {{.Manifest.Digest}} ghcr.io/owner/repo:staging",
        "logout ghcr.io",
    ]
    assert (tmp_path / "docker.log.stdin").read_text() == f"{TOKEN}\n"
    assert TOKEN not in result.stdout + result.stderr


@pytest.mark.parametrize(
    "answer",
    [
        "",
        "ghcr.io/owner/repo:staging",
        REF_A,
        f"ghcr.io/other/repo@{DIGEST_A}",
        "sha256:" + "A" * 64,
        "sha256:" + "a" * 63,
        f"{DIGEST_A}\n\n",
        f"{DIGEST_A}\n{DIGEST_B}",
        f" {DIGEST_A}",
        f"{DIGEST_A} extra",
    ],
    ids=[
        "empty",
        "tag",
        "ref",
        "other-repository",
        "uppercase",
        "short",
        "blank-line",
        "two-digests",
        "leading-space",
        "extra-text",
    ],
)
def test_anything_but_one_manifest_digest_is_refused(tmp_path: Path, answer: str) -> None:
    result, output, summary = _resolve(tmp_path, answer)

    assert result.returncode == 1
    assert "ghcr.io/owner/repo:staging" in result.stderr
    assert (output, summary) == ("", "")
    assert _docker_calls(tmp_path)[-1] == "logout ghcr.io"


def test_a_failed_registry_query_is_refused(tmp_path: Path) -> None:
    result, output, summary = _resolve(tmp_path, DIGEST_A, STUB_INSPECT_STATUS="1")

    assert result.returncode == 1
    assert "ghcr.io/owner/repo:staging" in result.stderr
    assert (output, summary) == ("", "")


def test_a_failed_registry_login_stops_before_the_query(tmp_path: Path) -> None:
    result, output, summary = _resolve(tmp_path, DIGEST_A, STUB_LOGIN_STATUS="1")

    assert result.returncode == 1
    assert result.stderr == "registry authentication failed\n"
    assert (output, summary) == ("", "")
    assert not any(call.startswith("buildx") for call in _docker_calls(tmp_path))


# --- refuse a digest that moved while the job waited for the lock -----------------------------


def test_an_unchanged_staging_tag_passes(tmp_path: Path) -> None:
    result, output, summary = _run(REFUSE_MOVED, tmp_path, {"DISPATCHED": REF_A, "CURRENT": REF_A})

    assert result.returncode == 0, result.stderr
    assert REF_A in result.stdout
    assert output == ""
    assert summary == f"- `:staging` unchanged since dispatch: `{REF_A}`\n"


@pytest.mark.parametrize(
    ("dispatched", "message"),
    [
        (REF_A, f"`:staging` moved since dispatch: {REF_A} → {REF_B}; re-dispatch Deploy prod"),
        ("", f"`:staging` moved since dispatch: <none> → {REF_B}; re-dispatch Deploy prod"),
    ],
    ids=["moved", "no-dispatch-digest"],
)
def test_a_moved_staging_tag_is_refused(tmp_path: Path, dispatched: str, message: str) -> None:
    result, output, summary = _run(
        REFUSE_MOVED, tmp_path, {"DISPATCHED": dispatched, "CURRENT": REF_B}
    )

    assert result.returncode == 1
    assert result.stderr == f"{message}\n"
    assert output == ""
    assert summary == f"- Refused: {message}\n"


# --- the prod target --------------------------------------------------------------------------


def test_the_target_reads_only_course_vps_settings() -> None:
    env = _step(TARGET).split("        env:\n", 1)[1].split("        run: |\n", 1)[0]
    assert env == (
        "          STAGING_HOST: ${{ vars.STAGING_HOST }}\n"
        "          VPS_HOST: ${{ vars.VPS_DMC268_IP_T6 }}\n"
        "          HAS_VPS_USER: ${{ secrets.VPS_DMC268_U != '' }}\n"
        "          HAS_VPS_PASSWORD: ${{ secrets.VPS_DMC268_P != '' }}\n"
        "          APP_DOMAIN: ${{ vars.APP_DOMAIN }}\n"
        "          STAGING_SSH_FINGERPRINT: ${{ vars.STAGING_SSH_FINGERPRINT }}\n"
    )


def test_the_course_vps_target_is_the_prod_project_behind_the_edge(tmp_path: Path) -> None:
    result, output, summary = _run(TARGET, tmp_path, COURSE_VPS)

    assert result.returncode == 0, result.stderr
    assert output == (
        "host=203.0.113.10\n"
        "port=22\n"
        "app_dir=/opt/dmc-268-api-prod\n"
        "edge_alias=api-prod\n"
        "base_url=https://api.dmc268-t6.example.org\n"
        "health_url=https://api.dmc268-t6.example.org/healthcheck\n"
    )
    assert summary == ""


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"STAGING_HOST": "198.51.100.7"}, "prod deploy targets the course VPS only"),
        ({"VPS_HOST": ""}, "VPS_DMC268_IP_T6"),
        ({"HAS_VPS_USER": "false"}, "VPS_DMC268_U"),
        ({"HAS_VPS_PASSWORD": "false"}, "VPS_DMC268_P"),
        ({"VPS_HOST": "203.0.113.10\nport=2222"}, "is not a hostname or IPv4 address"),
        ({"APP_DOMAIN": ""}, "APP_DOMAIN"),
        ({"APP_DOMAIN": "example.org/path"}, "APP_DOMAIN"),
        ({"APP_DOMAIN": "example.org\nhost=evil.test"}, "APP_DOMAIN"),
        ({"STAGING_SSH_FINGERPRINT": ""}, "STAGING_SSH_FINGERPRINT"),
        ({"STAGING_SSH_FINGERPRINT": "SHA256:short"}, "STAGING_SSH_FINGERPRINT"),
    ],
    ids=[
        "terraform-host",
        "no-vps-host",
        "no-vps-user",
        "no-vps-password",
        "multiline-host",
        "no-domain",
        "domain-with-path",
        "multiline-domain",
        "no-fingerprint",
        "bad-fingerprint",
    ],
)
def test_an_incomplete_or_foreign_target_is_refused(
    tmp_path: Path, override: dict[str, str], message: str
) -> None:
    result, output, _ = _run(TARGET, tmp_path, {**COURSE_VPS, **override})

    assert result.returncode == 1
    assert message in result.stderr
    assert output == ""


# --- the deploy on the VPS: no edge refresh, no app secrets, auto rollback (AC2-AC4) ----------

CI_WORKFLOW = WORKFLOW.parent / "ci-cd.yml"
HOST_STEPS = [
    CHECKOUT,
    UPLOAD,
    PREPARE,
    WAIT_TLS,
    DEPLOY,
    HEALTH,
    ISOLATION,
    ROLLBACK,
    FAIL,
    AFTER,
    REPORT,
]
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
    return {
        key: re.sub(r"\$\{\{ (.+?) \}\}", lambda match: context[match.group(1)], env[key])
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
    }


def _deploy_step(
    host: Host, image: str, deploy_run: str, **stubs: str
) -> subprocess.CompletedProcess[str]:
    """The "Deploy image" step of the run `deploy_run`; `image` is the resolved digest."""
    assert _ssh_script(_step(DEPLOY)).endswith('\n"${APP_DIR}/deploy.sh" "${IMAGE}"')
    context = {**_host_context(host, deploy_run), "steps.digest.outputs.image": image}
    env = _forwarded(DEPLOY, context)
    return host.run("deploy.sh", env["IMAGE"], env={**env, **stubs})


def _rollback_step(host: Host, deploy_run: str) -> subprocess.CompletedProcess[str]:
    """The auto rollback after a failed deploy or health check of the run `deploy_run`."""
    assert _ssh_script(_step(ROLLBACK)).endswith('\nROLLBACK_MODE=auto "${APP_DIR}/rollback.sh"')
    # No image in the context: the rollback target is the prod dir's previous release only.
    env = _forwarded(ROLLBACK, _host_context(host, deploy_run))
    return host.run("rollback.sh", env={**env, "ROLLBACK_MODE": "auto"})


def _assert_prod_only(host: Host) -> None:
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
    # No bundle reaches the host, so every role file stays empty until Part 2.
    for name in ENV_FILES:
        assert (host.app_dir / name).read_text(encoding="utf-8") == "", name


def test_the_deploy_job_changes_the_host_only_after_the_checks() -> None:
    assert _step_names(_job("deploy-prod")) == [AT_JOB_START, REFUSE_MOVED, TARGET, *HOST_STEPS]
    assert "        uses: actions/checkout@v4\n" in _step(CHECKOUT)


def test_no_step_refreshes_the_edge_proxy() -> None:
    # Prod relies on the edge proxy as staging deploys it. No edge files, no edge compose
    # project, no config reload; provision.sh only creates the edge network if it is missing.
    code = "\n".join(line for line in _text().splitlines() if not line.lstrip().startswith("#"))
    for needle in ("deploy/edge", "dmc-268-edge", "caddy"):
        assert needle not in code.lower(), needle
    assert "          EDGE_NETWORK: dmc268-edge\n" in _step(PREPARE)


def test_every_host_step_logs_in_to_the_course_vps_with_its_pinned_key() -> None:
    pins = {
        line.strip()
        for line in CI_WORKFLOW.read_text(encoding="utf-8").splitlines()
        if line.lstrip().startswith("uses: appleboy/")
    }
    remote = [name for name in _step_names(_job("deploy-prod")) if "appleboy/" in _step(name)]
    assert remote == [UPLOAD, PREPARE, DEPLOY, ISOLATION, ROLLBACK, AFTER]
    for name in remote:
        step = _step(name)
        uses = re.search(r"(?m)^        (uses: appleboy/.+)$", step)
        assert uses is not None and uses.group(1) in pins, name
        assert VPS_LOGIN in step, name
        assert re.search(r"(?m)^          key:", step) is None, name
    for name in (PREPARE, DEPLOY, ISOLATION, ROLLBACK, AFTER):
        step = _step(name)
        assert "          debug: false\n" in step, name
        script = _ssh_script(step)
        assert script.startswith("set -euo pipefail\nset +o xtrace\n"), name
        assert "${{" not in script, name


def test_the_upload_sends_the_staging_deploy_files_and_the_isolation_check() -> None:
    staging = re.search(r'(?m)^          source: "(.+)"$', _workflow_step(UPLOAD))
    assert staging is not None
    upload = _step(UPLOAD)
    assert f'          source: "{staging.group(1)},deploy/scripts/check-isolation.sh"\n' in upload
    assert (
        "          target: ${{ steps.target.outputs.app_dir }}\n          strip_components: 2\n"
    ) in upload


def _prepare(tmp_path: Path, **stubs: str) -> tuple[subprocess.CompletedProcess[str], Path]:
    """Run the Prepare host script on the uploaded files; provision.sh only names its network."""
    app_dir = tmp_path / "opt" / PROD_PROJECT
    app_dir.mkdir(parents=True)
    source = re.search(r'(?m)^          source: "(.+)"$', _step(UPLOAD))
    assert source is not None
    for path in source.group(1).split(","):
        (app_dir / Path(path).name).write_text("", encoding="utf-8")
    (app_dir / "provision.sh").write_text('#!/bin/bash\necho "provisioned ${EDGE_NETWORK}"\n')
    env = _forwarded(PREPARE, {"steps.target.outputs.app_dir": str(app_dir)})
    result, _, _ = _run(PREPARE, tmp_path, {**env, **stubs}, script=_ssh_script(_step(PREPARE)))
    return result, app_dir


@pytest.mark.parametrize(
    ("listed", "line"),
    [
        ("\n".join(STAGING_IDS) + "\n", "staging containers before: " + " ".join(STAGING_IDS)),
        ("", "staging containers before:"),
    ],
    ids=["staging-running", "fresh-vps"],
)
def test_prepare_host_provisions_the_prod_dir_and_lists_the_staging_containers(
    tmp_path: Path, listed: str, line: str
) -> None:
    step = _step(PREPARE)
    # The report compares it with the list read after the deploy (AC3: staging is not recreated).
    assert "        id: prepare\n" in step
    assert "          capture_stdout: true\n" in step

    result, app_dir = _prepare(tmp_path, STUB_PS_OUTPUT=listed)

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["provisioned dmc268-edge", line]
    assert _docker_calls(tmp_path) == [STAGING_PS]
    names = {path.name for path in app_dir.iterdir()}
    assert {"compose.yml", "compose.ports.yml", "compose.edge.yml"} <= names
    assert not [name for name in names if name.startswith("staging.")]
    for script in ("provision.sh", "deploy.sh", "rollback.sh", "healthcheck.sh", "env-file.sh"):
        assert os.access(app_dir / script, os.X_OK), script
    assert os.access(app_dir / "check-isolation.sh", os.X_OK)


def test_prepare_host_fails_when_the_staging_containers_cannot_be_listed(tmp_path: Path) -> None:
    result, _ = _prepare(tmp_path, STUB_PS_STATUS="1")

    assert result.returncode != 0
    assert "staging containers before" not in result.stdout


def test_the_deploy_gets_only_the_digest_resolved_at_job_start() -> None:
    # AC2: deploy.sh gets the @sha256: output of the re-resolve step, which the Refuse step has
    # just compared with the dispatch-time digest; no other image reaches the host.
    text = _text()
    assert [line.strip() for line in text.splitlines() if ".outputs.image" in line] == [
        "image: ${{ steps.digest.outputs.image }}",
        "DISPATCHED: ${{ needs.resolve.outputs.image }}",
        "CURRENT: ${{ steps.digest.outputs.image }}",
        "IMAGE: ${{ steps.digest.outputs.image }}",
    ]
    deploy = _step(DEPLOY)
    assert re.findall(r"(?m)^ +IMAGE:.*$", text) == [
        "          IMAGE: ${{ steps.digest.outputs.image }}"
    ]
    assert "          IMAGE: ${{ steps.digest.outputs.image }}\n" in deploy
    # No `if:`: a refused or unresolved digest has already failed the job.
    assert re.search(r"(?m)^        if:", deploy) is None
    assert _ssh_script(deploy) == (
        'set -euo pipefail\nset +o xtrace\n"${APP_DIR}/deploy.sh" "${IMAGE}"'
    )


def test_prod_gets_no_application_or_store_secrets() -> None:
    # No app bundle and nothing of staging's store settings until Part 2; deploy.sh generates
    # the store passwords and creates empty role files (/api/* and webhooks answer 503).
    text = _text()
    assert set(re.findall(r"secrets\.(\w+)", text)) == {
        "GITHUB_TOKEN",
        "VPS_DMC268_U",
        "VPS_DMC268_P",
    }
    assert set(re.findall(r"vars\.(\w+)", text)) == {
        "STAGING_HOST",
        "VPS_DMC268_IP_T6",
        "APP_DOMAIN",
        "STAGING_SSH_FINGERPRINT",
    }
    assert "APP_SECRETS_B64" not in text
    assert "POSTGRES_" not in text
    shared = "APP_DIR,DEPLOY_MODE,EDGE_ALIAS,NO_PREVIOUS_RELEASE,DEPLOY_RUN"
    assert f"          envs: GHCR_TOKEN,GHCR_USER,IMAGE,{shared}\n" in _step(DEPLOY)
    assert f"          envs: GHCR_TOKEN,GHCR_USER,{shared}\n" in _step(ROLLBACK)
    for name in (DEPLOY, ROLLBACK):
        step = _step(name)
        assert "          DEPLOY_MODE: edge\n" in step, name
        assert "          EDGE_ALIAS: ${{ steps.target.outputs.edge_alias }}\n" in step, name
        assert "          NO_PREVIOUS_RELEASE: stop\n" in step, name
        assert "          DEPLOY_RUN: ${{ github.run_id }}-${{ github.run_attempt }}\n" in step


def test_the_edge_wait_and_the_health_check_run_the_staging_scripts() -> None:
    for name in (WAIT_TLS, HEALTH):
        assert _run_script(_step(name)) == _run_script(_workflow_step(name)), name
    wait = _step(WAIT_TLS)
    assert "          BASE_URL: ${{ steps.target.outputs.base_url }}\n" in wait
    assert re.search(r"(?m)^        if:", wait) is None  # prod is always behind the edge proxy
    health = _step(HEALTH)
    assert (
        "        id: health\n"
        "        if: steps.deploy.outcome == 'success'\n"
        "        continue-on-error: true\n"
    ) in health
    assert "          HEALTH_URL: ${{ steps.target.outputs.health_url }}\n" in health
    rollback = _step(ROLLBACK)
    assert (
        "        id: rollback\n"
        "        if: always() && (steps.deploy.outcome == 'failure'"
        " || steps.health.outcome == 'failure')\n"
        "        continue-on-error: true\n"
    ) in rollback
    assert "          capture_stdout: true\n" in rollback


@pytest.mark.parametrize("failing", ["stack-start", "health-check"])
def test_a_failed_first_prod_release_is_only_taken_down(tmp_path: Path, failing: str) -> None:
    # AC4: without a previous prod release the stack goes down and the edge proxy answers its 503
    # stub for the prod API host; no nginx bootstrap container takes the alias.
    host = make_host(tmp_path, project=PROD_PROJECT)
    stubs = {"STUB_FAILING_IMAGE": IMAGE_A} if failing == "stack-start" else {}
    deploy = _deploy_step(host, IMAGE_A, "401-1", **stubs)
    assert deploy.returncode == (1 if stubs else 0), deploy.stderr

    result = _rollback_step(host, "401-1")

    assert result.returncode == 0, result.stderr
    assert STOPPED in result.stdout
    _assert_stopped(host, host.calls())
    _assert_prod_only(host)


def test_a_prod_release_failing_its_health_check_brings_back_the_previous_one(
    tmp_path: Path,
) -> None:
    # AC3: the previous release of the prod dir comes back; staging is never addressed.
    host = make_host(tmp_path, project=PROD_PROJECT)
    assert _deploy_step(host, IMAGE_A, "401-1").returncode == 0
    assert _deploy_step(host, IMAGE_B, "402-1").returncode == 0

    result = _rollback_step(host, "402-1")

    assert result.returncode == 0, result.stderr
    assert f"rolled back to {IMAGE_A}" in result.stdout
    state = _read_dotenv(host.app_dir / ".deploy-state")
    assert (state["current_image"], state["rolled_back"]) == (IMAGE_A, "true")
    assert _read_dotenv(host.env_file)["IMAGE"] == IMAGE_A
    _assert_prod_only(host)


@pytest.mark.parametrize(
    ("deploy", "rollback", "unchanged", "message"),
    [
        (
            "failure",
            "success",
            "true",
            "prod deploy failed before the release switch; the running release was left unchanged",
        ),
        ("failure", "success", "false", "prod deploy failed; rollback was triggered"),
        ("success", "success", "false", "prod health check failed; rollback was triggered"),
        ("failure", "failure", "false", "prod deploy failed; rollback was triggered"),
        ("success", "failure", "false", "prod health check failed; rollback was triggered"),
    ],
    ids=[
        "release-unchanged",
        "deploy-rolled-back",
        "health-rolled-back",
        "deploy-rollback-failed",
        "health-rollback-failed",
    ],
)
def test_the_run_fails_after_a_rollback_and_says_why(
    tmp_path: Path, deploy: str, rollback: str, unchanged: str, message: str
) -> None:
    step = _step(FAIL)
    assert (
        "        if: always() && (steps.deploy.outcome == 'failure'"
        " || steps.health.outcome == 'failure')\n"
    ) in step
    assert "          DEPLOY_OUTCOME: ${{ steps.deploy.outcome }}\n" in step
    assert "          ROLLBACK_OUTCOME: ${{ steps.rollback.outcome }}\n" in step
    # The line rollback.sh prints when deploy.sh of this run stopped before the release switch;
    # GitHub renders contains() to true/false, so the captured stdout never enters the script.
    literal = _release_unchanged_literal()
    assert (
        '          RELEASE_UNCHANGED: "${{ contains(steps.rollback.outputs.stdout, '
        f"'{literal}') }}}}\"\n"
    ) in step

    result, output, summary = _run(
        FAIL,
        tmp_path,
        {"DEPLOY_OUTCOME": deploy, "ROLLBACK_OUTCOME": rollback, "RELEASE_UNCHANGED": unchanged},
    )

    assert result.returncode == 1
    assert result.stdout == message + "\n"
    assert ("rollback failed" in result.stderr) is (rollback == "failure")
    assert (output, summary) == ("", "")


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
