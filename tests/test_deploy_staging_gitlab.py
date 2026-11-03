"""GitLab configuration recipients and the CI preflight of the staging deploy (#335)."""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
from collections.abc import Mapping
from pathlib import Path

import pytest

from tests.deploy_host_fakes import (
    CI_SECRETS,
    IMAGE_A,
    REPO_ROOT,
    STAGING_COMPOSE,
    WORKFLOW,
    _bundle,
    _keyring,
    _mode,
    _read_env_file,
    _run_bundle_step,
    _run_ci_gitlab_pipeline,
    _workflow_step,
)

# --- GitLab configuration recipients (#335) ---------------------------------------------------


def _write_application_bundle(directory: Path, values: Mapping[str, str]) -> None:
    result = subprocess.run(
        [
            "bash",
            "-eo",
            "pipefail",
            "-c",
            'source "$ENV_HELPER"; write_app_env_files "$APP_DIR" "$APP_SECRETS_B64"',
        ],
        env={
            "PATH": os.environ["PATH"],
            "ENV_HELPER": str(REPO_ROOT / "deploy" / "scripts" / "env-file.sh"),
            "APP_DIR": str(directory),
            "APP_SECRETS_B64": _bundle(values),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""


@pytest.mark.parametrize("private_origins", [None, "", "[]"])
def test_gitlab_bundle_uses_dedicated_owner_only_file_and_api_admission(
    tmp_path: Path, private_origins: str | None
) -> None:
    settings = {
        "GITLAB_CREDENTIAL_KEYS": _keyring(),
        "GITLAB_ACTIVE_KEY_ID": "current",
        "GITLAB_ALLOWED_ORIGINS": '["https://gitlab.com"]',
    }
    if private_origins is not None:
        settings["GITLAB_PRIVATE_ORIGINS"] = private_origins
    _write_application_bundle(
        tmp_path,
        {**CI_SECRETS, **settings, "GITLAB_WEBHOOK_INTAKE_ENABLED": "true"},
    )
    assert _read_env_file(tmp_path / "gitlab.env") == {
        name: value for name, value in settings.items() if value
    }
    assert _read_env_file(tmp_path / "api.env")["GITLAB_WEBHOOK_INTAKE_ENABLED"] == "true"
    for filename in ("app.env", "worker.env", "webhook-worker.env"):
        assert not any(name.startswith("GITLAB_") for name in _read_env_file(tmp_path / filename))
    assert not set(settings).intersection(_read_env_file(tmp_path / "api.env"))
    for filename in ("gitlab.env", "app.env", "api.env", "worker.env", "webhook-worker.env"):
        assert _mode(tmp_path / filename) == 0o600


@pytest.mark.parametrize("configured", [False, True], ids=["github-only", "gitlab-configured"])
def test_compose_routes_gitlab_to_workers_and_profile_only_operator(
    tmp_path: Path, configured: bool
) -> None:
    if shutil.which("docker") is None:
        pytest.skip("Docker Compose CLI is required for recipient rendering")
    gitlab = (
        {
            "GITLAB_CREDENTIAL_KEYS": _keyring(),
            "GITLAB_ACTIVE_KEY_ID": "current",
            "GITLAB_ALLOWED_ORIGINS": '["https://gitlab.com"]',
        }
        if configured
        else {}
    )
    admission = {"GITLAB_WEBHOOK_INTAKE_ENABLED": "true"} if configured else {}
    _write_application_bundle(tmp_path, {**CI_SECRETS, **gitlab, **admission})
    shutil.copy(STAGING_COMPOSE, tmp_path / "compose.yml")
    compose_env = tmp_path / ".env"
    compose_env.write_text(
        f"IMAGE={IMAGE_A}\nPOSTGRES_PASSWORD=test-db\nRABBITMQ_PASSWORD=test-broker\n"
        "REDIS_PASSWORD=test-cache\n"
    )
    compose_env.chmod(0o600)
    command = ["docker", "compose", "--project-directory", str(tmp_path)]
    environment = {"PATH": os.environ["PATH"], "HOME": os.environ["HOME"]}
    rendered = subprocess.run(
        [*command, "--profile", "gitlab-operator", "config", "--format", "json"],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert rendered.returncode == 0, "Compose recipient rendering failed"
    services = json.loads(rendered.stdout)["services"]
    assert "gitlab-admin" in services
    worker_keys = {
        "GITLAB_CREDENTIAL_KEYS",
        "GITLAB_ACTIVE_KEY_ID",
        "GITLAB_ALLOWED_ORIGINS",
        "GITLAB_PRIVATE_ORIGINS",
    }
    for name, service in services.items():
        actual = service.get("environment", {})
        assert {key: value for key, value in actual.items() if key in worker_keys} == (
            gitlab if name in {"worker", "webhook-worker", "gitlab-admin"} else {}
        ), name
        assert {
            key: value for key, value in actual.items() if key == "GITLAB_WEBHOOK_INTAKE_ENABLED"
        } == (admission if name == "api" else {}), name
        assert "GITLAB_TOKEN" not in actual
    operator = services["gitlab-admin"]
    assert operator["image"] == IMAGE_A
    assert operator["profiles"] == ["gitlab-operator"]
    assert operator["entrypoint"] == ["python", "-m", "app.gitlab_admin"]
    assert operator["command"] == []
    assert operator["healthcheck"] == {"disable": True}
    assert operator["restart"] == "no"
    assert operator["pull_policy"] == "missing"
    for name in ("api", "worker", "webhook-worker", "bootstrap"):
        assert services[name]["pull_policy"] == "always"
    assert not operator.get("ports")
    assert operator["environment"] == {
        "DATABASE_URL": "postgresql+psycopg://app:test-db@postgres:5432/app",
        **gitlab,
    }
    assert set(operator["depends_on"]) == {"postgres"}
    normal_services = subprocess.run(
        [*command, "config", "--services"],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert normal_services.returncode == 0
    assert set(normal_services.stdout.splitlines()) == {
        "api",
        "worker",
        "webhook-worker",
        "bootstrap",
        "postgres",
        "rabbitmq",
        "redis",
    }


@pytest.mark.parametrize("private_origins", [None, "", "CONFIG_CANARY"])
def test_ci_bundle_preserves_gitlab_sources_and_optional_absence(
    tmp_path: Path, private_origins: str | None
) -> None:
    settings = {
        "GITLAB_CREDENTIAL_KEYS": _keyring(),
        "GITLAB_ACTIVE_KEY_ID": "current",
        "GITLAB_ALLOWED_ORIGINS": '["https://gitlab.com"]',
        "GITLAB_WEBHOOK_INTAKE_ENABLED": "false",
    }
    if private_origins is not None:
        settings["GITLAB_PRIVATE_ORIGINS"] = private_origins
    stdout, bundle = _run_bundle_step(tmp_path, {**CI_SECRETS, **settings})
    decoded = {
        name: base64.b64decode(value).decode()
        for name, _, value in (
            line.partition("=") for line in base64.b64decode(bundle).decode().splitlines()
        )
    }
    assert decoded == {name: value for name, value in {**CI_SECRETS, **settings}.items() if value}
    assert "CONFIG_CANARY" not in stdout


def test_ci_rejects_enabled_gitlab_before_any_host_mutation(tmp_path: Path) -> None:
    result, mutations, calls, temporary = _run_ci_gitlab_pipeline(
        tmp_path, {**CI_SECRETS, "GITLAB_WEBHOOK_INTAKE_ENABLED": "true"}
    )
    assert result.returncode != 0
    assert mutations == []
    assert result.stderr == "configuration_invalid\n"
    assert calls
    assert list(temporary.iterdir()) == []


@pytest.mark.parametrize(
    ("settings", "valid"),
    [
        ({}, True),
        (
            {
                "GITLAB_CREDENTIAL_KEYS": _keyring(key_id="KEY_CANARY", material=b"q" * 32),
                "GITLAB_ACTIVE_KEY_ID": "KEY_CANARY",
                "GITLAB_ALLOWED_ORIGINS": '["https://gitlab.com"]',
                "GITLAB_WEBHOOK_INTAKE_ENABLED": "false",
            },
            True,
        ),
        (
            {
                "GITLAB_CREDENTIAL_KEYS": _keyring(
                    key_id="KEY_CANARY", material=b"q" * 32, indent=2
                ),
                "GITLAB_ACTIVE_KEY_ID": "KEY_CANARY",
                "GITLAB_ALLOWED_ORIGINS": '["https://gitlab.com"]',
                "GITLAB_WEBHOOK_INTAKE_ENABLED": "true",
            },
            True,
        ),
        ({"GITLAB_ACTIVE_KEY_ID": "KEY_CANARY"}, False),
        (
            {
                "GITLAB_CREDENTIAL_KEYS": '{"KEY_CANARY":"MATERIAL_CANARY"}',
                "GITLAB_ACTIVE_KEY_ID": "KEY_CANARY",
                "GITLAB_ALLOWED_ORIGINS": '["https://gitlab.com"]',
            },
            False,
        ),
        (
            {
                "GITLAB_CREDENTIAL_KEYS": _keyring(key_id="KEY_CANARY", material=b"q" * 32),
                "GITLAB_ACTIVE_KEY_ID": "KEY_CANARY",
                "GITLAB_ALLOWED_ORIGINS": '["https://gitlab.com"]',
                "GITLAB_PRIVATE_ORIGINS": "PRIVATE_CANARY",
            },
            False,
        ),
        ({"GITLAB_WEBHOOK_INTAKE_ENABLED": "FLAG_CANARY"}, False),
    ],
    ids=["empty", "complete-off", "multiline-on", "partial", "bad-key", "bad-private", "bad-flag"],
)
def test_ci_preflight_validates_exact_target_with_protected_gitlab_only_input(
    tmp_path: Path, settings: Mapping[str, str], valid: bool
) -> None:
    result, mutations, calls, temporary = _run_ci_gitlab_pipeline(
        tmp_path, {**CI_SECRETS, **settings}
    )
    assert (result.returncode == 0) is valid
    assert bool(mutations) is valid
    assert result.stdout == ""
    assert result.stderr == ("" if valid else "configuration_invalid\n")
    assert len(calls) == 3
    assert calls[0]["token_stdin_correct"] is True
    assert calls[1]["argv"] == ["pull", IMAGE_A]
    run = calls[2]
    args = run["argv"]
    assert isinstance(args, list)
    assert args[:4] == ["run", "--rm", "--network", "none"]
    assert args[4] == "--env-file"
    assert args[6:9] == [IMAGE_A, "python", "-c"]
    assert run["env_mode"] == 0o600
    assert run["env_names"] == ["GITLAB_DEPLOYMENT_BUNDLE", "GITLAB_DEPLOYMENT_KEYS"]
    assert run["gitlab_names"] == sorted(name for name, value in settings.items() if value)
    file_modes = run["file_modes"]
    assert isinstance(file_modes, list)
    assert file_modes and set(file_modes) == {0o600}
    for call in calls:
        arguments = call["argv"]
        assert isinstance(arguments, list)
        diagnostic = "\n".join(arguments) + result.stdout + result.stderr
        for canary in (
            "KEY_CANARY",
            "MATERIAL_CANARY",
            "PRIVATE_CANARY",
            "FLAG_CANARY",
            "REGISTRY_TOKEN_CANARY",
        ):
            assert canary not in diagnostic
        assert str(call["docker_config"]).startswith(str(temporary))
    assert list(temporary.iterdir()) == []


@pytest.mark.parametrize("fail_stage", ["login", "pull", "run"])
def test_ci_preflight_failures_leave_no_registry_or_secret_files(
    tmp_path: Path, fail_stage: str
) -> None:
    result, mutations, calls, temporary = _run_ci_gitlab_pipeline(
        tmp_path, CI_SECRETS, fail_stage=fail_stage
    )
    assert result.returncode != 0
    assert mutations == []
    assert calls
    assert "REGISTRY_TOKEN_CANARY" not in result.stdout + result.stderr
    assert list(temporary.iterdir()) == []


def test_ci_preflight_is_unconditional_and_precedes_all_remote_actions() -> None:
    step = _workflow_step("Validate application bundle and GitLab configuration")
    assert "        if:" not in step
    assert "IMAGE: ${{ needs.push-image.outputs.image_ref }}" in step
    assert "APP_SECRETS_B64: ${{ steps.app_secrets.outputs.bundle }}" in step
    assert "GHCR_TOKEN: ${{ secrets.GITHUB_TOKEN }}" in step
    assert "GHCR_USER: ${{ github.actor }}" in step
    deployment = (
        WORKFLOW.read_text()
        .split("  deploy-staging:\n", 1)[1]
        .split("\n  deploy-monitoring:", 1)[0]
    )
    preflight = deployment.index(
        "      - name: Validate application bundle and GitLab configuration"
    )
    assert deployment.index("      - name: Bundle application secrets") < preflight
    assert preflight < deployment.index("      - name: Upload deploy files")
    assert preflight < deployment.index("      - name: Upload edge proxy files")
    assert preflight < deployment.index("      - name: Prepare host")


@pytest.mark.parametrize(
    "payload",
    [
        "PAYLOAD_CANARY!",
        base64.b64encode(b"UNKNOWN=UEFZTE9BRF9DQU5BUlk=\n").decode(),
        base64.b64encode(b"GITLAB_ACTIVE_KEY_ID\n").decode(),
        base64.b64encode(b"GITLAB_ACTIVE_KEY_ID=/w==\n").decode(),
        base64.b64encode(b"GITLAB_ACTIVE_KEY_ID=YQ==\nGITLAB_ACTIVE_KEY_ID=Yg==\n").decode(),
    ],
    ids=["not-base64", "unknown-name", "missing-value", "non-utf8", "duplicate-name"],
)
def test_target_preflight_wrapper_rejects_malformed_transport_without_traceback(
    tmp_path: Path, payload: str
) -> None:
    result, mutations, _, temporary = _run_ci_gitlab_pipeline(
        tmp_path, {**CI_SECRETS, "STUB_PAYLOAD_OVERRIDE": payload}
    )
    assert result.returncode != 0
    assert mutations == []
    assert result.stdout == ""
    assert result.stderr == "configuration_invalid\n"
    assert list(temporary.iterdir()) == []


def test_ci_preflight_rejects_mutable_target_before_docker_or_host_calls(tmp_path: Path) -> None:
    result, mutations, calls, temporary = _run_ci_gitlab_pipeline(
        tmp_path, {**CI_SECRETS, "IMAGE": "ghcr.io/test/api:mutable"}
    )
    assert result.returncode != 0
    assert result.stderr == "deployment preflight requires an immutable image digest\n"
    assert mutations == []
    assert calls == []
    assert list(temporary.iterdir()) == []
