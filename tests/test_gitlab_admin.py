"""Real subprocess argument and secret-channel confidentiality for the operator CLI."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_INSTALLATION = "00000000-0000-0000-0000-000000000169"
_WORKSPACE = "00000000-0000-0000-0000-000000000170"
_CANARY = "CLI_SECRET_CANARY_169"


def cli(args: list[str], payload: bytes = b"", **extra: str) -> subprocess.CompletedProcess[bytes]:
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("GITLAB_", "DATABASE_", "SSL_CERT_", "TEST_DATABASE_"))
    }
    env.update(extra)
    return subprocess.run(
        [sys.executable, "-m", "app.gitlab_admin", *args],
        input=payload,
        capture_output=True,
        env=env,
        timeout=15,
        check=False,
    )


@pytest.mark.parametrize(
    "args",
    [
        ["--unknown=" + _CANARY],
        [_CANARY],
        ["inspect", "--installation-id", _CANARY],
        ["bind-project", "--installation-id", _INSTALLATION, "--project-id", _CANARY],
        ["rotate-token", "--installation-id", _INSTALLATION, "--expected-generation", _CANARY],
        [
            "create",
            "--workspace-id",
            _WORKSPACE,
            "--installation-id",
            _INSTALLATION,
            "--origin",
            "https://gitlab.com",
            "--secrets-fd",
            _CANARY,
        ],
        ["inspect", "--installation-id", _INSTALLATION, _CANARY],
    ],
)
def test_argument_failures_never_echo_rejected_values(args: list[str]) -> None:
    result = cli(args)
    assert result.returncode == 2
    assert result.stdout == b""
    assert result.stderr == b"arguments_invalid\n"
    assert _CANARY.encode() not in result.stdout + result.stderr


def test_help_documents_public_commands_and_protected_fd_ergonomics() -> None:
    result = cli(["--help"])
    assert result.returncode == 0
    assert result.stderr == b""
    for value in [
        b"bind-project",
        b"revoke-workspace",
        b"--secrets-fd",
        b"protected",
        b"SSL_CERT_FILE",
    ]:
        assert value in result.stdout


@pytest.mark.parametrize(
    "command",
    [
        [],
        ["create"],
        ["bind-project"],
        ["rotate-token"],
        ["activate"],
        ["revoke-installation"],
        ["revoke-workspace"],
        ["inspect"],
    ],
)
def test_help_uses_the_python_available_in_the_container(command: list[str]) -> None:
    result = cli([*command, "--help"], _CANARY.encode(), SSL_CERT_FILE="/missing/CA_CANARY")
    assert result.returncode == 0
    assert result.stderr == b""
    assert result.stdout.startswith(b"usage: python -m app.gitlab_admin")
    assert b"uv run" not in result.stdout
    assert _CANARY.encode() not in result.stdout
    assert b"CA_CANARY" not in result.stdout


def test_help_distinguishes_container_stdin_from_local_extra_descriptors() -> None:
    result = cli(["--help"])
    help_text = b" ".join(result.stdout.split())
    assert result.returncode == 0
    assert b"Container example" in help_text
    assert b"run on the host" in help_text
    assert (
        b"docker compose --profile gitlab-operator run --rm --no-deps -T gitlab-admin "
        b"create [public options] --secrets-fd 0 < /protected/secrets.json"
    ) in help_text
    assert b"Local process only" in help_text
    assert b"Compose does not forward extra descriptors" in help_text
    assert b"--secrets-fd 3 3< /protected/secrets.json" in help_text


def test_help_restores_reviewed_deploy_bootstrap_when_prompt_is_not_seeded() -> None:
    result = cli(["--help"])
    help_text = b" ".join(result.stdout.split())
    assert result.returncode == 0
    assert (
        b"For prompt_not_seeded: restore successful reviewed deploy bootstrap "
        b"(it runs the packaged seed)."
    ) in help_text
    assert b"python -m app.bootstrap.seed_prompts" not in help_text


def create_args() -> list[str]:
    return [
        "create",
        "--workspace-id",
        _WORKSPACE,
        "--installation-id",
        _INSTALLATION,
        "--origin",
        "https://gitlab.com",
    ]


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        b"\xff",
        b'{"token": "CLI_SECRET_CANARY_169"',
        b'{"token":"CLI_SECRET_CANARY_169","token":"duplicate","webhook_secret":"x"}',
        b'{"token":"CLI_SECRET_CANARY_169","extra":"private"}',
        b'{"token":"CLI_SECRET_CANARY_169"}',
        b'{"token":null,"webhook_secret":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}',
        b'{"token":"space token","webhook_secret":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}',
        b'{"token":"x","webhook_secret":"short"}',
        b'{"token":"' + b"a" * 8192 + b'"}',
        b'{"token":NaN}',
        b"[]",
        b'"CLI_SECRET_CANARY_169"',
    ],
)
def test_secret_input_is_bounded_strict_and_never_echoed(payload: bytes) -> None:
    result = cli(create_args(), payload)
    assert result.returncode == 1
    assert result.stdout == b""
    assert result.stderr == b"secrets_invalid\n"


def test_rotate_refuses_webhook_field_and_absent_pat_environment_fallback() -> None:
    args = ["rotate-token", "--installation-id", _INSTALLATION]
    result = cli(args, b"{}", GITLAB_TOKEN=_CANARY)
    assert result.stderr == b"secrets_invalid\n"
    result = cli(args, json.dumps({"token": _CANARY, "webhook_secret": "a" * 32}).encode())
    assert result.stderr == b"secrets_invalid\n"


def test_closed_fd_is_sanitized() -> None:
    result = cli(create_args() + ["--secrets-fd", "2147483647"])
    assert result.stdout == b""
    assert result.stderr == b"secrets_invalid\n"


@pytest.mark.parametrize(
    "args",
    [create_args(), ["bind-project", "--installation-id", _INSTALLATION, "--project-id", "42"]],
)
def test_missing_config_is_sanitized_without_global_pat_fallback(args: list[str]) -> None:
    result = cli(
        args,
        json.dumps({"token": _CANARY, "webhook_secret": "a" * 32}).encode(),
        GITLAB_TOKEN=_CANARY,
        GITLAB_BASE_URL="https://gitlab.com",
    )
    assert result.stdout == b""
    assert result.stderr == b"configuration_invalid\n"


def test_echoed_terminal_input_is_rejected_without_waiting_for_input() -> None:
    import pty

    master, slave = pty.openpty()
    try:
        result = subprocess.run(
            [sys.executable, "-m", "app.gitlab_admin", *create_args()],
            stdin=slave,
            capture_output=True,
            timeout=10,
            check=False,
        )
        assert result.returncode == 1
        assert result.stdout == b""
        assert result.stderr == b"secrets_invalid\n"
    finally:
        os.close(master)
        os.close(slave)


def configured_env() -> dict[str, str]:
    import base64

    return {
        "DATABASE_URL": "postgresql+psycopg://operator:DSN_CANARY@127.0.0.1:1/absent",
        "GITLAB_ACTIVE_KEY_ID": "current",
        "GITLAB_CREDENTIAL_KEYS": json.dumps({"current": base64.b64encode(b"k" * 32).decode()}),
        "GITLAB_ALLOWED_ORIGINS": '["https://gitlab.com"]',
    }


@pytest.mark.parametrize(
    "name,value",
    [
        ("GITLAB_CREDENTIAL_KEYS", '{"current":"CONFIG_CANARY","current":"private"}'),
        ("GITLAB_CREDENTIAL_KEYS", "CONFIG_CANARY" * 2000),
        ("GITLAB_ALLOWED_ORIGINS", '["https://CONFIG_CANARY"]'),
        ("DATABASE_URL", "DSN_CANARY"),
        ("SSL_CERT_FILE", "/nonexistent/CA_CANARY.pem"),
    ],
)
def test_process_configuration_failures_are_sanitized(name: str, value: str) -> None:
    env = configured_env()
    env[name] = value
    result = cli(
        create_args(), json.dumps({"token": _CANARY, "webhook_secret": "a" * 32}).encode(), **env
    )
    assert result.returncode == 1
    assert result.stdout == b""
    assert result.stderr == b"configuration_invalid\n"


def test_process_database_errors_exclude_dsn_traceback_and_input_secrets() -> None:
    result = cli(["inspect", "--installation-id", _INSTALLATION], b"", **configured_env())
    assert result.returncode == 1
    assert result.stdout == b""
    assert result.stderr == b"operation_failed\n"


@pytest.mark.parametrize(
    "asset_state",
    [
        "missing",
        "unreadable",
        "invalid",
        "schema_utf8",
        "default_utf8",
        "schema_deep",
        "default_deep",
        "schema_integer",
        "default_integer",
        "schema_syntax",
        "default_syntax",
    ],
)
def test_bind_reports_actionable_rule_asset_failure_without_raw_details(
    tmp_path: Path,
    asset_state: str,
) -> None:
    # Execute an unchanged copy of the real entrypoint as a damaged installation.
    # Its normal __file__ resource lookup resolves scratch assets, leaving real
    # checked-in files and the loader/use cases entirely unchanged.
    root = Path(__file__).resolve().parents[1]
    entrypoint = tmp_path / "app" / "gitlab_admin.py"
    entrypoint.parent.mkdir()
    shutil.copyfile(root / "app" / "gitlab_admin.py", entrypoint)
    rules = tmp_path / "review" / "rules"
    rules.mkdir(parents=True)
    if asset_state == "unreadable":
        (rules / "schema.json").mkdir()
    elif asset_state == "invalid":
        shutil.copyfile(root / "review" / "rules" / "schema.json", rules / "schema.json")
        (rules / "default-backend.v1.json").write_text(
            '{"version":1,"stack":"backend","rules":[{"private":"ASSET_CANARY_169"}]}'
        )
    elif "_" in asset_state:
        location, form = asset_state.split("_", 1)
        if location == "default":
            shutil.copyfile(root / "review" / "rules" / "schema.json", rules / "schema.json")
        target = rules / ("schema.json" if location == "schema" else "default-backend.v1.json")
        payloads = {
            "utf8": b"\xffASSET_CANARY_169",
            "deep": b'{"private":' + b"[" * 20000 + b"0" + b"]" * 20000 + b"}",
            "integer": b'{"private":' + b"9" * 5000 + b"}",
            "syntax": b'{"private":"ASSET_CANARY_169"',
        }
        target.write_bytes(payloads[form])
    env = dict(os.environ)
    env.update(configured_env(), PYTHONPATH=str(root))
    env.pop("PYTHONINTMAXSTRDIGITS", None)
    env.pop("SSL_CERT_FILE", None)
    env.pop("GITLAB_PRIVATE_ORIGINS", None)
    result = subprocess.run(
        [
            sys.executable,
            str(entrypoint),
            "bind-project",
            "--installation-id",
            _INSTALLATION,
            "--project-id",
            "42",
        ],
        input=b"",
        env=env,
        capture_output=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 1
    assert result.stdout == b""
    assert result.stderr == b"rule_assets_invalid\n"
    assert b"ASSET_CANARY_169" not in result.stdout + result.stderr


@pytest.mark.parametrize(
    "extra",
    [
        [],
        ["--expected-generation", "0"],
        ["--expected-generation", "1", "--secrets-fd", "0"],
        ["--expected-generation", "1", "--origin", _CANARY],
    ],
)
def test_activate_requires_generation_and_has_no_new_secret_or_origin_flags(
    extra: list[str],
) -> None:
    result = cli(["activate", "--installation-id", _INSTALLATION, *extra], _CANARY.encode())
    assert result.returncode == 2
    assert result.stdout == b""
    assert result.stderr == b"arguments_invalid\n"


def test_activate_help_explains_stored_authority_and_external_deployment_gate() -> None:
    result = cli(["--help"])
    assert result.returncode == 0
    assert b"activate" in result.stdout and b"stored credential" in result.stdout
    assert b"#171" in result.stdout
    assert b"No activation command" not in result.stdout
    detail = cli(["activate", "--help"])
    assert b"--expected-generation" in detail.stdout
    assert b"--secrets-fd" not in detail.stdout
    assert b"--origin" not in detail.stdout
