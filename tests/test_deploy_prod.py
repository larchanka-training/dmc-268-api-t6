"""The deploy and rollback scripts on a fake prod host (#308).

The prod stack lives next to staging on the course VPS: its own compose project, app dir and
alias. A prod release that fails without a previous one to go back to is only taken down
(NO_PREVIOUS_RELEASE=stop): the edge proxy then answers 503 for the prod API instead of the nginx
welcome page of a bootstrap container (AC4). No call of a prod deploy or rollback leaves the prod
compose project (AC3).
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Mapping
from pathlib import Path

import pytest

from tests.test_deploy_staging_services import (
    IMAGE_A,
    IMAGE_B,
    REPO_ROOT,
    Host,
    _read_dotenv,
    make_host,
)

PROD_PROJECT = "dmc-268-api-prod"
STOPPED = (
    f"stopped compose project {PROD_PROJECT}; no bootstrap container (NO_PREVIOUS_RELEASE=stop)"
)
INVALID = "NO_PREVIOUS_RELEASE must be bootstrap or stop, got: nginx"
# The variables the prod "Deploy image" step hands to deploy.sh: no bundle, no POSTGRES_*.
PROD_DEPLOY_ENV = {"DEPLOY_MODE": "edge", "EDGE_ALIAS": "api-prod", "NO_PREVIOUS_RELEASE": "stop"}


@pytest.fixture
def prod_host(tmp_path: Path) -> Host:
    return make_host(tmp_path, project=PROD_PROJECT)


def _deploy(
    host: Host, image: str, env: Mapping[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return host.run("deploy.sh", image, env={**PROD_DEPLOY_ENV, **(env or {})})


def _ci_rollback(
    host: Host, deploy_run: str, *, forward_no_previous: bool = True
) -> subprocess.CompletedProcess[str]:
    """The prod "Rollback on failed deploy or health check" step of the run `deploy_run`."""
    env = {
        "GHCR_TOKEN": "test-ghcr-token",
        "GHCR_USER": "github",
        "APP_DIR": str(host.app_dir),
        "DEPLOY_MODE": "edge",
        "EDGE_ALIAS": "api-prod",
        "DEPLOY_RUN": deploy_run,
        "ROLLBACK_MODE": "auto",
    }
    if forward_no_previous:
        env["NO_PREVIOUS_RELEASE"] = "stop"
    return host.run("rollback.sh", env=env)


def _assert_stopped(host: Host, calls: list[str]) -> None:
    down = calls.index(f"compose -p {PROD_PROJECT} down --remove-orphans")
    assert calls.index(f"compose -p {PROD_PROJECT} ps -aq") > down
    # An nginx left by an earlier bootstrap-mode rollback would keep answering under the alias.
    assert f"rm -f {PROD_PROJECT}-bootstrap" in calls
    assert not any(call.startswith("run -d") for call in calls)
    assert not any(call.startswith("pull nginx") for call in calls)
    assert not (host.app_dir / ".deploy-state").exists()
    assert not (host.app_dir / ".deploy-state.previous").exists()


def _write_compose_env_file(path: Path, *args: str) -> None:
    subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; shift; write_compose_env_file "$@"',
            "env-file",
            str(REPO_ROOT / "deploy" / "scripts" / "env-file.sh"),
            str(path),
            *args,
        ],
        check=True,
    )


# With the file first, the ten arguments of a caller from before NO_PREVIOUS_RELEASE
# (scripts/verify_rollback.py).
LEGACY_VALUES = (IMAGE_A, "app", "pg", "app", "edge", "api-prod", "app", "mq", "redis")


def test_failed_first_deploy_in_stop_mode_only_takes_the_stack_down(prod_host: Host) -> None:
    result = _deploy(prod_host, IMAGE_A, {"DEPLOY_RUN": "201-1", "STUB_FAILING_IMAGE": IMAGE_A})

    assert result.returncode == 1
    assert STOPPED in result.stdout
    _assert_stopped(prod_host, prod_host.calls())
    env = _read_dotenv(prod_host.env_file)
    for name in ("POSTGRES_PASSWORD", "RABBITMQ_PASSWORD", "REDIS_PASSWORD"):
        assert re.fullmatch(r"[0-9a-f]{48}", env[name]), name
    assert env["NO_PREVIOUS_RELEASE"] == "stop"


@pytest.mark.parametrize("forward_no_previous", [True, False], ids=["from-envs", "from-dotenv"])
def test_ci_rollback_after_a_failed_first_health_check_stops_the_stack(
    prod_host: Host, forward_no_previous: bool
) -> None:
    assert _deploy(prod_host, IMAGE_A, {"DEPLOY_RUN": "202-1"}).returncode == 0
    calls_before = len(prod_host.calls())

    result = _ci_rollback(prod_host, "202-1", forward_no_previous=forward_no_previous)

    assert result.returncode == 0, result.stderr
    assert STOPPED in result.stdout
    _assert_stopped(prod_host, prod_host.calls()[calls_before:])
    env = _read_dotenv(prod_host.env_file)
    assert env["NO_PREVIOUS_RELEASE"] == "stop"
    assert env["POSTGRES_PASSWORD"]


@pytest.mark.parametrize(
    "stubs",
    [{"STUB_DOWN_STATUS": "1"}, {"STUB_PS_OUTPUT": "0123456789ab"}, {"STUB_PS_STATUS": "1"}],
    ids=["down", "ps", "ps-fails"],
)
def test_stop_mode_fails_while_the_project_may_still_run(
    prod_host: Host, stubs: dict[str, str]
) -> None:
    assert _deploy(prod_host, IMAGE_A).returncode == 0
    calls_before = len(prod_host.calls())

    result = prod_host.run("rollback.sh", env=stubs)

    assert result.returncode != 0
    assert f"failed to stop compose project {PROD_PROJECT}" in result.stderr
    assert STOPPED not in result.stdout
    assert not any(call.startswith("run ") for call in prod_host.calls()[calls_before:])
    # The release may still run: its state stays.
    assert (prod_host.app_dir / ".deploy-state").exists()


@pytest.mark.parametrize("source", ["env", "dotenv"])
@pytest.mark.parametrize("script", ["deploy.sh", "rollback.sh"])
def test_invalid_no_previous_release_is_refused_before_the_host_changes(
    prod_host: Host, script: str, source: str
) -> None:
    assert _deploy(prod_host, IMAGE_A).returncode == 0
    if source == "dotenv":
        env = prod_host.env_file.read_text(encoding="utf-8")
        prod_host.env_file.write_text(
            env.replace("NO_PREVIOUS_RELEASE=stop\n", "NO_PREVIOUS_RELEASE=nginx\n"),
            encoding="utf-8",
        )
        override = {"NO_PREVIOUS_RELEASE": ""}
    else:
        override = {"NO_PREVIOUS_RELEASE": "nginx"}
    before = prod_host.files()
    calls_before = len(prod_host.calls())

    if script == "deploy.sh":
        result = _deploy(prod_host, IMAGE_B, override)
    else:
        result = prod_host.run("rollback.sh", env=override)

    assert result.returncode != 0
    assert INVALID in result.stderr
    assert prod_host.files() == before
    # Only the EXIT trap's credential cleanup reaches docker.
    assert set(prod_host.calls()[calls_before:]) <= {"logout ghcr.io"}


def test_second_stop_mode_rollback_is_idempotent(prod_host: Host) -> None:
    assert _deploy(prod_host, IMAGE_A, {"STUB_FAILING_IMAGE": IMAGE_A}).returncode == 1
    calls_before = len(prod_host.calls())

    # An operator's rollback: NO_PREVIOUS_RELEASE comes from .env only.
    result = prod_host.run("rollback.sh")

    assert result.returncode == 0, result.stderr
    assert STOPPED in result.stdout
    _assert_stopped(prod_host, prod_host.calls()[calls_before:])


def test_empty_no_previous_release_keeps_the_bootstrap_container(prod_host: Host) -> None:
    _write_compose_env_file(prod_host.env_file, *LEGACY_VALUES)

    result = prod_host.run("rollback.sh")

    assert result.returncode == 0, result.stderr
    assert "restored bootstrap container" in result.stdout
    started = [call for call in prod_host.calls() if call.startswith("run -d --name")]
    assert len(started) == 1
    assert f"--name {PROD_PROJECT}-bootstrap" in started[0]
    assert "--network-alias api-prod" in started[0]


def test_ten_argument_env_file_call_writes_an_empty_no_previous_release(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"

    _write_compose_env_file(env_file, *LEGACY_VALUES)

    assert "NO_PREVIOUS_RELEASE=\n" in env_file.read_text(encoding="utf-8").splitlines(True)


def test_prod_deploy_and_auto_rollback_stay_in_the_prod_project(prod_host: Host) -> None:
    assert _deploy(prod_host, IMAGE_A, {"DEPLOY_RUN": "301-1"}).returncode == 0
    failed = _deploy(prod_host, IMAGE_B, {"DEPLOY_RUN": "302-1", "STUB_FAILING_IMAGE": IMAGE_B})
    assert failed.returncode == 1
    assert f"rolled back to {IMAGE_A}" in failed.stdout
    rollback = _ci_rollback(prod_host, "302-1")
    assert rollback.returncode == 0, rollback.stderr

    calls = prod_host.calls()
    assert not [call for call in calls if "api-staging" in call]  # project, volumes, alias
    compose = [call for call in calls if call.startswith("compose ")]
    assert compose
    assert all(call.startswith(f"compose -p {PROD_PROJECT} ") for call in compose)
    volumes = [call for call in calls if call.startswith("volume ")]
    assert volumes
    assert all(f" {PROD_PROJECT}_" in call for call in volumes)
    # A rollback rewrites .env: stop mode survives it for a later release without a previous one.
    env = _read_dotenv(prod_host.env_file)
    assert env["IMAGE"] == IMAGE_A
    assert env["NO_PREVIOUS_RELEASE"] == "stop"
