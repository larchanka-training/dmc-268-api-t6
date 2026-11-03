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

from tests.test_deploy_staging_services import STAGING_COMPOSE

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
