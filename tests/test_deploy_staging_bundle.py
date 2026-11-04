"""CI bundle of the staging secrets and how it reaches the env files of the host (#35)."""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path

import pytest

from tests.deploy_host_fakes import (
    CI_SECRETS,
    ENV_FILES,
    IMAGE_A,
    IMAGE_B,
    IMAGE_C,
    SECRET_SOURCES,
    VARIABLE_SOURCES,
    Host,
    _bundle,
    _mode,
    _read,
    _read_env_file,
    _run_bundle_step,
    _workflow_step,
)
from tests.deploy_host_fakes import host as _host_fixture

# Not a pytest_plugins entry: the monitoring tests have their own `host` and plugins are global.
host = _host_fixture

BUNDLED = {**SECRET_SOURCES, **VARIABLE_SOURCES}


def _bash_array(script: str, name: str) -> list[str]:
    match = re.search(rf"^\s*{name}=\(([^)]*)\)", script, re.MULTILINE)
    assert match is not None, f"array {name} is missing"
    return match.group(1).split()


# --- CI: from GitHub secrets to the host -------------------------------------------------------


def test_bundle_step_maps_environment_secrets_to_container_names() -> None:
    step = _workflow_step("Bundle application secrets")
    secrets = dict(re.findall(r"^\s+([A-Z_]+): \$\{\{ secrets\.([A-Z_0-9]+) \}\}$", step, re.M))
    variables = dict(re.findall(r"^\s+([A-Z_]+): \$\{\{ vars\.([A-Z_0-9]+) \}\}$", step, re.M))

    assert "id: app_secrets" in step
    assert "shell: bash" in step  # bash -eo pipefail, as _run_bundle_step runs it
    assert secrets == SECRET_SOURCES
    assert variables == VARIABLE_SOURCES
    assert set(_bash_array(step, "names")) == set(BUNDLED)


def test_bundle_step_masks_the_bundle_and_logs_only_names(tmp_path: Path) -> None:
    stdout, bundle = _run_bundle_step(tmp_path, CI_SECRETS)
    names = _bash_array(_workflow_step("Bundle application secrets"), "names")

    # The bundle is reversible base64 of every secret and is shown in the env of "Deploy image":
    # the runner must mask exactly this value before anything else is logged.
    assert bundle
    assert stdout.splitlines() == [
        f"::add-mask::{bundle}",
        *(f"set: {name}" if CI_SECRETS[name] else f"not set: {name}" for name in names),
    ]


def test_bundle_step_without_secrets_outputs_an_empty_bundle(tmp_path: Path) -> None:
    stdout, bundle = _run_bundle_step(tmp_path, dict.fromkeys(BUNDLED, ""))
    names = _bash_array(_workflow_step("Bundle application secrets"), "names")

    assert bundle == ""
    assert stdout.splitlines() == [f"not set: {name}" for name in names]


def test_portal_url_from_ci_reaches_only_the_review_worker(tmp_path: Path, host: Host) -> None:
    stdout, bundle = _run_bundle_step(tmp_path, {"PORTAL_URL": "https://staging-ui.example.test"})

    result = host.deploy(IMAGE_A, bundle=bundle)

    assert result.returncode == 0, result.stderr
    assert _read_env_file(host.app_dir / "worker.env") == {
        "PORTAL_URL": "https://staging-ui.example.test"
    }
    assert "set: PORTAL_URL" in stdout.splitlines()
    for name in ("app.env", "api.env", "webhook-worker.env"):
        assert "PORTAL_URL" not in _read_env_file(host.app_dir / name)


@pytest.mark.parametrize("portal_url", [None, ""], ids=["unset", "empty"])
def test_optional_portal_url_is_omitted_from_env_files(
    tmp_path: Path, host: Host, portal_url: str | None
) -> None:
    values = {**CI_SECRETS}
    if portal_url is None:
        values.pop("PORTAL_URL")
    else:
        values["PORTAL_URL"] = portal_url
    stdout, bundle = _run_bundle_step(tmp_path, values)

    result = host.deploy(IMAGE_A, bundle=bundle)

    assert result.returncode == 0, result.stderr
    assert "not set: PORTAL_URL" in stdout.splitlines()
    for name in ENV_FILES:
        assert "PORTAL_URL" not in _read_env_file(host.app_dir / name)


@pytest.mark.parametrize("fallback", [None, ""], ids=["unset", "empty"])
def test_optional_fallback_model_is_omitted_from_worker_env(
    tmp_path: Path, host: Host, fallback: str | None
) -> None:
    values = {**CI_SECRETS}
    if fallback is None:
        values.pop("LLM_FALLBACK_MODEL")
    else:
        values["LLM_FALLBACK_MODEL"] = fallback
    stdout, bundle = _run_bundle_step(tmp_path, values)

    result = host.deploy(IMAGE_A, bundle=bundle)

    assert result.returncode == 0, result.stderr
    assert "not set: LLM_FALLBACK_MODEL" in stdout.splitlines()
    assert _read_env_file(host.app_dir / "worker.env") == {
        "LLM_API_KEYS": "sk-test-1,sk-test-2",
        "LLM_BASE_URL": "https://llm.test/api/v1",
        "LLM_MODEL": "gpt-4.1-mini",
        "LLM_VERIFIER_MODEL": "verifier-model",
        "LLM_VERIFIER_FALLBACK_MODEL": "verifier-fallback-model",
        "PORTAL_URL": "https://staging-ui.example.test",
    }


@pytest.mark.parametrize("verifier", [None, ""], ids=["unset", "empty"])
def test_optional_verifier_models_are_omitted_from_worker_env(
    tmp_path: Path, host: Host, verifier: str | None
) -> None:
    # Without the pair the worker keeps today's route: the verifier falls back to the review
    # models (LlmSettings.from_env, #290).
    values = {**CI_SECRETS}
    for name in ("LLM_VERIFIER_MODEL", "LLM_VERIFIER_FALLBACK_MODEL"):
        if verifier is None:
            values.pop(name)
        else:
            values[name] = verifier
    stdout, bundle = _run_bundle_step(tmp_path, values)

    result = host.deploy(IMAGE_A, bundle=bundle)

    assert result.returncode == 0, result.stderr
    assert "not set: LLM_VERIFIER_MODEL" in stdout.splitlines()
    assert "not set: LLM_VERIFIER_FALLBACK_MODEL" in stdout.splitlines()
    assert _read_env_file(host.app_dir / "worker.env") == {
        "LLM_API_KEYS": "sk-test-1,sk-test-2",
        "LLM_BASE_URL": "https://llm.test/api/v1",
        "LLM_MODEL": "gpt-4.1-mini",
        "LLM_FALLBACK_MODEL": "mistral-small-3.2-24b",
        "PORTAL_URL": "https://staging-ui.example.test",
    }


@pytest.mark.parametrize(
    ("kept", "value", "dropped"),
    [
        ("LLM_VERIFIER_MODEL", "verifier-model", "LLM_VERIFIER_FALLBACK_MODEL"),
        ("LLM_VERIFIER_FALLBACK_MODEL", "verifier-fallback-model", "LLM_VERIFIER_MODEL"),
    ],
    ids=["model-only", "fallback-only"],
)
def test_one_verifier_model_reaches_worker_env_without_the_other(
    tmp_path: Path, host: Host, kept: str, value: str, dropped: str
) -> None:
    values = {**CI_SECRETS}
    values.pop(dropped)
    stdout, bundle = _run_bundle_step(tmp_path, values)

    result = host.deploy(IMAGE_A, bundle=bundle)

    assert result.returncode == 0, result.stderr
    assert f"set: {kept}" in stdout.splitlines()
    assert f"not set: {dropped}" in stdout.splitlines()
    assert _read_env_file(host.app_dir / "worker.env") == {
        "LLM_API_KEYS": "sk-test-1,sk-test-2",
        "LLM_BASE_URL": "https://llm.test/api/v1",
        "LLM_MODEL": "gpt-4.1-mini",
        "LLM_FALLBACK_MODEL": "mistral-small-3.2-24b",
        kept: value,
        "PORTAL_URL": "https://staging-ui.example.test",
    }


def test_legacy_eur_rate_is_ignored_by_bundle_and_worker_env(tmp_path: Path, host: Host) -> None:
    stdout, bundle = _run_bundle_step(tmp_path, {**CI_SECRETS, "LLM_EUR_TO_USD_RATE": "1.1204"})

    result = host.deploy(IMAGE_A, bundle=bundle)

    assert result.returncode == 0, result.stderr
    assert all("LLM_EUR_TO_USD_RATE" not in line for line in stdout.splitlines())
    assert "LLM_EUR_TO_USD_RATE" not in _read_env_file(host.app_dir / "worker.env")


def test_host_allowlist_follows_the_container_table() -> None:
    env_file = _read("deploy", "scripts", "env-file.sh")

    for array, keys in ENV_FILES.values():
        assert set(_bash_array(env_file, array)) == keys, array
    # Every CI bundle name has exactly one recipient file.
    assert sorted(k for _, keys in ENV_FILES.values() for k in keys) == sorted(BUNDLED)


def test_every_gitlab_key_routes_only_to_api_or_gitlab_env() -> None:
    for file_name, (_, keys) in ENV_FILES.items():
        for key in keys:
            if key.startswith("GITLAB_"):
                assert file_name in {"api.env", "gitlab.env"}, (file_name, key)


@pytest.mark.parametrize(
    "secrets",
    [CI_SECRETS, {**CI_SECRETS, "GITHUB_WEBHOOK_SECRET": "whsec$1\\x"}],
    ids=["webhook-secret-unset", "all-set"],
)
def test_secrets_from_ci_land_in_the_right_files_unchanged(
    tmp_path: Path, host: Host, secrets: Mapping[str, str]
) -> None:
    stdout, bundle = _run_bundle_step(tmp_path, secrets)

    result = host.deploy(IMAGE_A, bundle=bundle)

    assert result.returncode == 0, result.stderr
    assert "BEGIN TEST" not in stdout
    assert secrets["GITHUB_CLIENT_SECRET"] not in stdout
    expected = {name: value for name, value in secrets.items() if value}
    # Unset names are absent from every file, not an empty string.
    for file_name, (_, keys) in ENV_FILES.items():
        assert _read_env_file(host.app_dir / file_name) == {
            name: expected[name] for name in keys if name in expected
        }, file_name
    for name in (".env", *ENV_FILES):
        assert _mode(host.app_dir / name) == 0o600


def test_empty_value_in_the_bundle_is_left_out(host: Host) -> None:
    bundle = _bundle({"GITHUB_APP_ID": "7", "GITHUB_CLIENT_ID": ""})

    assert host.deploy(IMAGE_A, bundle=bundle).returncode == 0

    assert _read_env_file(host.app_dir / "app.env") == {"GITHUB_APP_ID": "7"}
    for name in ("api.env", "worker.env", "webhook-worker.env", "bitbucket.env"):
        assert _read_env_file(host.app_dir / name) == {}


@pytest.mark.parametrize(
    ("bundle", "message"),
    [
        (_bundle({"PATH": "/tmp"}), "PATH is not in the allowlist"),
        (_bundle({"GITLAB_TOKEN": "abc-PAT-CANARY"}), "GITLAB_TOKEN is not in the allowlist"),
        (_bundle({"GITHUB_CLIENT_SECRET": "abc'def"}), "single quotes are not supported"),
        (_bundle({"GITHUB_WEBHOOK_SECRET": "abc\\"}), "a trailing backslash is not supported"),
        # GNU base64 rejects the input; the macOS one decodes garbage that fails the line check.
        ("not base64!", "not valid base64|malformed line"),
    ],
    ids=["unknown-name", "gitlab-global-token", "single-quote", "trailing-backslash", "not-base64"],
)
def test_invalid_bundle_fails_before_the_host_changes(
    host: Host, bundle: str, message: str
) -> None:
    assert host.deploy(IMAGE_A, bundle=_bundle({"GITHUB_APP_ID": "1"})).returncode == 0
    before = host.files()

    result = host.deploy(IMAGE_B, bundle=bundle)

    assert result.returncode != 0
    assert re.search(message, result.stderr)
    assert "abc" not in result.stderr
    assert host.files() == before  # .env, state and secrets untouched, no temporary files left


def test_empty_bundle_clears_app_secrets_and_a_missing_one_keeps_them(host: Host) -> None:
    secrets = _bundle({"GITHUB_APP_ID": "1", "GITHUB_CLIENT_ID": "client"})
    assert host.deploy(IMAGE_A, bundle=secrets).returncode == 0

    assert host.deploy(IMAGE_B).returncode == 0
    kept = {name: _read_env_file(host.app_dir / name) for name in ("app.env", "api.env")}
    assert host.deploy(IMAGE_C, bundle="").returncode == 0
    cleared = {name: _read_env_file(host.app_dir / name) for name in ("app.env", "api.env")}

    assert kept == {"app.env": {"GITHUB_APP_ID": "1"}, "api.env": {"GITHUB_CLIENT_ID": "client"}}
    assert cleared == {"app.env": {}, "api.env": {}}
