"""Rendered staging Compose keeps credential rotation as an explicit operator job."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.deploy_host_fakes import (
    CI_SECRETS,
    IMAGE_A,
    STAGING_COMPOSE,
    _keyring,
    _write_application_bundle,
)


def test_compose_rotation_operator_receives_both_provider_keyrings(tmp_path: Path) -> None:
    if shutil.which("docker") is None:
        pytest.skip("Docker Compose CLI is required for recipient rendering")
    provider_keys = {
        "BITBUCKET_CREDENTIAL_KEYS": _keyring(key_id="bitbucket-current"),
        "BITBUCKET_ACTIVE_KEY_ID": "bitbucket-current",
        "GITLAB_CREDENTIAL_KEYS": _keyring(key_id="gitlab-current"),
        "GITLAB_ACTIVE_KEY_ID": "gitlab-current",
    }
    _write_application_bundle(tmp_path, {**CI_SECRETS, **provider_keys})
    shutil.copy(STAGING_COMPOSE, tmp_path / "compose.yml")
    (tmp_path / ".env").write_text(
        f"IMAGE={IMAGE_A}\nPOSTGRES_PASSWORD=test-db\nRABBITMQ_PASSWORD=test-broker\n"
        "REDIS_PASSWORD=test-cache\n"
    )
    command = ["docker", "compose", "--project-directory", str(tmp_path)]
    environment = {"PATH": os.environ["PATH"], "HOME": os.environ["HOME"]}
    default = subprocess.run(
        [*command, "config", "--format", "json"],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert default.returncode == 0, default.stderr
    assert "credential-key-rotation" not in json.loads(default.stdout)["services"]
    rendered = subprocess.run(
        [*command, "--profile", "credential-operator", "config", "--format", "json"],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert rendered.returncode == 0, rendered.stderr
    operator = json.loads(rendered.stdout)["services"]["credential-key-rotation"]
    actual = operator["environment"]
    assert {key: actual[key] for key in provider_keys} == provider_keys
    assert actual["DATABASE_URL"] == "postgresql+psycopg://app:test-db@postgres:5432/app"
    assert "GITHUB_APP_PRIVATE_KEY" not in actual
    assert operator["image"] == IMAGE_A
    assert operator["profiles"] == ["credential-operator"]
    assert operator["entrypoint"] == ["python", "-m", "app.credential_key_rotation"]
    assert operator["command"] == []
    assert operator["healthcheck"] == {"disable": True}
    assert operator["restart"] == "no"
    assert operator["pull_policy"] == "missing"
    assert not operator.get("ports")
