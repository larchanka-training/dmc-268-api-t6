"""Deployment contracts for the staging broker, cache, workers and application secrets (#35).

The behavioural tests run the real `run:` script of the CI bundle step and the real deploy and
rollback scripts against a fake docker CLI: they cover what reaches the host and what the host
keeps across deploys and rollbacks, without a VPS or a Docker daemon.
"""

from __future__ import annotations

import base64
import os
import re
import shutil
import stat
import subprocess
import textwrap
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci-cd.yml"
STAGING_COMPOSE = REPO_ROOT / "deploy" / "compose" / "staging.yml"
PROJECT = "dmc-268-api-staging"

# GitHub rejects secret names starting with GITHUB_, so the App secrets are GH_* in the
# Environment (comment of the tech lead in #35); the container keeps the names of .env.example.
SECRET_SOURCES = {
    "GITHUB_APP_ID": "GH_APP_ID",
    "GITHUB_APP_PRIVATE_KEY": "GH_APP_PRIVATE_KEY",
    "GITHUB_WEBHOOK_SECRET": "GH_WEBHOOK_SECRET",
    "GITHUB_CLIENT_ID": "GH_CLIENT_ID",
    "GITHUB_CLIENT_SECRET": "GH_CLIENT_SECRET",
    "AUTH_JWT_PRIVATE_KEY": "AUTH_JWT_PRIVATE_KEY",
    "AUTH_JWT_PUBLIC_KEY": "AUTH_JWT_PUBLIC_KEY",
    # Organization secret of the LLM gateway (decision of the tech lead in #35, 04.10).
    "LLM_API_KEYS": "AI_DMC268_T6",
}
# Non-secret configuration, routed like the secrets: the bot login is a variable of the
# Environment, the LLM models are repository variables (an Environment variable of the same
# name overrides them), and the endpoint of the LLM gateway is an organization variable.
VARIABLE_SOURCES = {
    "GITHUB_APP_BOT_LOGIN": "GH_APP_BOT_LOGIN",
    "LLM_BASE_URL": "AI_DMC268_URL",
    "LLM_MODEL": "LLM_MODEL",
    "LLM_FALLBACK_MODEL": "LLM_FALLBACK_MODEL",
}
# One env file per set of recipients (docs/SECRETS.md); its bash array in env-file.sh.
ENV_FILES = {
    "app.env": ("APP_ENV_KEYS", {"GITHUB_APP_ID", "GITHUB_APP_PRIVATE_KEY"}),  # both workers
    "api.env": (
        "API_ENV_KEYS",
        {
            "GITHUB_WEBHOOK_SECRET",
            "GITHUB_CLIENT_ID",
            "GITHUB_CLIENT_SECRET",
            "AUTH_JWT_PRIVATE_KEY",
            "AUTH_JWT_PUBLIC_KEY",
        },
    ),
    "worker.env": (
        "WORKER_ENV_KEYS",
        {"LLM_API_KEYS", "LLM_BASE_URL", "LLM_MODEL", "LLM_FALLBACK_MODEL"},
    ),
    "webhook-worker.env": ("WEBHOOK_WORKER_ENV_KEYS", {"GITHUB_APP_BOT_LOGIN"}),
}
BUNDLED = {**SECRET_SOURCES, **VARIABLE_SOURCES}

# Multi-line like a PEM, with what a shell or Compose could rewrite: $, ${...} and backslashes.
FAKE_PEM = "-----BEGIN TEST KEY-----\nAAAA$HOME/${PATH}\nBB\\nBB\\\\CC\n-----END TEST KEY-----"
CI_SECRETS = {
    "GITHUB_APP_ID": "424242",
    "GITHUB_APP_PRIVATE_KEY": FAKE_PEM,
    "GITHUB_WEBHOOK_SECRET": "",  # not set in the Environment: GitHub yields an empty string
    "GITHUB_CLIENT_ID": "Iv23-test-client",
    "GITHUB_CLIENT_SECRET": "aaaa$bbbb\\cccc${dddd}",
    "AUTH_JWT_PRIVATE_KEY": FAKE_PEM.replace("TEST KEY", "TEST SIGNING"),
    "AUTH_JWT_PUBLIC_KEY": "-----BEGIN TEST VERIFY-----\nCCCC\n-----END TEST VERIFY-----",
    "LLM_API_KEYS": "sk-test-1,sk-test-2",
    "LLM_BASE_URL": "https://llm.test/api/v1",
    "GITHUB_APP_BOT_LOGIN": "reviewer[bot]",
    "LLM_MODEL": "gpt-4.1-mini",
    "LLM_FALLBACK_MODEL": "mistral-small-3.2-24b",
}

FAKE_DOCKER = """\
#!/usr/bin/env bash
# Fake docker CLI: logs every call and answers only what deploy.sh and rollback.sh ask.
printf '%s\\n' "$*" >> "${STUB_LOG}"
case "$1" in
  volume) [[ "$2" == inspect && -e "${STUB_VOLUMES}/$3" ]] ;;
  inspect) exit 1 ;;
  compose)
    shift
    project_dir=""
    env_file=""
    action=""
    while (( $# )); do
      case "$1" in
        -p) shift 2 ;;
        -f) [[ -n "${project_dir}" ]] || project_dir="$(dirname "$2")"; shift 2 ;;
        --env-file) env_file="$2"; shift 2 ;;
        *) [[ -n "${action}" ]] || action="$1"; shift ;;
      esac
    done
    case "${action}" in
      up)
        # Like Compose: env_file paths resolve against the directory of the first compose file.
        # Every file must exist: deploy and rollback keep each role's file in place.
        for name in app.env api.env worker.env webhook-worker.env; do
          [[ -f "${project_dir}/${name}" ]] || { echo "env file ${name} not found" >&2; exit 1; }
        done
        ! grep -qxF "IMAGE=${STUB_FAILING_IMAGE:-}" "${env_file}"
        ;;
      down) exit "${STUB_DOWN_STATUS:-0}" ;;
    esac
    ;;
esac
"""

_ENV_FILE_ENTRY = re.compile(r"^([A-Z][A-Z0-9_]*)='([^']*)'\n", re.MULTILINE)


def _read(*parts: str) -> str:
    return REPO_ROOT.joinpath(*parts).read_text(encoding="utf-8")


def _service_block(name: str) -> str:
    compose = STAGING_COMPOSE.read_text(encoding="utf-8")
    match = re.search(rf"^  {name}:\n((?:    .*\n|\n)*)", compose, re.MULTILINE)
    assert match is not None, f"service {name} is missing"
    return match.group(1)


def _workflow_step(name: str, workflow_file: Path = WORKFLOW) -> str:
    workflow = workflow_file.read_text(encoding="utf-8")
    header = f"      - name: {name}\n"
    assert workflow.count(header) == 1, f"step {name!r} must exist exactly once"
    start = workflow.index(header)
    end = workflow.find("\n      - name: ", start + len(header))
    return workflow[start : end if end != -1 else len(workflow)]


def _run_script(step: str) -> str:
    """The ``run: |`` block of a workflow step, dedented."""
    match = re.search(r"^        run: \|\n((?:          .*\n|\n)*)", step, re.MULTILINE)
    assert match is not None, "the step has no run block"
    return textwrap.dedent(match.group(1)).strip("\n")


def _bash_array(script: str, name: str) -> list[str]:
    match = re.search(rf"^\s*{name}=\(([^)]*)\)", script, re.MULTILINE)
    assert match is not None, f"array {name} is missing"
    return match.group(1).split()


def _bundle(secrets: Mapping[str, str]) -> str:
    lines = "".join(
        f"{name}={base64.b64encode(value.encode()).decode()}\n" for name, value in secrets.items()
    )
    return base64.b64encode(lines.encode()).decode()


def _read_env_file(path: Path) -> dict[str, str]:
    """Parse NAME='value' entries the way Compose reads them; anything else fails the test."""
    text = path.read_text(encoding="utf-8")
    entries = dict(_ENV_FILE_ENTRY.findall(text))
    assert "".join(f"{name}='{value}'\n" for name, value in entries.items()) == text
    return entries


def _read_dotenv(path: Path) -> dict[str, str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    return {name: value for name, _, value in (line.partition("=") for line in lines)}


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


@dataclass(frozen=True)
class Host:
    """A fake staging host: APP_DIR with the uploaded deploy files and a fake docker on PATH."""

    app_dir: Path
    bin_dir: Path
    volumes: Path
    log: Path
    tmp: Path

    @property
    def env_file(self) -> Path:
        return self.app_dir / ".env"

    def files(self) -> dict[str, bytes]:
        return {path.name: path.read_bytes() for path in sorted(self.app_dir.iterdir())}

    def calls(self) -> list[str]:
        return self.log.read_text(encoding="utf-8").splitlines() if self.log.exists() else []

    def add_volume(self, name: str) -> None:
        (self.volumes / f"{PROJECT}_{name}").touch()

    def run(
        self, script: str, *args: str, env: Mapping[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        environment = {
            "PATH": f"{self.bin_dir}{os.pathsep}{os.environ['PATH']}",
            "HOME": str(self.app_dir.parent),
            "APP_DIR": str(self.app_dir),
            "STUB_LOG": str(self.log),
            "STUB_VOLUMES": str(self.volumes),
            "TMPDIR": str(self.tmp),
            **(env or {}),
        }
        return subprocess.run(
            [str(self.app_dir / script), *args],
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )

    def deploy(
        self, image: str, *, bundle: str | None = None, env: Mapping[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        # The variables the "Deploy image" step hands to deploy.sh on the course VPS.
        ci_env = {
            "DEPLOY_MODE": "edge",
            "EDGE_ALIAS": "api-staging",
            "POSTGRES_USER": "",
            "POSTGRES_PASSWORD": "",
            "POSTGRES_DB": "",
        }
        if bundle is not None:
            ci_env["APP_SECRETS_B64"] = bundle
        return self.run("deploy.sh", image, env={**ci_env, **(env or {})})


@pytest.fixture
def host(tmp_path: Path) -> Host:
    app_dir = tmp_path / "opt" / PROJECT
    app_dir.mkdir(parents=True)
    # The same renames as the "Prepare host" step.
    shutil.copy(STAGING_COMPOSE, app_dir / "compose.yml")
    shutil.copy(REPO_ROOT / "deploy" / "compose" / "staging.edge.yml", app_dir / "compose.edge.yml")
    for script in ("deploy.sh", "rollback.sh", "env-file.sh"):
        target = app_dir / script
        shutil.copy(REPO_ROOT / "deploy" / "scripts" / script, target)
        target.chmod(0o755)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text(FAKE_DOCKER, encoding="utf-8")
    docker.chmod(0o755)
    volumes = tmp_path / "volumes"
    volumes.mkdir()
    tmp = tmp_path / "tmp"
    tmp.mkdir()
    return Host(
        app_dir=app_dir, bin_dir=bin_dir, volumes=volumes, log=tmp_path / "docker.log", tmp=tmp
    )


def _run_bundle_step(tmp_path: Path, secrets: Mapping[str, str]) -> tuple[str, str]:
    """Run the step script as GitHub does (bash -eo pipefail); return (stdout, bundle output)."""
    step = _workflow_step("Bundle application secrets")
    script = tmp_path / "bundle-step.sh"
    script.write_text(textwrap.dedent(step.split("        run: |\n", 1)[1]), encoding="utf-8")
    output = tmp_path / "github-output"
    result = subprocess.run(
        ["bash", "--noprofile", "--norc", "-eo", "pipefail", str(script)],
        env={"PATH": os.environ["PATH"], "GITHUB_OUTPUT": str(output), **secrets},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    (line,) = output.read_text(encoding="utf-8").splitlines()
    assert line.startswith("bundle=")
    return result.stdout, line.removeprefix("bundle=")


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

    result = host.deploy("ghcr.io/test/api@sha256:a", bundle=bundle)

    assert result.returncode == 0, result.stderr
    assert "not set: LLM_FALLBACK_MODEL" in stdout.splitlines()
    assert _read_env_file(host.app_dir / "worker.env") == {
        "LLM_API_KEYS": "sk-test-1,sk-test-2",
        "LLM_BASE_URL": "https://llm.test/api/v1",
        "LLM_MODEL": "gpt-4.1-mini",
    }


def test_legacy_eur_rate_is_ignored_by_bundle_and_worker_env(tmp_path: Path, host: Host) -> None:
    stdout, bundle = _run_bundle_step(tmp_path, {**CI_SECRETS, "LLM_EUR_TO_USD_RATE": "1.1204"})

    result = host.deploy("ghcr.io/test/api@sha256:a", bundle=bundle)

    assert result.returncode == 0, result.stderr
    assert all("LLM_EUR_TO_USD_RATE" not in line for line in stdout.splitlines())
    assert "LLM_EUR_TO_USD_RATE" not in _read_env_file(host.app_dir / "worker.env")


def test_staging_worker_uses_nonisolated_default_network_for_ecb_https() -> None:
    compose = STAGING_COMPOSE.read_text(encoding="utf-8")
    worker = compose.split("  worker:\n", 1)[1].split("\n  webhook-worker:", 1)[0]

    assert "network_mode: none" not in worker
    assert "networks:" not in worker  # Compose's project default network has outbound egress.
    assert "internal: true" not in compose


def test_deploy_step_forwards_the_bundle_to_the_host() -> None:
    step = _workflow_step("Deploy image")
    envs = re.search(r"^\s+envs: (\S+)$", step, re.MULTILINE)

    assert "APP_SECRETS_B64: ${{ steps.app_secrets.outputs.bundle }}" in step
    assert envs is not None
    assert "APP_SECRETS_B64" in envs.group(1).split(",")


def test_host_allowlist_follows_the_container_table() -> None:
    env_file = _read("deploy", "scripts", "env-file.sh")

    for array, keys in ENV_FILES.values():
        assert set(_bash_array(env_file, array)) == keys, array
    # Every bundled name has exactly one file.
    assert sorted(k for _, keys in ENV_FILES.values() for k in keys) == sorted(BUNDLED)


@pytest.mark.parametrize(
    "secrets",
    [CI_SECRETS, {**CI_SECRETS, "GITHUB_WEBHOOK_SECRET": "whsec$1\\x"}],
    ids=["webhook-secret-unset", "all-set"],
)
def test_secrets_from_ci_land_in_the_right_files_unchanged(
    tmp_path: Path, host: Host, secrets: Mapping[str, str]
) -> None:
    stdout, bundle = _run_bundle_step(tmp_path, secrets)

    result = host.deploy("ghcr.io/test/api@sha256:a", bundle=bundle)

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

    assert host.deploy("ghcr.io/test/api@sha256:a", bundle=bundle).returncode == 0

    assert _read_env_file(host.app_dir / "app.env") == {"GITHUB_APP_ID": "7"}
    for name in ("api.env", "worker.env", "webhook-worker.env"):
        assert _read_env_file(host.app_dir / name) == {}


# --- Host: generated credentials, rollbacks and refusals ----------------------------------------


def test_first_deploy_generates_store_passwords_once(host: Host) -> None:
    assert host.deploy("ghcr.io/test/api@sha256:a", bundle="").returncode == 0
    first = _read_dotenv(host.env_file)
    assert host.deploy("ghcr.io/test/api@sha256:b", bundle="").returncode == 0
    second = _read_dotenv(host.env_file)

    for name in ("POSTGRES_PASSWORD", "RABBITMQ_PASSWORD", "REDIS_PASSWORD"):
        assert re.fullmatch(r"[0-9a-f]{48}", first[name]), name
        assert second[name] == first[name], name
    assert first["RABBITMQ_USER"] == "app"
    assert second["IMAGE"] == "ghcr.io/test/api@sha256:b"
    assert list(host.tmp.iterdir()) == []  # the per-run DOCKER_CONFIG is removed


def test_rollbacks_keep_store_passwords_and_app_secrets(host: Host) -> None:
    bundle = _bundle(CI_SECRETS)
    assert host.deploy("ghcr.io/test/api@sha256:a", bundle=bundle).returncode == 0
    assert host.deploy("ghcr.io/test/api@sha256:b", bundle=bundle).returncode == 0
    deployed = _read_dotenv(host.env_file)
    secrets = {name: (host.app_dir / name).read_bytes() for name in ENV_FILES}

    manual = host.run("rollback.sh")
    after_manual = _read_dotenv(host.env_file)
    secrets_after_manual = {name: (host.app_dir / name).read_bytes() for name in ENV_FILES}
    # The failing deploy brings rotated secrets, one of them no longer set. They stay after the
    # automatic rollback: secrets are not tied to an image (docs/SECRETS.md §3).
    rotated = {
        **CI_SECRETS,
        "GITHUB_APP_ID": "2",
        "GITHUB_CLIENT_ID": "client",
        "AUTH_JWT_PUBLIC_KEY": "",
        "LLM_API_KEYS": "sk-rotated",
        "GITHUB_APP_BOT_LOGIN": "rotated[bot]",
    }
    failed = host.deploy(
        "ghcr.io/test/api@sha256:bad",
        bundle=_bundle(rotated),
        env={"STUB_FAILING_IMAGE": "ghcr.io/test/api@sha256:bad"},
    )
    after_auto = _read_dotenv(host.env_file)

    assert manual.returncode == 0, manual.stderr
    assert after_manual["IMAGE"] == "ghcr.io/test/api@sha256:a"
    assert failed.returncode != 0
    assert "rolled back to ghcr.io/test/api@sha256:a" in failed.stdout
    assert after_auto["IMAGE"] == "ghcr.io/test/api@sha256:a"
    for name in ("POSTGRES_PASSWORD", "RABBITMQ_USER", "RABBITMQ_PASSWORD", "REDIS_PASSWORD"):
        assert after_manual[name] == deployed[name], name
        assert after_auto[name] == deployed[name], name
    assert all(secrets.values())  # every role has something to lose
    assert secrets_after_manual == secrets
    for file_name, (_, keys) in ENV_FILES.items():
        assert _read_env_file(host.app_dir / file_name) == {
            name: rotated[name] for name in keys if rotated[name]
        }, file_name


@pytest.mark.parametrize("name", ["POSTGRES_PASSWORD", "RABBITMQ_PASSWORD", "REDIS_PASSWORD"])
def test_rollback_refuses_without_a_store_password(host: Host, name: str) -> None:
    assert host.deploy("ghcr.io/test/api@sha256:a", bundle="").returncode == 0
    assert host.deploy("ghcr.io/test/api@sha256:b", bundle="").returncode == 0
    env = host.env_file.read_text(encoding="utf-8")
    host.env_file.write_text(
        "".join(line for line in env.splitlines(True) if not line.startswith(f"{name}=")),
        encoding="utf-8",
    )
    before = host.files()
    calls_before = len(host.calls())

    result = host.run("rollback.sh")

    assert result.returncode != 0
    assert f"{name} is required" in result.stderr
    assert host.files() == before
    assert not any(call.startswith("compose") for call in host.calls()[calls_before:])


def test_rollback_recreates_missing_app_secret_files(host: Host) -> None:
    assert host.deploy("ghcr.io/test/api@sha256:a", bundle="").returncode == 0
    assert host.deploy("ghcr.io/test/api@sha256:b", bundle="").returncode == 0
    for name in ENV_FILES:
        (host.app_dir / name).unlink()

    result = host.run("rollback.sh")

    assert result.returncode == 0, result.stderr
    for name in ENV_FILES:
        assert _mode(host.app_dir / name) == 0o600


def test_host_deployed_before_the_broker_keeps_its_postgres_password(host: Host) -> None:
    postgres_password = "0" * 48
    host.env_file.write_text(
        "IMAGE=ghcr.io/test/api@sha256:old\nPOSTGRES_USER=app\n"
        f"POSTGRES_PASSWORD={postgres_password}\nPOSTGRES_DB=app\n"
        "DEPLOY_MODE=edge\nEDGE_ALIAS=api-staging\n",
        encoding="utf-8",
    )
    host.add_volume("postgres-data")

    result = host.run("deploy.sh", "ghcr.io/test/api@sha256:a")

    assert result.returncode == 0, result.stderr
    env = _read_dotenv(host.env_file)
    assert env["POSTGRES_PASSWORD"] == postgres_password
    assert re.fullmatch(r"[0-9a-f]{48}", env["RABBITMQ_PASSWORD"])
    assert re.fullmatch(r"[0-9a-f]{48}", env["REDIS_PASSWORD"])
    for name in ENV_FILES:
        assert (host.app_dir / name).read_text(encoding="utf-8") == ""
        assert _mode(host.app_dir / name) == 0o600


def test_broker_volume_without_a_password_is_never_given_a_new_one(host: Host) -> None:
    assert host.deploy("ghcr.io/test/api@sha256:a", bundle="").returncode == 0
    env = host.env_file.read_text(encoding="utf-8")
    host.env_file.write_text(
        "".join(line for line in env.splitlines(True) if not line.startswith("RABBITMQ_PASSWORD=")),
        encoding="utf-8",
    )
    host.add_volume("rabbitmq-data")
    before = host.files()

    result = host.deploy("ghcr.io/test/api@sha256:b", bundle="")

    assert result.returncode != 0
    assert "refusing to generate a new password" in result.stderr
    assert host.files() == before


@pytest.mark.parametrize(
    ("bundle", "message"),
    [
        (_bundle({"PATH": "/tmp"}), "PATH is not in the allowlist"),
        (_bundle({"GITHUB_CLIENT_SECRET": "abc'def"}), "single quotes are not supported"),
        (_bundle({"GITHUB_WEBHOOK_SECRET": "abc\\"}), "a trailing backslash is not supported"),
        # GNU base64 rejects the input; the macOS one decodes garbage that fails the line check.
        ("not base64!", "not valid base64|malformed line"),
    ],
    ids=["unknown-name", "single-quote", "trailing-backslash", "not-base64"],
)
def test_invalid_bundle_fails_before_the_host_changes(
    host: Host, bundle: str, message: str
) -> None:
    assert (
        host.deploy("ghcr.io/test/api@sha256:a", bundle=_bundle({"GITHUB_APP_ID": "1"})).returncode
        == 0
    )
    before = host.files()

    result = host.deploy("ghcr.io/test/api@sha256:b", bundle=bundle)

    assert result.returncode != 0
    assert re.search(message, result.stderr)
    assert "abc" not in result.stderr
    assert host.files() == before  # .env, state and secrets untouched, no temporary files left


def test_empty_bundle_clears_app_secrets_and_a_missing_one_keeps_them(host: Host) -> None:
    secrets = _bundle({"GITHUB_APP_ID": "1", "GITHUB_CLIENT_ID": "client"})
    assert host.deploy("ghcr.io/test/api@sha256:a", bundle=secrets).returncode == 0

    assert host.deploy("ghcr.io/test/api@sha256:b").returncode == 0
    kept = {name: _read_env_file(host.app_dir / name) for name in ("app.env", "api.env")}
    assert host.deploy("ghcr.io/test/api@sha256:c", bundle="").returncode == 0
    cleared = {name: _read_env_file(host.app_dir / name) for name in ("app.env", "api.env")}

    assert kept == {"app.env": {"GITHUB_APP_ID": "1"}, "api.env": {"GITHUB_CLIENT_ID": "client"}}
    assert cleared == {"app.env": {}, "api.env": {}}


def test_rollback_without_a_release_stops_the_stack_by_project_name(host: Host) -> None:
    # A host whose .env predates the broker: loading compose.yml would fail on RABBITMQ_PASSWORD.
    host.env_file.write_text("DEPLOY_MODE=edge\nEDGE_ALIAS=api-staging\n", encoding="utf-8")

    result = host.run("rollback.sh")

    assert result.returncode == 0, result.stderr
    calls = host.calls()
    down = calls.index(f"compose -p {PROJECT} down --remove-orphans")
    started = next(i for i, call in enumerate(calls) if call.startswith("run -d --name"))
    assert down < started
    assert "--network-alias api-staging" in calls[started]


def test_rollback_does_not_start_the_bootstrap_next_to_a_running_stack(host: Host) -> None:
    host.env_file.write_text("DEPLOY_MODE=edge\nEDGE_ALIAS=api-staging\n", encoding="utf-8")

    result = host.run("rollback.sh", env={"STUB_DOWN_STATUS": "1"})

    assert result.returncode != 0
    assert "not starting the bootstrap container" in result.stderr
    assert not any(call.startswith("run ") for call in host.calls())


# --- Compose, workflow and edge contracts -------------------------------------------------------


def test_staging_stores_publish_no_host_ports() -> None:
    for service in ("postgres", "rabbitmq", "redis"):
        assert "ports:" not in _service_block(service)
    # Overrides may publish the API, never a store.
    for override in STAGING_COMPOSE.parent.glob("staging.*.yml"):
        text = override.read_text(encoding="utf-8")
        for service in ("postgres", "rabbitmq", "redis"):
            assert f"\n  {service}:" not in text, f"{override.name} overrides {service}"


def test_rabbitmq_keeps_its_node_name_across_recreates() -> None:
    rabbitmq = _service_block("rabbitmq")

    assert re.search(r"image: rabbitmq:4\.[\d.]+-management-alpine@sha256:", rabbitmq)
    assert "hostname: rabbitmq" in rabbitmq
    assert "rabbitmq-data:/var/lib/rabbitmq" in rabbitmq


def test_redis_is_a_bounded_password_protected_cache() -> None:
    redis = _service_block("redis")

    assert re.search(r"image: redis:8\.[\d.]+-alpine@sha256:", redis)
    assert "requirepass %s" in redis
    assert "--requirepass" not in redis  # the password stays out of argv
    assert "maxmemory 128mb" in redis
    assert "maxmemory-policy allkeys-lru" in redis
    assert "volumes:" not in redis


def test_api_starts_without_the_broker_and_the_cache() -> None:
    api = _service_block("api")
    depends_on = api[api.index("    depends_on:\n") :]
    dependencies = re.findall(
        r"^      ([a-z-]+):$", depends_on[: depends_on.index("    restart:")], re.M
    )

    assert dependencies == ["bootstrap", "postgres"]


def test_api_gets_only_its_own_env_file() -> None:
    api = _service_block("api")
    env_files = api[api.index("    env_file:\n") : api.index("    environment:\n")]

    # The API reads no GitHub App credentials: app.env belongs to the workers.
    assert re.findall(r"^      - (\S+)$", env_files, re.MULTILINE) == ["api.env"]
    assert "GITHUB_" not in api
    assert "AUTH_JWT_PRIVATE_KEY" not in api


def _list_under(block: str, key: str) -> list[str]:
    """Items (``- x``) or keys (``x:``) directly under a top-level key of a service block."""
    match = re.search(rf"^    {key}:\n((?:      .*\n)*)", block, re.MULTILINE)
    assert match is not None, f"{key} is missing"
    return re.findall(r"^      (?:- )?([a-z][a-z.-]*):?$", match.group(1), re.MULTILINE)


@pytest.mark.parametrize(
    ("service", "module", "env_files", "dependencies"),
    [
        ("worker", "app.worker", ["app.env", "worker.env"], ["bootstrap", "postgres", "rabbitmq"]),
        # The projection creates Runs and publishes them to the review queue (#52).
        (
            "webhook-worker",
            "app.webhook_worker",
            ["app.env", "webhook-worker.env"],
            ["bootstrap", "postgres", "rabbitmq"],
        ),
    ],
)
def test_workers_run_from_the_api_image_with_their_own_files_and_dependencies(
    service: str, module: str, env_files: list[str], dependencies: list[str]
) -> None:
    block = _service_block(service)

    assert "image: ${IMAGE:?IMAGE is required}" in block
    assert f'command: ["python", "-m", "{module}"]' in block
    assert _list_under(block, "env_file") == env_files
    assert _list_under(block, "depends_on") == dependencies
    assert "condition: service_completed_successfully" in block  # bootstrap: migrations first
    assert "DATABASE_URL: postgresql+psycopg://" in block
    assert ("RABBITMQ_URL: amqp://" in block) == ("rabbitmq" in dependencies)
    assert "restart: unless-stopped" in block
    assert "ports:" not in block


@pytest.mark.parametrize("service", ["worker", "webhook-worker"])
def test_worker_healthcheck_reads_its_heartbeat_not_the_http_port(service: str) -> None:
    block = _service_block(service)
    heartbeat = re.search(r"^      WORKER_HEARTBEAT_FILE: (\S+)$", block, re.MULTILINE)
    test = re.search(r"^      test:\n        - CMD-SHELL\n        - (.+)$", block, re.MULTILINE)

    assert heartbeat is not None and test is not None
    # The image HEALTHCHECK polls :8000/healthcheck, which no worker serves: it must be replaced.
    assert "8000" not in block and "urllib" not in block
    # The module file is the marker of an image that beats; WORKDIR /srv, app copied to ./app.
    module = REPO_ROOT / "app" / "common" / "infrastructure" / "heartbeat.py"
    assert module.is_file()
    assert test.group(1) == (
        "test ! -f /srv/app/common/infrastructure/heartbeat.py || exec python -m "
        f"app.common.infrastructure.heartbeat {heartbeat.group(1)} 30"
    )
    assert "COPY app ./app" in _read("Dockerfile") and "WORKDIR /srv" in _read("Dockerfile")


def test_workers_are_not_on_the_edge_network() -> None:
    edge = (STAGING_COMPOSE.parent / "staging.edge.yml").read_text(encoding="utf-8")
    ports = (STAGING_COMPOSE.parent / "staging.ports.yml").read_text(encoding="utf-8")

    for service in ("worker", "webhook-worker"):
        assert f"\n  {service}:" not in edge
        assert f"\n  {service}:" not in ports


def test_push_image_waits_for_the_required_python_check() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    push_image = workflow[workflow.index("  push-image:") : workflow.index("  deploy-staging:")]

    assert "- python-lint-type-test" in push_image


def test_ci_lints_the_openapi_contract_before_the_push() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    job = workflow[workflow.index("  openapi-lint:") : workflow.index("  docker-build:")]
    push_image = workflow[workflow.index("  push-image:") : workflow.index("  deploy-staging:")]

    assert "name: OpenAPI lint" in job
    # Pinned CLI, the same command as locally: the repo-root redocly.yaml and
    # .redocly.lint-ignore.yaml apply, no CLI flag overrides them.
    assert re.search(
        r"^        run: npx --yes @redocly/cli@2\.57\.0 lint contracts/openapi\.yaml$",
        job,
        re.MULTILINE,
    )
    assert "--extends" not in job
    # Redocly exits 0 on warnings; the config's strict ruleset turns every warning into an error.
    assert re.search(r"^extends:\n  - recommended-strict$", _read("redocly.yaml"), re.MULTILINE)
    assert "- openapi-lint" in push_image


def test_ci_validates_the_edge_caddyfile_before_the_push() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert "\n  edge-validate:\n" in workflow
    next_job = workflow.index("  terraform-lint-security:")
    job = workflow[workflow.index("  edge-validate:") : next_job]
    push_image = workflow[workflow.index("  push-image:") : workflow.index("  deploy-staging:")]
    script = _run_script(_workflow_step("caddy validate"))

    assert "name: Edge Caddyfile validate" in job
    assert "      - name: caddy validate\n" in job
    # The image the VPS runs comes from deploy/edge/compose.yml: no second tag to keep in step.
    assert "image=\"$(sed -n 's/^    image: //p' deploy/edge/compose.yml)\"" in script.splitlines()
    assert not re.search(r"\bcaddy(?::\d|@sha256:)", workflow)
    # The same command as docs/CICD.md §8.2; unset, APP_DOMAIN leaves the apex site keyless.
    assert (
        "docker run --rm -e APP_DOMAIN=example.test "
        '-v "$PWD/deploy/edge:/etc/caddy:ro" "${image}" \\\n'
        "  caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile"
    ) in script
    # The deploy runs caddy reload with this file: a broken one must stop the pipeline first.
    assert "- edge-validate" in push_image


def test_ui_host_sends_api_paths_to_the_api_and_the_rest_to_the_ui() -> None:
    caddyfile = _read("deploy", "edge", "Caddyfile")
    ui_site = caddyfile[caddyfile.index("staging-ui.{$APP_DOMAIN} {") :]
    ui_site = ui_site[: ui_site.index("\n}\n")]
    api_route = re.search(r"handle /api/\* \{\n(.*?)\n\t\}", ui_site, re.DOTALL)
    fallback = re.search(r"handle \{\n(.*?)\n\t\}", ui_site, re.DOTALL)

    assert api_route is not None and fallback is not None
    assert api_route.group(1).strip() == "reverse_proxy api-staging:8000"
    assert fallback.group(1).strip() == "reverse_proxy ui-staging:8080"
    assert api_route.start() < fallback.start()
