"""Pure shared GitLab configuration contract, with no secret-bearing failures."""

from __future__ import annotations

import base64
import json

import pytest

from app.modules.integrations.gitlab.infrastructure.settings import (
    DatabaseSettings,
    GitLabRuntimeSettings,
    GitLabSettingsError,
)


def test_runtime_settings_parse_copy_and_redact_secrets() -> None:
    encoded = base64.b64encode(b"k" * 32).decode()
    env = {
        "DATABASE_URL": "postgresql+psycopg://operator:DSN_CANARY@localhost/app",
        "GITLAB_ACTIVE_KEY_ID": "current",
        "GITLAB_CREDENTIAL_KEYS": json.dumps({"current": encoded}),
        "GITLAB_ALLOWED_ORIGINS": '["https://gitlab.com", "https://localhost:49443"]',
        "GITLAB_PRIVATE_ORIGINS": '["https://localhost:49443"]',
    }
    settings = GitLabRuntimeSettings.from_environment(env)
    env.clear()
    assert settings.keys["current"] == b"k" * 32
    assert settings.origin_policy.validate("https://localhost:49443") == "https://localhost:49443"
    assert settings.active_key_id == "current"
    assert encoded not in repr(settings)
    assert "DSN_CANARY" not in repr(settings)


def runtime_env() -> dict[str, str]:
    return {
        "DATABASE_URL": "postgresql+psycopg://operator:DSN_CANARY@localhost/app",
        "GITLAB_ACTIVE_KEY_ID": "current",
        "GITLAB_CREDENTIAL_KEYS": json.dumps({"current": base64.b64encode(b"k" * 32).decode()}),
        "GITLAB_ALLOWED_ORIGINS": '["https://gitlab.com"]',
    }


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("DATABASE_URL", "CONFIG_CANARY"),
        ("DATABASE_URL", "sqlite:///CONFIG_CANARY"),
        ("DATABASE_URL", "postgresql+psycopg://localhost/CONFIG_CANARY\n"),
        ("DATABASE_URL", "postgresql+psycopg://localhost"),
        ("GITLAB_ACTIVE_KEY_ID", "CONFIG_CANARY"),
        ("GITLAB_CREDENTIAL_KEYS", '{"current":"CONFIG_CANARY"}'),
        ("GITLAB_CREDENTIAL_KEYS", '{"current":"CONFIG_CANARY","current":"duplicate"}'),
        ("GITLAB_CREDENTIAL_KEYS", "{}"),
        ("GITLAB_CREDENTIAL_KEYS", "[]"),
        ("GITLAB_CREDENTIAL_KEYS", "{"),
        ("GITLAB_CREDENTIAL_KEYS", "x" * 16385),
        ("GITLAB_ALLOWED_ORIGINS", '["https://gitlab.com","https://gitlab.com"]'),
        ("GITLAB_ALLOWED_ORIGINS", '["http://CONFIG_CANARY"]'),
        ("GITLAB_ALLOWED_ORIGINS", '["https://user:CONFIG_CANARY@gitlab.com"]'),
        ("GITLAB_ALLOWED_ORIGINS", '["https://localhost"]'),
        ("GITLAB_ALLOWED_ORIGINS", '["https://gitlab.com/"]'),
        ("GITLAB_ALLOWED_ORIGINS", '[["CONFIG_CANARY"]]'),
        ("GITLAB_ALLOWED_ORIGINS", "[]"),
        ("GITLAB_PRIVATE_ORIGINS", '["https://localhost"]'),
        ("GITLAB_PRIVATE_ORIGINS", '["https://gitlab.com","https://gitlab.com"]'),
        ("SSL_CERT_FILE", "CONFIG_CANARY\n"),
        ("SSL_CERT_FILE", ""),
    ],
    ids=lambda value: str(len(value)) if len(value) > 100 else value,
)
def test_invalid_configuration_is_static_and_redacted(name: str, value: str) -> None:
    env = runtime_env()
    env[name] = value
    with pytest.raises(GitLabSettingsError, match="^configuration_invalid$") as caught:
        GitLabRuntimeSettings.from_environment(env)
    assert "CONFIG_CANARY" not in str(caught.value)


@pytest.mark.parametrize("name", list(runtime_env()))
def test_required_settings_never_fall_back(name: str) -> None:
    env = runtime_env()
    del env[name]
    env.update(GITLAB_TOKEN="PAT_CANARY", GITLAB_BASE_URL="https://gitlab.com")
    with pytest.raises(GitLabSettingsError, match="^configuration_invalid$"):
        GitLabRuntimeSettings.from_environment(env)


def test_key_material_is_immutable_and_base64_must_be_canonical() -> None:
    settings = GitLabRuntimeSettings.from_environment(runtime_env())
    with pytest.raises(TypeError):
        settings.keys["current"] = b"z" * 32  # type: ignore[index]
    for key in (b"a" * 31, b"a" * 33):
        env = runtime_env()
        env["GITLAB_CREDENTIAL_KEYS"] = json.dumps({"current": base64.b64encode(key).decode()})
        with pytest.raises(GitLabSettingsError):
            GitLabRuntimeSettings.from_environment(env)


def test_database_only_recovery_ignores_broken_crypto_origin_and_ca_configuration() -> None:
    env = runtime_env()
    env.update(GITLAB_CREDENTIAL_KEYS="broken", GITLAB_ALLOWED_ORIGINS="broken", SSL_CERT_FILE="\n")
    settings = DatabaseSettings.from_environment(env)
    assert settings.database_url == "postgresql+psycopg://operator:DSN_CANARY@localhost/app"
    assert "DSN_CANARY" not in repr(settings)
