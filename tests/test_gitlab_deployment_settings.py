"""Deployment preflight validates configuration without initializing the runtime."""

from __future__ import annotations

import base64
import json

import pytest

from app.bootstrap.gitlab_deployment import validate_environment
from app.modules.integrations.gitlab.infrastructure.settings import GitLabSettingsError


def _keyring(*, material: bytes = b"k" * 32) -> str:
    return json.dumps({"current": base64.b64encode(material).decode()})


def runtime_env() -> dict[str, str]:
    return {
        "GITLAB_ACTIVE_KEY_ID": "current",
        "GITLAB_CREDENTIAL_KEYS": _keyring(),
        "GITLAB_ALLOWED_ORIGINS": '["https://gitlab.com"]',
    }


def test_unconfigured_deployment_is_default_off() -> None:
    validate_environment({})


def test_enabled_intake_requires_worker_configuration() -> None:
    with pytest.raises(GitLabSettingsError, match="^configuration_invalid$"):
        validate_environment({"GITLAB_WEBHOOK_INTAKE_ENABLED": "true"})


@pytest.mark.parametrize("flag", ["", "false", "true", " TRUE "])
def test_complete_configuration_is_valid_with_admission_off_or_on(flag: str) -> None:
    env = {**runtime_env(), "GITLAB_WEBHOOK_INTAKE_ENABLED": flag}
    original = dict(env)
    validate_environment(env)
    assert env == original


@pytest.mark.parametrize("flag", ["", "false"])
def test_empty_optional_configuration_stays_disabled(flag: str) -> None:
    validate_environment(
        {
            "GITLAB_CREDENTIAL_KEYS": "",
            "GITLAB_ACTIVE_KEY_ID": "",
            "GITLAB_ALLOWED_ORIGINS": "",
            "GITLAB_PRIVATE_ORIGINS": "",
            "GITLAB_WEBHOOK_INTAKE_ENABLED": flag,
        }
    )


@pytest.mark.parametrize("name", list(runtime_env()))
@pytest.mark.parametrize("flag", ["false", "true"])
def test_partial_worker_configuration_is_rejected_even_when_admission_is_off(
    name: str, flag: str
) -> None:
    env = {**runtime_env(), "GITLAB_WEBHOOK_INTAKE_ENABLED": flag}
    del env[name]
    with pytest.raises(GitLabSettingsError, match="^configuration_invalid$"):
        validate_environment(env)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("GITLAB_CREDENTIAL_KEYS", "CONFIG_CANARY"),
        ("GITLAB_CREDENTIAL_KEYS", "{}"),
        ("GITLAB_CREDENTIAL_KEYS", "[]"),
        ("GITLAB_CREDENTIAL_KEYS", '{"current":"CONFIG_CANARY"}'),
        ("GITLAB_CREDENTIAL_KEYS", '{"current":"a","current":"CONFIG_CANARY"}'),
        ("GITLAB_CREDENTIAL_KEYS", _keyring(material=b"k" * 31)),
        ("GITLAB_ACTIVE_KEY_ID", "CONFIG_CANARY"),
        ("GITLAB_ALLOWED_ORIGINS", "CONFIG_CANARY"),
        ("GITLAB_ALLOWED_ORIGINS", "[]"),
        ("GITLAB_ALLOWED_ORIGINS", '["http://CONFIG_CANARY"]'),
        ("GITLAB_ALLOWED_ORIGINS", '["https://user:CONFIG_CANARY@gitlab.com"]'),
        ("GITLAB_ALLOWED_ORIGINS", '["https://gitlab.com", "https://gitlab.com"]'),
        ("GITLAB_ALLOWED_ORIGINS", '["https://localhost"]'),
        ("GITLAB_PRIVATE_ORIGINS", ""),
        ("GITLAB_PRIVATE_ORIGINS", "CONFIG_CANARY"),
        ("GITLAB_PRIVATE_ORIGINS", '["https://unlisted.example"]'),
        ("GITLAB_WEBHOOK_INTAKE_ENABLED", "CONFIG_CANARY"),
        ("GITLAB_WEBHOOK_INTAKE_ENABLED", "1"),
    ],
    ids=[
        "malformed-keys",
        "empty-keys",
        "keys-array",
        "bad-key-material",
        "duplicate-key-id",
        "short-key",
        "missing-active-key",
        "malformed-origins",
        "empty-origins",
        "http-origin",
        "userinfo-origin",
        "duplicate-origin",
        "private-origin-not-opted-in",
        "empty-private-origins",
        "malformed-private-origins",
        "private-origin-not-allowed",
        "invalid-flag",
        "numeric-flag",
    ],
)
def test_invalid_configuration_has_only_static_redacted_diagnostics(name: str, value: str) -> None:
    env = {**runtime_env(), "GITLAB_WEBHOOK_INTAKE_ENABLED": "false", name: value}
    with pytest.raises(GitLabSettingsError, match="^configuration_invalid$"):
        validate_environment(env)


def test_private_network_origin_accepts_explicit_opt_in() -> None:
    validate_environment(
        {
            **runtime_env(),
            "GITLAB_ALLOWED_ORIGINS": '["https://localhost:49443"]',
            "GITLAB_PRIVATE_ORIGINS": '["https://localhost:49443"]',
            "GITLAB_WEBHOOK_INTAKE_ENABLED": "true",
        }
    )


def test_private_origins_alone_do_not_enable_a_partial_runtime() -> None:
    with pytest.raises(GitLabSettingsError, match="^configuration_invalid$"):
        validate_environment({"GITLAB_PRIVATE_ORIGINS": '["https://localhost:49443"]'})


def test_deployment_validation_needs_no_database_or_ca_file() -> None:
    env = {
        **runtime_env(),
        "DATABASE_URL": "invalid-and-must-not-be-used",
        "SSL_CERT_FILE": "/nonexistent/dmc335-preflight.pem",
        "GITLAB_WEBHOOK_INTAKE_ENABLED": "true",
    }
    validate_environment(env)
    assert env["DATABASE_URL"] == "invalid-and-must-not-be-used"


def test_global_token_and_base_url_never_replace_worker_configuration() -> None:
    with pytest.raises(GitLabSettingsError, match="^configuration_invalid$"):
        validate_environment(
            {
                "GITLAB_WEBHOOK_INTAKE_ENABLED": "true",
                "GITLAB_TOKEN": "PAT_CANARY",
                "GITLAB_BASE_URL": "https://gitlab.com",
            }
        )
