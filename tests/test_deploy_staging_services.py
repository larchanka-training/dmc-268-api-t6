"""Deployment contracts for the staging broker, cache and application secrets (#35).

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
}
APP_ENV_KEYS = {"GITHUB_APP_ID", "GITHUB_APP_PRIVATE_KEY"}  # worker and webhook-worker
API_ENV_KEYS = set(SECRET_SOURCES) - APP_ENV_KEYS  # api only

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
        # Both files must exist: deploy and rollback keep app.env in place for the workers too.
        for name in app.env api.env; do
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


def _workflow_step(name: str) -> str:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    header = f"      - name: {name}\n"
    assert workflow.count(header) == 1, f"step {name!r} must exist exactly once"
    start = workflow.index(header)
    end = workflow.find("\n      - name: ", start + len(header))
    return workflow[start : end if end != -1 else len(workflow)]


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
    mapping = dict(re.findall(r"^\s+([A-Z_]+): \$\{\{ secrets\.([A-Z_]+) \}\}$", step, re.M))

    assert "id: app_secrets" in step
    assert "shell: bash" in step  # bash -eo pipefail, as _run_bundle_step runs it
    assert mapping == SECRET_SOURCES
    assert set(_bash_array(step, "names")) == set(SECRET_SOURCES)


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
    stdout, bundle = _run_bundle_step(tmp_path, dict.fromkeys(SECRET_SOURCES, ""))
    names = _bash_array(_workflow_step("Bundle application secrets"), "names")

    assert bundle == ""
    assert stdout.splitlines() == [f"not set: {name}" for name in names]


def test_deploy_step_forwards_the_bundle_to_the_host() -> None:
    step = _workflow_step("Deploy image")
    envs = re.search(r"^\s+envs: (\S+)$", step, re.MULTILINE)

    assert "APP_SECRETS_B64: ${{ steps.app_secrets.outputs.bundle }}" in step
    assert envs is not None
    assert "APP_SECRETS_B64" in envs.group(1).split(",")


def test_host_allowlist_follows_the_container_table() -> None:
    env_file = _read("deploy", "scripts", "env-file.sh")

    assert set(_bash_array(env_file, "APP_ENV_KEYS")) == APP_ENV_KEYS
    assert set(_bash_array(env_file, "API_ENV_KEYS")) == API_ENV_KEYS


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
    app_env = _read_env_file(host.app_dir / "app.env")
    api_env = _read_env_file(host.app_dir / "api.env")
    assert app_env == {name: expected[name] for name in APP_ENV_KEYS}
    assert api_env == {name: expected[name] for name in API_ENV_KEYS if name in expected}
    for name in secrets.keys() - expected.keys():
        assert name not in app_env and name not in api_env  # absent, not an empty string
    for name in (".env", "app.env", "api.env"):
        assert _mode(host.app_dir / name) == 0o600


def test_empty_value_in_the_bundle_is_left_out(host: Host) -> None:
    bundle = _bundle({"GITHUB_APP_ID": "7", "GITHUB_CLIENT_ID": ""})

    assert host.deploy("ghcr.io/test/api@sha256:a", bundle=bundle).returncode == 0

    assert _read_env_file(host.app_dir / "app.env") == {"GITHUB_APP_ID": "7"}
    assert _read_env_file(host.app_dir / "api.env") == {}


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
    bundle = _bundle({"GITHUB_APP_ID": "1", "AUTH_JWT_PUBLIC_KEY": FAKE_PEM})
    assert host.deploy("ghcr.io/test/api@sha256:a", bundle=bundle).returncode == 0
    assert host.deploy("ghcr.io/test/api@sha256:b", bundle=bundle).returncode == 0
    deployed = _read_dotenv(host.env_file)
    secrets = {name: (host.app_dir / name).read_bytes() for name in ("app.env", "api.env")}

    manual = host.run("rollback.sh")
    after_manual = _read_dotenv(host.env_file)
    secrets_after_manual = {name: (host.app_dir / name).read_bytes() for name in secrets}
    # The failing deploy brings rotated secrets. They stay after the automatic rollback: secrets
    # are not tied to an image (docs/SECRETS.md §3).
    rotated = _bundle({"GITHUB_APP_ID": "2", "GITHUB_CLIENT_ID": "client"})
    failed = host.deploy(
        "ghcr.io/test/api@sha256:bad",
        bundle=rotated,
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
    assert secrets_after_manual == secrets
    assert _read_env_file(host.app_dir / "app.env") == {"GITHUB_APP_ID": "2"}
    assert _read_env_file(host.app_dir / "api.env") == {"GITHUB_CLIENT_ID": "client"}


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
    for name in ("app.env", "api.env"):
        (host.app_dir / name).unlink()

    result = host.run("rollback.sh")

    assert result.returncode == 0, result.stderr
    for name in ("app.env", "api.env"):
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
    for name in ("app.env", "api.env"):
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

    assert "image: rabbitmq:4-management-alpine" in rabbitmq
    assert "hostname: rabbitmq" in rabbitmq
    assert "rabbitmq-data:/var/lib/rabbitmq" in rabbitmq


def test_redis_is_a_bounded_password_protected_cache() -> None:
    redis = _service_block("redis")

    assert "image: redis:8-alpine" in redis
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


def test_push_image_waits_for_the_required_python_check() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    push_image = workflow[workflow.index("  push-image:") : workflow.index("  deploy-staging:")]

    assert "- python-lint-type-test" in push_image


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
