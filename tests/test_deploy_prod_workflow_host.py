"""Deploy prod workflow on the VPS: host steps, deploy and rollback of the release (#308 Part 1)."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

from tests.deploy_host_fakes import (
    IMAGE_A,
    IMAGE_B,
    PROD_PROJECT,
    SECRET_SOURCES,
    STOPPED,
    VARIABLE_SOURCES,
    Host,
    _assert_stopped,
    _read_dotenv,
    _release_unchanged_literal,
    _run_script,
    _workflow_step,
    make_host,
)
from tests.deploy_prod_workflow_fakes import (
    AFTER,
    AT_JOB_START,
    BUNDLE,
    CHECKOUT,
    CI_WORKFLOW,
    DEPLOY,
    FAIL,
    HEALTH,
    ISOLATION,
    REFUSE_MOVED,
    REFUSE_SECRETS,
    REPORT,
    ROLLBACK,
    STAGING_IDS,
    STAGING_PS,
    TARGET,
    UPLOAD,
    VALIDATE,
    VPS_LOGIN,
    _assert_prod_only,
    _deploy_step,
    _docker_calls,
    _forwarded,
    _host_context,
    _job,
    _run,
    _ssh_script,
    _step,
    _step_names,
    _text,
)

PREPARE = "Prepare host"
WAIT_TLS = "Wait for the edge certificate"


# --- the deploy on the VPS: no edge refresh, bundled app secrets, auto rollback (AC2-AC4) -----


HOST_STEPS = [
    CHECKOUT,
    BUNDLE,
    VALIDATE,
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


def _rollback_step(host: Host, deploy_run: str) -> subprocess.CompletedProcess[str]:
    """The auto rollback after a failed deploy or health check of the run `deploy_run`."""
    assert _ssh_script(_step(ROLLBACK)).endswith('\nROLLBACK_MODE=auto "${APP_DIR}/rollback.sh"')
    # No image in the context: the rollback target is the prod dir's previous release only.
    env = _forwarded(ROLLBACK, _host_context(host, deploy_run))
    return host.run("rollback.sh", env={**env, "ROLLBACK_MODE": "auto"})


def test_the_deploy_job_changes_the_host_only_after_the_checks() -> None:
    assert _step_names(_job("deploy-prod")) == [
        AT_JOB_START,
        REFUSE_MOVED,
        TARGET,
        REFUSE_SECRETS,
        *HOST_STEPS,
    ]
    assert "        uses: actions/checkout@v7\n" in _step(CHECKOUT)


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
        "IMAGE: ${{ steps.digest.outputs.image }}",  # the bundle preflight pulls it
        "IMAGE: ${{ steps.digest.outputs.image }}",
    ]
    deploy = _step(DEPLOY)
    assert (
        re.findall(r"(?m)^ +IMAGE:.*$", text)
        == ["          IMAGE: ${{ steps.digest.outputs.image }}"] * 2
    )
    assert "          IMAGE: ${{ steps.digest.outputs.image }}\n" in deploy
    # No `if:`: a refused or unresolved digest has already failed the job.
    assert re.search(r"(?m)^        if:", deploy) is None
    assert _ssh_script(deploy) == (
        'set -euo pipefail\nset +o xtrace\n"${APP_DIR}/deploy.sh" "${IMAGE}"'
    )


def test_prod_reads_the_production_bundle_and_no_store_settings() -> None:
    # The application secrets come from Environment production, bundled like staging's; the store
    # passwords stay on the host (deploy.sh generates them and creates the role files).
    text = _text()
    assert set(re.findall(r"(?<!\w)secrets\.(\w+)", text)) == {
        "GITHUB_TOKEN",
        "VPS_DMC268_U",
        "VPS_DMC268_P",
        *SECRET_SOURCES.values(),
    }
    assert set(re.findall(r"vars\.(\w+)", text)) == {
        "STAGING_HOST",
        "VPS_DMC268_IP_T6",
        "APP_DOMAIN",
        "STAGING_SSH_FINGERPRINT",
        *VARIABLE_SOURCES.values(),
    }
    assert "POSTGRES_" not in text
    carriers = [
        name for name in _step_names(_job("deploy-prod")) if "APP_SECRETS_B64" in _step(name)
    ]
    assert carriers == [VALIDATE, DEPLOY]
    assert "          APP_SECRETS_B64: ${{ steps.app_secrets.outputs.bundle }}\n" in _step(DEPLOY)
    shared = "APP_DIR,DEPLOY_MODE,EDGE_ALIAS,NO_PREVIOUS_RELEASE,DEPLOY_RUN"
    assert f"          envs: GHCR_TOKEN,GHCR_USER,IMAGE,APP_SECRETS_B64,{shared}\n" in _step(DEPLOY)
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
