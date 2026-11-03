"""The worker replica count (#308): compose scaling, and what deploy and rollback do with it.

The compose tests render the real file with the Docker Compose CLI; the host tests reuse the fake
host of the staging deploy tests, which records the replica value `up` receives.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.test_deploy_prod import LEGACY_VALUES, _write_compose_env_file
from tests.test_deploy_staging_services import (
    IMAGE_A,
    IMAGE_B,
    STAGING_COMPOSE,
    Host,
    _read_dotenv,
    make_host,
)

ROLE_FILES = (
    "api.env",
    "app.env",
    "worker.env",
    "webhook-worker.env",
    "gitlab.env",
    "bitbucket.env",
)
REQUIRED = {
    "IMAGE": "example.test/api:x",
    "POSTGRES_PASSWORD": "x",
    "RABBITMQ_PASSWORD": "x",
    "REDIS_PASSWORD": "x",
}


def _rendered_services(
    tmp_path: Path, *, dotenv: dict[str, str] | None = None, shell: dict[str, str] | None = None
) -> dict[str, dict[str, object]]:
    if shutil.which("docker") is None:
        pytest.skip("Docker Compose CLI is required for rendering")
    shutil.copy(STAGING_COMPOSE, tmp_path / "compose.yml")
    for name in ROLE_FILES:
        (tmp_path / name).touch()
    lines = {**REQUIRED, **(dotenv or {})}
    (tmp_path / ".env").write_text("".join(f"{k}={v}\n" for k, v in lines.items()))
    result = subprocess.run(
        ["docker", "compose", "--project-directory", str(tmp_path), "config", "--format", "json"],
        env={"PATH": os.environ["PATH"], "HOME": os.environ["HOME"], **(shell or {})},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    services: dict[str, dict[str, object]] = json.loads(result.stdout)["services"]
    return services


@pytest.mark.parametrize(
    ("dotenv", "shell", "expected"),
    [
        pytest.param({}, {}, 1, id="unset"),
        pytest.param({"WORKER_REPLICAS": ""}, {}, 1, id="empty"),
        pytest.param({"WORKER_REPLICAS": "3"}, {}, 3, id="dotenv"),
        pytest.param({}, {"WORKER_REPLICAS": "3"}, 3, id="shell"),
    ],
)
def test_compose_renders_the_worker_replica_count(
    tmp_path: Path, dotenv: dict[str, str], shell: dict[str, str], expected: int
) -> None:
    services = _rendered_services(tmp_path, dotenv=dotenv, shell=shell)

    deploy = services["worker"]["deploy"]
    assert isinstance(deploy, dict)
    assert deploy["replicas"] == expected


def test_only_the_worker_is_replicated(tmp_path: Path) -> None:
    services = _rendered_services(tmp_path, dotenv={"WORKER_REPLICAS": "3"})

    replicated = set()
    for name, service in services.items():
        deploy = service.get("deploy")
        if isinstance(deploy, dict) and "replicas" in deploy:
            replicated.add(name)
    assert replicated == {"worker"}


@pytest.fixture
def host(tmp_path: Path) -> Host:
    return make_host(tmp_path)


def _up_replicas(host: Host) -> str:
    """The WORKER_REPLICAS value the last `compose up` was started with."""
    return (host.log.parent / "up-replicas").read_text(encoding="utf-8").strip()


def _deploy(host: Host, image: str, replicas: str | None) -> subprocess.CompletedProcess[str]:
    env = {} if replicas is None else {"WORKER_REPLICAS": replicas}
    return host.deploy(image, bundle="", env=env)


def test_deploy_applies_the_ci_replica_count(host: Host) -> None:
    result = _deploy(host, IMAGE_A, "3")

    assert result.returncode == 0, result.stderr
    assert "worker replicas: 3\n" in result.stdout
    assert _read_dotenv(host.env_file)["WORKER_REPLICAS"] == "3"
    assert _up_replicas(host) == "3"
    assert _read_dotenv(host.app_dir / ".deploy-state")["worker_replicas"] == "3"


def test_ci_value_one_scales_a_host_back_to_one_replica(host: Host) -> None:
    assert _deploy(host, IMAGE_A, "3").returncode == 0

    result = _deploy(host, IMAGE_B, "1")

    assert result.returncode == 0, result.stderr
    assert "worker replicas: 1\n" in result.stdout
    assert _read_dotenv(host.env_file)["WORKER_REPLICAS"] == "1"
    assert _up_replicas(host) == "1"


def test_manual_deploy_without_the_variable_keeps_the_host_count(host: Host) -> None:
    assert _deploy(host, IMAGE_A, "3").returncode == 0

    result = _deploy(host, IMAGE_B, None)

    assert result.returncode == 0, result.stderr
    assert "worker replicas: 3\n" in result.stdout
    assert _read_dotenv(host.env_file)["WORKER_REPLICAS"] == "3"
    assert _up_replicas(host) == "3"


def test_fresh_host_without_the_variable_runs_one_replica(host: Host) -> None:
    result = _deploy(host, IMAGE_A, None)

    assert result.returncode == 0, result.stderr
    assert "worker replicas: 1\n" in result.stdout
    assert _read_dotenv(host.env_file)["WORKER_REPLICAS"] == "1"
    assert _up_replicas(host) == "1"


@pytest.mark.parametrize(
    ("source", "value"),
    [
        *[("env", v) for v in ("0", "6", "03", "abc", "-1", "2 ", "10", "99999999999999999999")],
        *[("dotenv", v) for v in ("0", "6", "03", "abc", "-1", "10", "99999999999999999999")],
    ],
)
def test_invalid_replica_count_is_refused_before_the_host_changes(
    host: Host, source: str, value: str
) -> None:
    assert _deploy(host, IMAGE_A, "2").returncode == 0
    if source == "dotenv":
        text = host.env_file.read_text(encoding="utf-8")
        host.env_file.write_text(
            text.replace("WORKER_REPLICAS=2\n", f"WORKER_REPLICAS={value}\n"), encoding="utf-8"
        )
        replicas = None
    else:
        replicas = value
    before = host.files()
    calls_before = len(host.calls())

    result = _deploy(host, IMAGE_B, replicas)

    assert result.returncode != 0
    assert f"WORKER_REPLICAS must be an integer from 1 to 5, got: {value}" in result.stderr
    assert host.files() == before
    # Only the EXIT trap's credential cleanup reaches docker.
    assert set(host.calls()[calls_before:]) <= {"logout ghcr.io"}


@pytest.mark.parametrize("values_given", [9, 10, 11], ids=["10-args", "11-args", "12-args"])
def test_env_file_writer_takes_an_optional_replica_count(tmp_path: Path, values_given: int) -> None:
    # The values after the file: callers from before NO_PREVIOUS_RELEASE pass 9, then 10, now 11.
    values = [*LEGACY_VALUES, "stop", "3"][:values_given]
    env_file = tmp_path / ".env"

    _write_compose_env_file(env_file, *values)

    expected = "WORKER_REPLICAS=3\n" if values_given == 11 else "WORKER_REPLICAS=\n"
    assert expected in env_file.read_text(encoding="utf-8")


def test_deploy_records_the_count_of_the_release_it_replaces(host: Host) -> None:
    assert _deploy(host, IMAGE_A, "2").returncode == 0

    assert _deploy(host, IMAGE_B, "3").returncode == 0

    assert _read_dotenv(host.app_dir / ".deploy-state.previous")["worker_replicas"] == "2"
    assert _read_dotenv(host.app_dir / ".deploy-state")["worker_replicas"] == "3"


def test_release_recorded_without_a_count_is_replaced_with_the_count_the_host_runs(
    host: Host,
) -> None:
    assert _deploy(host, IMAGE_A, "2").returncode == 0
    state = host.app_dir / ".deploy-state"
    state.write_text(
        "".join(line + "\n" for line in state.read_text().splitlines() if "worker" not in line)
    )

    assert _deploy(host, IMAGE_B, "3").returncode == 0

    assert _read_dotenv(host.app_dir / ".deploy-state.previous")["worker_replicas"] == "2"
