"""Deployment contracts for the staging broker, cache, workers and application secrets (#35).

The behavioural tests run the real `run:` script of the CI bundle step and the real deploy and
rollback scripts against a fake docker CLI: they cover what reaches the host and what the host
keeps across deploys and rollbacks, without a VPS or a Docker daemon.
"""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import textwrap
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci-cd.yml"
ROLLBACK_WORKFLOW = WORKFLOW.parent / "rollback.yml"
EDGE_GUARD = "        if: steps.target.outputs.deploy_mode == 'edge'"
STAGING_COMPOSE = REPO_ROOT / "deploy" / "compose" / "staging.yml"
PROJECT = "dmc-268-api-staging"
IMAGE_A = "ghcr.io/test/api@sha256:" + "a" * 64
IMAGE_B = "ghcr.io/test/api@sha256:" + "b" * 64
IMAGE_C = "ghcr.io/test/api@sha256:" + "c" * 64
IMAGE_EXPLICIT = "ghcr.io/test/api@sha256:" + "e" * 64
IMAGE_BAD = "ghcr.io/test/api@sha256:" + "f" * 64
A_ID = "sha256:" + "1" * 64
B_ID = "sha256:" + "2" * 64
EXPLICIT_ID = "sha256:" + "3" * 64
# rollback.sh's answer to a CI auto rollback whose deploy run never reached the release switch.
STOPPED_BEFORE_SWITCH = (
    "release unchanged: deploy run {} stopped before the release switch; skipping auto rollback\n"
)

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
    "GITLAB_CREDENTIAL_KEYS": "GITLAB_CREDENTIAL_KEYS",
}
# Non-secret configuration, routed like the secrets: the bot login is a variable of the
# Environment, the LLM models (review and verifier pairs) are repository variables (an
# Environment variable of the same name overrides them), the portal URL is an Environment
# variable, and the endpoint of the LLM gateway is an organization variable.
VARIABLE_SOURCES = {
    "GITHUB_APP_BOT_LOGIN": "GH_APP_BOT_LOGIN",
    "PORTAL_URL": "PORTAL_URL",
    "LLM_BASE_URL": "AI_DMC268_URL",
    "LLM_MODEL": "LLM_MODEL",
    "LLM_FALLBACK_MODEL": "LLM_FALLBACK_MODEL",
    "LLM_VERIFIER_MODEL": "LLM_VERIFIER_MODEL",
    "LLM_VERIFIER_FALLBACK_MODEL": "LLM_VERIFIER_FALLBACK_MODEL",
    "GITLAB_ACTIVE_KEY_ID": "GITLAB_ACTIVE_KEY_ID",
    "GITLAB_ALLOWED_ORIGINS": "GITLAB_ALLOWED_ORIGINS",
    "GITLAB_PRIVATE_ORIGINS": "GITLAB_PRIVATE_ORIGINS",
    "GITLAB_WEBHOOK_INTAKE_ENABLED": "GITLAB_WEBHOOK_INTAKE_ENABLED",
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
            "GITLAB_WEBHOOK_INTAKE_ENABLED",
        },
    ),
    "worker.env": (
        "WORKER_ENV_KEYS",
        {
            "LLM_API_KEYS",
            "LLM_BASE_URL",
            "LLM_MODEL",
            "LLM_FALLBACK_MODEL",
            "LLM_VERIFIER_MODEL",
            "LLM_VERIFIER_FALLBACK_MODEL",
            "PORTAL_URL",
        },
    ),
    "webhook-worker.env": ("WEBHOOK_WORKER_ENV_KEYS", {"GITHUB_APP_BOT_LOGIN"}),
    "gitlab.env": (
        "GITLAB_ENV_KEYS",
        {
            "GITLAB_CREDENTIAL_KEYS",
            "GITLAB_ACTIVE_KEY_ID",
            "GITLAB_ALLOWED_ORIGINS",
            "GITLAB_PRIVATE_ORIGINS",
        },
    ),
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
    "PORTAL_URL": "https://staging-ui.example.test",
    "LLM_MODEL": "gpt-4.1-mini",
    "LLM_FALLBACK_MODEL": "mistral-small-3.2-24b",
    "LLM_VERIFIER_MODEL": "verifier-model",
    "LLM_VERIFIER_FALLBACK_MODEL": "verifier-fallback-model",
    "GITLAB_CREDENTIAL_KEYS": "",
    "GITLAB_ACTIVE_KEY_ID": "",
    "GITLAB_ALLOWED_ORIGINS": "",
    "GITLAB_PRIVATE_ORIGINS": "",
    "GITLAB_WEBHOOK_INTAKE_ENABLED": "",
}

FAKE_DOCKER = """\
#!/usr/bin/env bash
# Fake docker CLI: logs every call and answers only what deploy.sh and rollback.sh ask.
if [[ "$1" == run && " $* " == *" --env-file "* ]]; then
  logged=()
  for argument in "$@"; do
    logged+=("${argument}")
    [[ "${argument}" != -c ]] || break
  done
  printf '%s <validator>\\n' "${logged[*]}" >> "${STUB_LOG}"
else
  printf '%s\\n' "$*" >> "${STUB_LOG}"
fi
image_identity() {
  case "$1" in
    "${STUB_SELECTED_REF:-}")
      if [[ -e "${STUB_TAG_MOVED}" ]]; then echo "${STUB_ID_B}"; else echo "${STUB_ID_A}"; fi ;;
    "${STUB_REF_A}") echo "${STUB_ID_A}" ;;
    "${STUB_REF_B}") echo "${STUB_ID_B}" ;;
    "${STUB_REF_EXPLICIT}") echo "${STUB_ID_EXPLICIT}" ;;
    sha256:*) echo "$1" ;;
    *) echo "${STUB_ID_A}" ;;
  esac
}
case "$1" in
  volume) [[ "$2" == inspect && -e "${STUB_VOLUMES}/$3" ]] ;;
  inspect) exit 1 ;;
  image)
    if [[ "${STUB_INSPECT_FAILURE:-}" == id && "$4" == '{{.Id}}' ]]; then
      echo "${STUB_UNSAFE_ERROR}" >&2; exit 1
    fi
    if [[ "$4" == '{{.Id}}' ]]; then
      if [[ -n "${STUB_ID_OUTPUT+x}" ]]; then printf '%s\\n' "$STUB_ID_OUTPUT";
      elif [[ -n "${STUB_DIGEST_MISMATCH:-}" && "$5" == "${STUB_REF_A}" ]]; then
        echo "${STUB_ID_B}";
      else image_identity "$5"; fi
    else
      printf '%s\\n' "${STUB_REPODIGESTS-${STUB_REF_A}}"
      exit "${STUB_REPODIGEST_STATUS:-0}"
    fi
    ;;
  login)
    # Drain the piped token first: the caller runs under pipefail.
    if [[ -n "${STUB_REGISTRY_HELPER:-}" ]]; then
      TMPDIR="$(dirname "${STUB_LOG}")" \\
        uv run --project "${STUB_REPO_ROOT}" python "${STUB_REGISTRY_HELPER}" login || exit 1
    else
      cat >/dev/null
    fi
    if [[ -n "${STUB_LOGIN_FAILURE:-}" ]]; then
      echo "${STUB_LOGIN_FAILURE}" >&2
      exit 1
    fi
    ;;
  pull)
    if [[ -n "${STUB_REGISTRY_HELPER:-}" ]]; then
      TMPDIR="$(dirname "${STUB_LOG}")" \\
        uv run --project "${STUB_REPO_ROOT}" python "${STUB_REGISTRY_HELPER}" pull || exit 1
    fi
    if [[ "$2" == "${STUB_PULL_FAILURE:-}" ]]; then
      echo "${STUB_UNSAFE_ERROR:-raw-registry-secret}" >&2
      exit 1
    fi
    ;;
  run)
    if [[ " $* " == *" --env-file "* ]]; then
      TMPDIR="$(dirname "${STUB_LOG}")" PYTHONPATH="${STUB_REPO_ROOT}" \
        uv run --project "${STUB_REPO_ROOT}" python "${STUB_GITLAB_HELPER}" "$@"
    fi
    ;;
  compose)
    shift
    project_dir=""
    env_file=""
    action=""
    action_args=()
    while (( $# )); do
      case "$1" in
        -p) shift 2 ;;
        -f) [[ -n "${project_dir}" ]] || project_dir="$(dirname "$2")"; shift 2 ;;
        --env-file) env_file="$2"; shift 2 ;;
        *)
          if [[ -z "${action}" ]]; then action="$1"; else action_args+=("$1"); fi
          shift ;;
      esac
    done
    case "${action}" in
      run)
        cat > "${STUB_PROBE_SOURCE}"
        printf '%s\\n' "${IMAGE:-}" > "${STUB_PROBE_IMAGE}"
        cp "${APP_DIR}/.env" "${STUB_PROBE_BEFORE}"
        if [[ -f "${APP_DIR}/worker.env" ]]; then
          echo present > "${STUB_PROBE_ROLE}"
        else
          echo absent > "${STUB_PROBE_ROLE}"
        fi
        cp "${project_dir}/probe.yml" "${STUB_PROBE_CONFIG}"
        [[ "${STUB_MOVE_TAG:-}" != true ]] || touch "${STUB_TAG_MOVED}"
        printf '%s\\n' "${STUB_PROBE_STDERR:-}" >&2
        echo "${STUB_PROBE_OUTPUT-rollback revision check: compatible}"
        exit "${STUB_PROBE_STATUS:-0}"
        ;;
      up)
        printf '%s\\n' "${IMAGE:-}" > "${STUB_UP_IMAGE}"
        # Like Compose: env_file paths resolve against the directory of the first compose file.
        # Every file must exist: deploy and rollback keep each role's file in place.
        for name in app.env api.env worker.env webhook-worker.env gitlab.env; do
          [[ -f "${project_dir}/${name}" ]] || { echo "env file ${name} not found" >&2; exit 1; }
        done
        if grep -qxF "IMAGE=${STUB_FAILING_IMAGE:-}" "${env_file}"; then
          if [[ -n "${STUB_ROLLBACK_ENTRY:-}" ]]; then
            mkdir -p "${STUB_ROLLBACK_ENTRY}"
            cp -p "${APP_DIR}/"* "${APP_DIR}/.env" "${APP_DIR}/.deploy-state"* \\
              "${STUB_ROLLBACK_ENTRY}/"
          fi
          exit 1
        fi
        ;;
      exec)
        service="${action_args[1]}"
        cp "${APP_DIR}/.deploy-state" "${STUB_DIAGNOSTIC_STATE}"
        printf '%s\\n' "${service}" >> "${STUB_DIAGNOSTIC_CALLS}"
        if [[ "${service}" == "${STUB_EXEC_FAILURE_SERVICE:-}" ]]; then
          printf '%s\\n' "${STUB_EXEC_ERROR-raw-container-secret}"
          exit "${STUB_EXEC_STATUS:-1}"
        fi
        printf '%s\\n' "${STUB_EXEC_STDERR:-}" >&2
        uv run python "${STUB_EXEC_HELPER}" "${action_args[@]}"
        ;;
      down) exit "${STUB_DOWN_STATUS:-0}" ;;
    esac
    ;;
esac
"""

EXEC_HELPER = """\
from __future__ import annotations
import json
import os
import sys
from pathlib import Path
environments = json.loads(Path(os.environ['STUB_DIAGNOSTIC_ENV']).read_text())
service = sys.argv[2]
# macOS adds a CoreFoundation key at execve startup; set the fake container environment now.
os.environ.clear()
os.environ.update(environments[service])
assert sys.argv[3:5] == ['python', '-c']
exec(compile(sys.argv[5], '<docker-exec>', 'exec'))
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


def _steps(workflow_file: Path) -> list[tuple[str, str]]:
    """Every step of a workflow as (name, text), comment lines left out."""
    chunks = workflow_file.read_text(encoding="utf-8").split("\n      - name: ")[1:]
    steps = []
    for chunk in chunks:
        name, _, body = chunk.partition("\n")
        lines = [line for line in body.splitlines() if not line.lstrip().startswith("#")]
        steps.append((name, "\n".join(lines)))
    return steps


def _run_script(step: str) -> str:
    """The ``run: |`` block of a workflow step, dedented."""
    match = re.search(r"^        run: \|\n((?:          .*\n|\n)*)", step, re.MULTILINE)
    assert match is not None, "the step has no run block"
    return textwrap.dedent(match.group(1)).strip("\n")


def _bash_array(script: str, name: str) -> list[str]:
    match = re.search(rf"^\s*{name}=\(([^)]*)\)", script, re.MULTILINE)
    assert match is not None, f"array {name} is missing"
    return match.group(1).split()


def _keyring(
    *, key_id: str = "current", material: bytes = b"k" * 32, indent: int | None = None
) -> str:
    return json.dumps({key_id: base64.b64encode(material).decode()}, indent=indent)


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
            "STUB_PROBE_SOURCE": str(self.log.parent / "probe-source.py"),
            "STUB_PROBE_IMAGE": str(self.log.parent / "probe-image"),
            "STUB_PROBE_CONFIG": str(self.log.parent / "probe-config.yml"),
            "STUB_PROBE_BEFORE": str(self.log.parent / "probe-dotenv"),
            "STUB_PROBE_ROLE": str(self.log.parent / "probe-role"),
            "STUB_UP_IMAGE": str(self.log.parent / "up-image"),
            "STUB_TAG_MOVED": str(self.log.parent / "tag-moved"),
            "STUB_REF_A": IMAGE_A,
            "STUB_REF_B": IMAGE_B,
            "STUB_REF_EXPLICIT": IMAGE_EXPLICIT,
            "STUB_ID_A": A_ID,
            "STUB_ID_B": B_ID,
            "STUB_ID_EXPLICIT": EXPLICIT_ID,
            "STUB_EXEC_HELPER": str(self.log.parent / "exec-helper.py"),
            "STUB_GITLAB_HELPER": str(self.log.parent / "gitlab-helper.py"),
            "STUB_GITLAB_LOG": str(self.log.parent / "gitlab-probes.jsonl"),
            "STUB_REPO_ROOT": str(REPO_ROOT),
            "STUB_DIAGNOSTIC_ENV": str(self.log.parent / "container-environments.json"),
            "STUB_DIAGNOSTIC_STATE": str(self.log.parent / "diagnostic-before-state"),
            "STUB_DIAGNOSTIC_CALLS": str(self.log.parent / "diagnostic-services"),
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
    shutil.copy(
        REPO_ROOT / "deploy" / "compose" / "staging.ports.yml", app_dir / "compose.ports.yml"
    )
    for script in ("deploy.sh", "rollback.sh", "env-file.sh", "check-rollback-revision.py"):
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
    (tmp_path / "exec-helper.py").write_text(EXEC_HELPER)
    (tmp_path / "gitlab-helper.py").write_text(GITLAB_PROBE_HELPER)
    (tmp_path / "container-environments.json").write_text(
        json.dumps(
            {service: {"LC_CTYPE": "UTF-8"} for service in ("api", "worker", "webhook-worker")}
        )
    )
    return Host(
        app_dir=app_dir, bin_dir=bin_dir, volumes=volumes, log=tmp_path / "docker.log", tmp=tmp
    )


def _ci_auto_rollback(
    host: Host,
    deploy_run: str,
    *,
    deploy_mode: str = "edge",
    stubs: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """The CI "Rollback on failed deploy or health check" step of the run `deploy_run`.

    Like ssh-action, only the names in the step's ``envs:`` reach the host.
    """
    step = _workflow_step("Rollback on failed deploy or health check")
    envs = re.search(r"^\s+envs: (\S+)$", step, re.MULTILINE)
    assert envs is not None
    assert 'ROLLBACK_MODE=auto "${APP_DIR}/rollback.sh"' in step
    values = {
        "GHCR_TOKEN": "test-ghcr-token",
        "GHCR_USER": "github",
        "APP_DIR": str(host.app_dir),
        "DEPLOY_MODE": deploy_mode,
        "EDGE_ALIAS": "api-staging",
        "DEPLOY_RUN": deploy_run,
    }
    forwarded = {name: values[name] for name in envs.group(1).split(",")}
    return host.run("rollback.sh", env={**forwarded, "ROLLBACK_MODE": "auto", **(stubs or {})})


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


@pytest.mark.parametrize("workflow", [WORKFLOW, ROLLBACK_WORKFLOW], ids=["ci", "manual-edge"])
def test_workflow_upload_delivers_host_supplied_revision_checker(workflow: Path) -> None:
    step = _workflow_step("Upload deploy files", workflow)
    source = re.search(r'^\s+source: "([^"]+)"$', step, re.MULTILINE)
    assert source is not None
    assert "deploy/scripts/rollback.sh" in source.group(1).split(",")
    assert "deploy/scripts/check-rollback-revision.py" in source.group(1).split(",")


@pytest.mark.parametrize("compatible", [False, True])
def test_manual_ports_refreshes_old_rollback_before_execution(host: Host, compatible: bool) -> None:
    assert host.deploy(IMAGE_A, bundle="").returncode == 0
    assert host.deploy(IMAGE_B).returncode == 0
    (host.app_dir / "rollback.sh").write_text("#!/usr/bin/env bash\necho old-unguarded-rollback\n")
    (host.app_dir / "rollback.sh").chmod(0o600)
    (host.app_dir / "check-rollback-revision.py").unlink()
    upload = _workflow_step("Upload rollback scripts", ROLLBACK_WORKFLOW)
    assert "        if: steps.target.outputs.deploy_mode == 'ports'" in upload.splitlines()
    source = re.search(r'^\s+source: "([^"]+)"$', upload, re.MULTILINE)
    assert source is not None
    assert set(source.group(1).split(",")) == {
        "deploy/scripts/rollback.sh",
        "deploy/scripts/env-file.sh",
        "deploy/scripts/check-rollback-revision.py",
    }
    assert "          strip_components: 2" in upload
    for relative in source.group(1).split(","):
        path = REPO_ROOT / relative
        (host.app_dir / path.name).write_bytes(path.read_bytes())
    rollback = _workflow_step("Rollback", ROLLBACK_WORKFLOW)
    script = re.search(r"^          script: \|\n((?:            .*\n|\n)*)", rollback, re.MULTILINE)
    assert script is not None
    (host.app_dir / "manual-step.sh").write_text(
        "#!/usr/bin/env bash\n" + textwrap.dedent(script.group(1))
    )
    (host.app_dir / "manual-step.sh").chmod(0o755)
    before = host.files()
    calls_before = len(host.calls())

    result = host.run(
        "manual-step.sh",
        env={
            "IMAGE": "",
            "DEPLOY_MODE": "ports",
            "STUB_PROBE_STATUS": "0" if compatible else "1",
            "STUB_PROBE_OUTPUT": (
                "rollback revision check: compatible"
                if compatible
                else "rollback revision check: refused: database revision is unknown"
            ),
        },
    )

    assert "old-unguarded-rollback" not in result.stdout
    assert any(" run --rm --no-deps " in call for call in host.calls()[calls_before:])
    if compatible:
        assert result.returncode == 0, result.stderr
        assert f"rolled back to {IMAGE_A}" in result.stdout
        assert _read_dotenv(host.app_dir / ".deploy-state")["current_image"] == IMAGE_A
    else:
        assert result.returncode != 0
        assert "database revision is unknown" in result.stderr
        assert "rolled back" not in result.stdout
        assert host.files() == before
        assert not any(" up " in call for call in host.calls()[calls_before:])


def test_manual_rollback_failure_keeps_followup_and_promotion_success_gated() -> None:
    for name in ("Rollback", "Read deployed image", "Parse deployed image", "Health check"):
        step = _workflow_step(name, ROLLBACK_WORKFLOW)
        assert "continue-on-error" not in step
        assert re.search(r"^        if:", step, re.MULTILINE) is None
    workflow = ROLLBACK_WORKFLOW.read_text()
    assert "    if: github.ref == 'refs/heads/main'" in workflow
    assert "    needs: rollback" in workflow.split("  promote-staging:", 1)[1]
    assert "format('rollback-ignored-{0}', github.run_id)" in workflow
    assert "  cancel-in-progress: false" in workflow


def _release_unchanged_literal() -> str:
    """The text the Fail step looks for in the captured stdout of the auto rollback."""
    step = _workflow_step("Fail the pipeline after rollback")
    match = re.search(
        r"^          RELEASE_UNCHANGED: "
        r"\"\$\{\{ contains\(steps\.rollback\.outputs\.stdout, '([^']+)'\) \}\}\"$",
        step,
        re.MULTILINE,
    )
    assert match is not None, "the Fail step has no RELEASE_UNCHANGED expression"
    return match.group(1)


@pytest.mark.parametrize("unchanged", [False, True], ids=["rolled-back", "release-unchanged"])
@pytest.mark.parametrize(
    ("deploy", "health", "rollback"),
    [
        ("failure", "skipped", "failure"),
        ("success", "failure", "failure"),
        ("failure", "skipped", "success"),
        ("success", "failure", "success"),
    ],
)
def test_failed_forward_deploy_never_promotes_and_reports_failed_auto_rollback(
    deploy: str, health: str, rollback: str, unchanged: bool, tmp_path: Path
) -> None:
    values = {"deploy": deploy, "health": health, "rollback": rollback}

    def render(script: str) -> str:
        for step, outcome in values.items():
            script = script.replace("${{ steps." + step + ".outcome }}", outcome)
        return script

    output = tmp_path / "github-output"
    environment = {"PATH": os.environ["PATH"], "GITHUB_OUTPUT": str(output)}
    promote = subprocess.run(
        [
            "bash",
            "--noprofile",
            "--norc",
            "-eo",
            "pipefail",
            "-c",
            render(_run_script(_workflow_step("Set promote flag"))),
        ],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert promote.returncode == 0
    assert output.read_text() == "promote=false\n"
    # Stdout captured by the Rollback step: rollback.sh's answer when deploy.sh of this run
    # stopped before the release switch, or the line of a real rollback.
    stdout = STOPPED_BEFORE_SWITCH.format("101-1") if unchanged else f"rolled back to {IMAGE_A}\n"
    # GitHub renders the env expression to "true" / "false" (contains() ignores case), so the
    # raw stdout never enters the script.
    release_unchanged = str(_release_unchanged_literal().lower() in stdout.lower()).lower()
    fail_script = _run_script(_workflow_step("Fail the pipeline after rollback"))
    assert "outputs.stdout" not in fail_script
    failed = subprocess.run(
        [
            "bash",
            "--noprofile",
            "--norc",
            "-eo",
            "pipefail",
            "-c",
            render(fail_script),
        ],
        env={**environment, "RELEASE_UNCHANGED": release_unchanged},
        capture_output=True,
        text=True,
        check=False,
    )
    assert failed.returncode == 1
    assert ("rollback failed" in failed.stderr) is (rollback == "failure")
    if deploy == "failure" and rollback == "success" and unchanged:
        message = (
            "staging deploy failed before the release switch; "
            "the running release was left unchanged"
        )
    elif deploy == "failure":
        message = "staging deploy failed; rollback was triggered"
    else:
        message = "staging health check failed; rollback was triggered"
    assert failed.stdout == message + "\n"
    auto = _workflow_step("Rollback on failed deploy or health check")
    assert "ROLLBACK_MODE=auto" in auto
    assert "\n          capture_stdout: true\n" in auto
    assert (
        "if: always() && (steps.deploy.outcome == 'failure' || steps.health.outcome == 'failure')"
        in auto
    )
    promotion = WORKFLOW.read_text().split("  promote-staging:", 1)[1]
    assert "needs.deploy-staging.outputs.promote == 'true'" in promotion


def test_fail_step_looks_for_the_release_unchanged_line_of_rollback_sh() -> None:
    # The Fail step tells an unchanged release apart by this text; rollback.sh must print it.
    literal = _release_unchanged_literal()
    echoes = [
        line
        for line in _read("deploy", "scripts", "rollback.sh").splitlines()
        if line.lstrip().startswith("echo ") and literal in line
    ]
    assert len(echoes) == 1, echoes
    assert "skipping auto rollback" in echoes[0]
    assert literal in STOPPED_BEFORE_SWITCH


def test_deploy_and_auto_rollback_steps_share_the_deploy_run() -> None:
    # The auto rollback undoes a deploy only once deploy.sh of the same run reached the switch.
    run_id = "          DEPLOY_RUN: ${{ github.run_id }}-${{ github.run_attempt }}\n"
    for name in ("Deploy image", "Rollback on failed deploy or health check"):
        step = _workflow_step(name)
        envs = re.search(r"^\s+envs: (\S+)$", step, re.MULTILINE)
        assert run_id in step, name
        assert envs is not None
        assert "DEPLOY_RUN" in envs.group(1).split(","), name


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
    for name in ("api.env", "worker.env", "webhook-worker.env"):
        assert _read_env_file(host.app_dir / name) == {}


# --- Host: generated credentials, rollbacks and refusals ----------------------------------------


def test_first_deploy_generates_store_passwords_once(host: Host) -> None:
    assert host.deploy(IMAGE_A, bundle="").returncode == 0
    first = _read_dotenv(host.env_file)
    assert host.deploy(IMAGE_B, bundle="").returncode == 0
    second = _read_dotenv(host.env_file)

    for name in ("POSTGRES_PASSWORD", "RABBITMQ_PASSWORD", "REDIS_PASSWORD"):
        assert re.fullmatch(r"[0-9a-f]{48}", first[name]), name
        assert second[name] == first[name], name
    assert first["RABBITMQ_USER"] == "app"
    assert second["IMAGE"] == IMAGE_B
    assert list(host.tmp.iterdir()) == []  # the per-run DOCKER_CONFIG is removed


def test_rollbacks_keep_store_passwords_and_app_secrets(host: Host) -> None:
    bundle = _bundle(CI_SECRETS)
    assert host.deploy(IMAGE_A, bundle=bundle).returncode == 0
    assert host.deploy(IMAGE_B, bundle=bundle).returncode == 0
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
        IMAGE_BAD,
        bundle=_bundle(rotated),
        env={"STUB_FAILING_IMAGE": IMAGE_BAD},
    )
    after_auto = _read_dotenv(host.env_file)

    assert manual.returncode == 0, manual.stderr
    assert after_manual["IMAGE"] == IMAGE_A
    assert failed.returncode != 0
    assert f"rolled back to {IMAGE_A}" in failed.stdout
    assert after_auto["IMAGE"] == IMAGE_A
    for name in ("POSTGRES_PASSWORD", "RABBITMQ_USER", "RABBITMQ_PASSWORD", "REDIS_PASSWORD"):
        assert after_manual[name] == deployed[name], name
        assert after_auto[name] == deployed[name], name
    assert all(secrets[name] for name in ENV_FILES if name != "gitlab.env")
    assert secrets_after_manual == secrets
    for file_name, (_, keys) in ENV_FILES.items():
        assert _read_env_file(host.app_dir / file_name) == {
            name: rotated[name] for name in keys if rotated.get(name)
        }, file_name


@pytest.mark.parametrize("name", ["POSTGRES_PASSWORD", "RABBITMQ_PASSWORD", "REDIS_PASSWORD"])
def test_rollback_refuses_without_a_store_password(host: Host, name: str) -> None:
    assert host.deploy(IMAGE_A, bundle="").returncode == 0
    assert host.deploy(IMAGE_B, bundle="").returncode == 0
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
    assert host.deploy(IMAGE_A, bundle="").returncode == 0
    assert host.deploy(IMAGE_B, bundle="").returncode == 0
    for name in ENV_FILES:
        (host.app_dir / name).unlink()

    result = host.run("rollback.sh")

    assert result.returncode == 0, result.stderr
    for name in ENV_FILES:
        assert _mode(host.app_dir / name) == 0o600


@pytest.mark.parametrize("mode", ["manual", "auto"])
@pytest.mark.parametrize("explicit", [False, True])
def test_moved_tag_cannot_change_admitted_runtime_or_promotion_identity(
    host: Host, mode: str, explicit: bool
) -> None:
    selected = "ghcr.io/test/api:requested" if explicit else "ghcr.io/test/api:previous"
    assert host.deploy(selected, bundle="").returncode == 0
    previous = (host.app_dir / ".deploy-state").read_bytes()
    assert host.deploy(IMAGE_B).returncode == 0
    current = (host.app_dir / ".deploy-state").read_bytes()
    calls_before = len(host.calls())

    result = host.run(
        "rollback.sh",
        *([selected] if explicit else []),
        env={
            "ROLLBACK_MODE": mode,
            "STUB_SELECTED_REF": selected,
            "STUB_MOVE_TAG": "true",
            "STUB_REPODIGESTS": "ghcr.io/unrelated/api@sha256:" + "d" * 64 + "\n" + IMAGE_A,
        },
    )

    assert result.returncode == 0, result.stderr
    assert (host.log.parent / "tag-moved").exists()
    assert (host.log.parent / "probe-image").read_text().strip() == A_ID
    assert (host.log.parent / "up-image").read_text().strip() == A_ID
    assert _read_dotenv(host.env_file)["IMAGE"] == IMAGE_A
    assert _read_dotenv(host.app_dir / ".deploy-state")["current_image"] == IMAGE_A
    assert (host.app_dir / ".deploy-state.previous").read_bytes() == (
        current if mode == "manual" else previous
    )
    calls = host.calls()[calls_before:]
    assert calls.count(f"pull {selected if explicit else IMAGE_A}") == 1
    assert any(" up --pull missing " in call for call in calls)
    for role in ("api", "worker", "webhook-worker", "bootstrap"):
        assert "    image: ${IMAGE:?IMAGE is required}" in _service_block(role)
    if mode == "manual":
        promotion = _run_script(
            _workflow_step("Promote rolled-back image to staging", ROLLBACK_WORKFLOW)
        )
        promotion = promotion.replace("${{ secrets.GITHUB_TOKEN }}", "synthetic-token")
        promotion = promotion.replace("${{ github.actor }}", "synthetic-actor")
        step = host.app_dir / "promote-step.sh"
        step.write_text("#!/usr/bin/env bash\nset -euo pipefail\n" + promotion)
        step.chmod(0o755)
        promotion_before = len(host.calls())
        promoted = host.run(
            "promote-step.sh",
            env={
                "DEPLOYED_IMAGE": _read_dotenv(host.app_dir / ".deploy-state")["current_image"],
                "STAGING_TAG": "ghcr.io/test/api:staging",
                "PREVIOUS_TAG": "ghcr.io/test/api:staging-previous",
                "REPOSITORY": "test/api",
                "STUB_SELECTED_REF": selected,
            },
        )
        assert promoted.returncode == 0, promoted.stderr
        promotion_calls = host.calls()[promotion_before:]
        assert f"pull {IMAGE_A}" in promotion_calls
        assert f"tag {IMAGE_A} ghcr.io/test/api:staging" in promotion_calls
        assert f"pull {selected}" not in promotion_calls


@pytest.mark.parametrize("mode", ["manual", "auto"])
@pytest.mark.parametrize(
    ("status", "verdict", "reason"),
    [
        (0, "rollback revision check: compatible", None),
        (
            1,
            "rollback revision check: refused: database revision is unknown",
            "database revision is unknown",
        ),
        (1, "rollback revision check: compatible", "target revision probe failed"),
        (0, "rollback revision check: refused: database revision is unknown", "unexpected result"),
        (
            1,
            "rollback revision check: refused: database revision is unknown\nSECRET-CANARY",
            "target revision probe failed",
        ),
        (0, "rollback revision check: compatible\n", "unexpected result"),
        (0, "", "unexpected result"),
    ],
)
def test_probe_stdout_protocol_ignores_secret_bearing_compose_stderr(
    host: Host, mode: str, status: int, verdict: str, reason: str | None
) -> None:
    assert host.deploy(IMAGE_A, bundle="").returncode == 0
    assert host.deploy(IMAGE_B).returncode == 0
    before = host.files()
    modes = {p.name: _mode(p) for p in host.app_dir.iterdir()}
    calls_before = len(host.calls())
    result = host.run(
        "rollback.sh",
        env={
            "ROLLBACK_MODE": mode,
            "STUB_PROBE_STATUS": str(status),
            "STUB_PROBE_OUTPUT": verdict,
            "STUB_PROBE_STDERR": (
                "Container project-bootstrap-run Creating\nSECRET-CANARY\nPEM-CANARY"
            ),
        },
    )
    output = result.stdout + result.stderr
    assert "SECRET-CANARY" not in output and "PEM-CANARY" not in output
    assert "Creating" not in output
    if reason is None:
        assert result.returncode == 0, result.stderr
        assert f"rolled back to {IMAGE_A}" in result.stdout
    else:
        assert result.returncode != 0
        assert reason in result.stderr
        assert host.files() == before
        assert {p.name: _mode(p) for p in host.app_dir.iterdir()} == modes
        assert not any(
            set(c.split()) & {"up", "rm", "down", "exec"} for c in host.calls()[calls_before:]
        )
    assert list(host.tmp.iterdir()) == []


@pytest.mark.parametrize("mode", ["manual", "auto"])
@pytest.mark.parametrize("explicit", [False, True])
@pytest.mark.parametrize("failure", ["pull", "probe", "noisy-probe", "false-success"])
def test_rollback_refuses_before_any_persistent_or_stack_change(
    host: Host, mode: str, explicit: bool, failure: str
) -> None:
    assert host.deploy(IMAGE_A, bundle=_bundle(CI_SECRETS)).returncode == 0
    assert host.deploy(IMAGE_B).returncode == 0
    # Missing role files and unusual permissions must survive a refused probe too.
    (host.app_dir / "worker.env").unlink()
    (host.app_dir / "api.env").chmod(0o640)
    before = host.files()
    modes = {path.name: _mode(path) for path in host.app_dir.iterdir()}
    calls_before = len(host.calls())
    target = IMAGE_EXPLICIT if explicit else IMAGE_A
    secret = "SECRET-CANARY\n-----BEGIN PRIVATE KEY-----\nPEM-CANARY"
    environment = {"ROLLBACK_MODE": mode, "STUB_UNSAFE_ERROR": secret}
    if failure == "pull":
        environment["STUB_PULL_FAILURE"] = target
    else:
        environment["STUB_PROBE_STATUS"] = "0" if failure == "false-success" else "1"
        environment["STUB_PROBE_OUTPUT"] = (
            "rollback revision check: refused: database revision is unknown"
            if failure == "probe"
            else secret
        )

    result = host.run("rollback.sh", *([target] if explicit else []), env=environment)

    assert result.returncode != 0
    assert host.files() == before
    assert {path.name: _mode(path) for path in host.app_dir.iterdir()} == modes
    calls = host.calls()[calls_before:]
    assert f"pull {target}" in calls
    assert not any(set(call.split()) & {"up", "rm", "down", "exec"} for call in calls)
    assert calls[-1] == "logout ghcr.io"
    assert list(host.tmp.iterdir()) == []
    output = result.stdout + result.stderr
    assert "rolled back" not in output
    assert "Select a compatible image" in output
    assert "SECRET-CANARY" not in output and "PEM-CANARY" not in output
    if failure == "probe":
        assert "database revision is unknown" in output


@pytest.mark.parametrize("mode", ["manual", "auto"])
@pytest.mark.parametrize(
    "failure",
    [
        "inspect",
        "short-id",
        "uppercase-id",
        "noisy-id",
        "multiple-ids",
        "extra-line",
        "empty-digests",
        "wrong-repository",
        "malformed-digests",
        "tagged-digest",
        "ambiguous-digests",
        "digest-error",
        "digest-mismatch",
        "local-id-mismatch",
    ],
)
def test_unusable_immutable_identity_refuses_without_file_or_stack_mutation(
    host: Host, mode: str, failure: str
) -> None:
    assert host.deploy(IMAGE_A, bundle="").returncode == 0
    assert host.deploy(IMAGE_B).returncode == 0
    (host.app_dir / "worker.env").unlink()
    (host.app_dir / "api.env").chmod(0o640)
    before = host.files()
    modes = {p.name: _mode(p) for p in host.app_dir.iterdir()}
    calls_before = len(host.calls())
    secret = "IDENTITY-SECRET-CANARY\nPEM-BODY-CANARY"
    selected = A_ID if failure == "local-id-mismatch" else "ghcr.io/test/api:movable"
    options = {
        "inspect": {"STUB_INSPECT_FAILURE": "id"},
        "short-id": {"STUB_ID_OUTPUT": "sha256:abc"},
        "uppercase-id": {"STUB_ID_OUTPUT": "sha256:" + "A" * 64},
        "noisy-id": {"STUB_ID_OUTPUT": secret + "\n" + A_ID},
        "multiple-ids": {"STUB_ID_OUTPUT": A_ID + "\n" + B_ID},
        "extra-line": {"STUB_ID_OUTPUT": A_ID + "\n"},
        "empty-digests": {"STUB_REPODIGESTS": ""},
        "wrong-repository": {"STUB_REPODIGESTS": "ghcr.io/unrelated/api@sha256:" + "a" * 64},
        "malformed-digests": {"STUB_REPODIGESTS": IMAGE_A + "\n" + secret},
        "tagged-digest": {"STUB_REPODIGESTS": "ghcr.io/test/api:tag@sha256:" + "a" * 64},
        "ambiguous-digests": {
            "STUB_REPODIGESTS": IMAGE_A + "\nghcr.io/test/api@sha256:" + "9" * 64
        },
        "digest-error": {"STUB_REPODIGEST_STATUS": "1", "STUB_REPODIGESTS": secret},
        "digest-mismatch": {"STUB_DIGEST_MISMATCH": "true"},
        "local-id-mismatch": {"STUB_ID_OUTPUT": B_ID},
    }[failure]
    result = host.run(
        "rollback.sh",
        selected,
        env={
            "ROLLBACK_MODE": mode,
            "STUB_SELECTED_REF": selected,
            "STUB_UNSAFE_ERROR": secret,
            **options,
        },
    )
    assert result.returncode != 0
    assert "rollback refused: target " in result.stderr
    assert host.files() == before
    assert {p.name: _mode(p) for p in host.app_dir.iterdir()} == modes
    calls = host.calls()[calls_before:]
    assert not any(" run " in call or " up " in call or " exec " in call for call in calls)
    assert calls[-1] == "logout ghcr.io" and list(host.tmp.iterdir()) == []
    assert "rolled back" not in result.stdout
    assert "IDENTITY-SECRET-CANARY" not in result.stdout + result.stderr
    assert "PEM-BODY-CANARY" not in result.stdout + result.stderr


@pytest.mark.parametrize(
    ("selected", "release"),
    [
        (IMAGE_A, IMAGE_A),
        (A_ID, A_ID),
        ("ghcr.io/test/api:tag@sha256:" + "a" * 64, "ghcr.io/test/api:tag@sha256:" + "a" * 64),
        ("ghcr.io/test/api:" + "4" * 40, IMAGE_A),
        ("registry.test:5000/team/api:version", "registry.test:5000/team/api@sha256:" + "a" * 64),
    ],
)
def test_immutable_resolution_preserves_digest_refs_and_parses_registry_port(
    host: Host, selected: str, release: str
) -> None:
    assert host.deploy(IMAGE_A, bundle="").returncode == 0
    assert host.deploy(IMAGE_B).returncode == 0
    result = host.run("rollback.sh", selected, env={"STUB_REPODIGESTS": release})
    assert result.returncode == 0, result.stderr
    assert _read_dotenv(host.env_file)["IMAGE"] == release
    assert _read_dotenv(host.app_dir / ".deploy-state")["current_image"] == release
    assert (host.log.parent / "probe-image").read_text().strip() == A_ID
    assert (host.log.parent / "up-image").read_text().strip() == A_ID


@pytest.mark.parametrize("mode", ["manual", "auto"])
@pytest.mark.parametrize("explicit", [False, True])
def test_compatible_rollback_probes_target_before_changes_and_keeps_mode_state(
    host: Host, mode: str, explicit: bool
) -> None:
    assert host.deploy(IMAGE_A, bundle=_bundle(CI_SECRETS)).returncode == 0
    previous = (host.app_dir / ".deploy-state").read_bytes()
    assert host.deploy(IMAGE_B).returncode == 0
    current = (host.app_dir / ".deploy-state").read_bytes()
    before_env = host.env_file.read_bytes()
    (host.app_dir / "worker.env").unlink()
    secrets = {
        name: (host.app_dir / name).read_bytes() for name in ENV_FILES if name != "worker.env"
    }
    calls_before = len(host.calls())
    target = IMAGE_EXPLICIT if explicit else IMAGE_A

    result = host.run("rollback.sh", *([target] if explicit else []), env={"ROLLBACK_MODE": mode})

    assert result.returncode == 0, result.stderr
    calls = host.calls()[calls_before:]
    pull = calls.index(f"pull {target}")
    probe = next(i for i, call in enumerate(calls) if " run " in call)
    up = next(i for i, call in enumerate(calls) if " up " in call)
    assert pull < probe < up
    assert calls[probe].endswith("run --rm --no-deps -T bootstrap python -")
    assert (host.log.parent / "probe-source.py").read_bytes() == (
        REPO_ROOT / "deploy/scripts/check-rollback-revision.py"
    ).read_bytes()
    assert (host.log.parent / "probe-image").read_text().strip() == (
        EXPLICIT_ID if explicit else A_ID
    )
    assert (host.log.parent / "probe-dotenv").read_bytes() == before_env
    assert (host.log.parent / "probe-role").read_text() == "absent\n"
    assert (host.log.parent / "probe-config.yml").read_text() == (
        "services:\n"
        "  bootstrap:\n"
        "    image: ${IMAGE:?IMAGE is required}\n"
        "    environment:\n"
        "      DATABASE_URL: postgresql+psycopg://${POSTGRES_USER:-app}:"
        "${POSTGRES_PASSWORD:?POSTGRES_PASSWORD is required}"
        "@postgres:5432/${POSTGRES_DB:-app}\n"
        '    restart: "no"\n'
        "networks:\n"
        "  default:\n"
        "    external: true\n"
        "    name: ${COMPOSE_PROJECT:?COMPOSE_PROJECT is required}_default\n"
    )
    assert _read_dotenv(host.env_file)["IMAGE"] == target
    assert (host.app_dir / ".deploy-state.previous").read_bytes() == (
        current if mode == "manual" else previous
    )
    state = _read_dotenv(host.app_dir / ".deploy-state")
    assert state["current_image"] == target and state["rolled_back"] == "true"
    assert {name: (host.app_dir / name).read_bytes() for name in secrets} == secrets
    assert (host.app_dir / "worker.env").read_bytes() == b""
    assert _mode(host.app_dir / "worker.env") == 0o600
    assert list(host.tmp.iterdir()) == []


@pytest.mark.parametrize("mode", ["manual", "auto"])
def test_successful_rollback_prints_only_sorted_running_container_keys(
    host: Host, mode: str
) -> None:
    assert host.deploy(IMAGE_A, bundle=_bundle(CI_SECRETS)).returncode == 0
    assert host.deploy(IMAGE_B).returncode == 0
    canary = "ENV-VALUE-CANARY$HOME\\backslash"
    shared = {
        "LC_CTYPE": "UTF-8",
        "Z_KEY": canary,
        "A_KEY": "BEGIN-SECRET\nPEM-LINE-CANARY\nEND-SECRET",
        "bad\nHOSTILE-KEY-CANARY": "value",
        "bad-key": "value",
        'quote"key': "value",
        "unicodeé": "value",
    }
    (host.log.parent / "container-environments.json").write_text(
        json.dumps(
            {
                "api": {**shared, "AUTH_JWT_PRIVATE_KEY": FAKE_PEM},
                "worker": {**shared, "GITHUB_APP_PRIVATE_KEY": FAKE_PEM, "lower_key": canary},
                "webhook-worker": {**shared, "GITHUB_APP_BOT_LOGIN": canary},
            }
        )
    )
    calls_before = len(host.calls())

    result = host.run("rollback.sh", env={"ROLLBACK_MODE": mode})

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        'api environment keys: ["AUTH_JWT_PRIVATE_KEY", "A_KEY", "LC_CTYPE", "Z_KEY"]',
        'worker environment keys: ["A_KEY", "GITHUB_APP_PRIVATE_KEY", "LC_CTYPE", "Z_KEY", '
        '"lower_key"]',
        'webhook-worker environment keys: ["A_KEY", "GITHUB_APP_BOT_LOGIN", "LC_CTYPE", "Z_KEY"]',
        f"rolled back to {IMAGE_A}",
    ]
    assert result.stderr == ""
    assert (host.log.parent / "diagnostic-services").read_text() == "api\nworker\nwebhook-worker\n"
    assert _read_dotenv(host.log.parent / "diagnostic-before-state")["current_image"] == IMAGE_A
    calls = host.calls()[calls_before:]
    up = next(i for i, call in enumerate(calls) if " up " in call)
    assert all(up < i for i, call in enumerate(calls) if " exec " in call)
    assert sum(" exec -T " in call for call in calls) == 3
    for value in (canary, "PEM-LINE-CANARY", "HOSTILE-KEY-CANARY", *FAKE_PEM.splitlines()):
        assert value not in result.stdout + result.stderr


@pytest.mark.parametrize("mode", ["manual", "auto"])
@pytest.mark.parametrize("service", ["api", "worker", "webhook-worker"])
@pytest.mark.parametrize("failure", ["exec", "noise", "invalid-list", "empty"])
def test_diagnostic_failure_is_secret_safe_and_records_the_healthy_release(
    host: Host, mode: str, service: str, failure: str
) -> None:
    assert host.deploy(IMAGE_A, bundle=_bundle(CI_SECRETS)).returncode == 0
    assert host.deploy(IMAGE_B).returncode == 0
    state = (host.app_dir / ".deploy-state").read_bytes()
    previous = (host.app_dir / ".deploy-state.previous").read_bytes()
    secret = "DOCKER-SECRET-CANARY\nPEM-BODY-CANARY"
    output = {
        "exec": secret,
        "noise": secret + '\n["A_KEY"]',
        "invalid-list": '["A_KEY", "bad\\nPEM-BODY-CANARY"]',
        "empty": "",
    }[failure]

    result = host.run(
        "rollback.sh",
        env={
            "ROLLBACK_MODE": mode,
            "STUB_EXEC_FAILURE_SERVICE": service,
            "STUB_EXEC_STATUS": "1" if failure == "exec" else "0",
            "STUB_EXEC_ERROR": output,
        },
    )

    assert result.returncode != 0
    assert _read_dotenv(host.app_dir / ".deploy-state")["current_image"] == IMAGE_A
    assert _read_dotenv(host.app_dir / ".deploy-state")["rolled_back"] == "true"
    assert (host.app_dir / ".deploy-state.previous").read_bytes() == (
        state if mode == "manual" else previous
    )
    assert (host.log.parent / "diagnostic-before-state").read_bytes() == (
        host.app_dir / ".deploy-state"
    ).read_bytes()
    assert "rolled back" not in result.stdout + result.stderr
    assert f"{service} environment key diagnostics failed" in result.stderr
    assert "rollback completion failed:" in result.stderr
    assert "healthy target release is recorded" in result.stderr
    assert "Inspect service health" in result.stderr
    assert "rollback refused:" not in result.stderr
    for canary in ("DOCKER-SECRET-CANARY", "PEM-BODY-CANARY"):
        assert canary not in result.stdout + result.stderr
    assert list(host.tmp.iterdir()) == []


@pytest.mark.parametrize("mode", ["manual", "auto"])
def test_failed_stack_start_does_not_run_diagnostics_or_record_success(
    host: Host, mode: str
) -> None:
    assert host.deploy(IMAGE_A, bundle="").returncode == 0
    assert host.deploy(IMAGE_B).returncode == 0
    state = (host.app_dir / ".deploy-state").read_bytes()
    previous = (host.app_dir / ".deploy-state.previous").read_bytes()
    calls_before = len(host.calls())

    result = host.run(
        "rollback.sh",
        env={"ROLLBACK_MODE": mode, "STUB_FAILING_IMAGE": IMAGE_A},
    )

    assert result.returncode != 0
    assert not any(" exec " in call for call in host.calls()[calls_before:])
    assert "environment keys" not in result.stdout
    assert "rolled back" not in result.stdout
    assert (host.app_dir / ".deploy-state").read_bytes() == state
    assert (host.app_dir / ".deploy-state.previous").read_bytes() == previous


def test_host_deployed_before_the_broker_keeps_its_postgres_password(host: Host) -> None:
    postgres_password = "0" * 48
    host.env_file.write_text(
        "IMAGE=ghcr.io/test/api@sha256:old\nPOSTGRES_USER=app\n"
        f"POSTGRES_PASSWORD={postgres_password}\nPOSTGRES_DB=app\n"
        "DEPLOY_MODE=edge\nEDGE_ALIAS=api-staging\n",
        encoding="utf-8",
    )
    host.add_volume("postgres-data")

    result = host.run("deploy.sh", IMAGE_A)

    assert result.returncode == 0, result.stderr
    env = _read_dotenv(host.env_file)
    assert env["POSTGRES_PASSWORD"] == postgres_password
    assert re.fullmatch(r"[0-9a-f]{48}", env["RABBITMQ_PASSWORD"])
    assert re.fullmatch(r"[0-9a-f]{48}", env["REDIS_PASSWORD"])
    for name in ENV_FILES:
        assert (host.app_dir / name).read_text(encoding="utf-8") == ""
        assert _mode(host.app_dir / name) == 0o600


def test_broker_volume_without_a_password_is_never_given_a_new_one(host: Host) -> None:
    assert host.deploy(IMAGE_A, bundle="").returncode == 0
    env = host.env_file.read_text(encoding="utf-8")
    host.env_file.write_text(
        "".join(line for line in env.splitlines(True) if not line.startswith("RABBITMQ_PASSWORD=")),
        encoding="utf-8",
    )
    host.add_volume("rabbitmq-data")
    before = host.files()

    result = host.deploy(IMAGE_B, bundle="")

    assert result.returncode != 0
    assert "refusing to generate a new password" in result.stderr
    assert host.files() == before


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


@pytest.mark.parametrize("mode", ["manual", "auto"])
def test_rollback_without_a_release_stops_the_stack_by_project_name(host: Host, mode: str) -> None:
    # A host whose .env predates the broker: loading compose.yml would fail on RABBITMQ_PASSWORD.
    host.env_file.write_text("DEPLOY_MODE=edge\nEDGE_ALIAS=api-staging\n", encoding="utf-8")

    result = host.run("rollback.sh", env={"ROLLBACK_MODE": mode})

    assert result.returncode == 0, result.stderr
    calls = host.calls()
    down = calls.index(f"compose -p {PROJECT} down --remove-orphans")
    started = next(i for i, call in enumerate(calls) if call.startswith("run -d --name"))
    assert down < started
    assert "--network-alias api-staging" in calls[started]
    assert not any(
        " exec " in call or " run --rm " in call for call in calls if call.startswith("compose ")
    )
    assert "environment keys" not in result.stdout


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
        (
            "worker",
            "app.worker",
            ["app.env", "gitlab.env", "worker.env"],
            ["bootstrap", "postgres", "rabbitmq"],
        ),
        # The projection creates Runs and publishes them to the review queue (#52).
        (
            "webhook-worker",
            "app.webhook_worker",
            ["app.env", "gitlab.env", "webhook-worker.env"],
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
    # Fail closed: a red validation must fail the job and with it push-image.
    assert "continue-on-error" not in job
    assert "|| true" not in script
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


def test_rollback_validates_the_edge_caddyfile_before_it_reaches_the_host() -> None:
    rollback = ROLLBACK_WORKFLOW.read_text(encoding="utf-8")
    step = _workflow_step("caddy validate", ROLLBACK_WORKFLOW)
    order = [
        rollback.index(f"      - name: {name}\n")
        for name in ("Checkout", "caddy validate", "Upload deploy files", "Upload edge proxy files")
    ]

    # Rollback uploads the Caddyfile of its own checkout and reloads Caddy with it: the CI check
    # runs on that revision before the first step that reaches the host, as in the edge mode only.
    assert _run_script(step) == _run_script(_workflow_step("caddy validate"))
    assert "continue-on-error" not in step
    assert "|| true" not in _run_script(step)
    assert order == sorted(order)
    assert not re.search(r"\bcaddy(?::\d|@sha256:)", rollback)
    # The check runs exactly when the Caddyfile reaches the host: every step that uploads the edge
    # files or reloads Caddy carries the same guard as the check.
    edge_steps = [
        (name, text)
        for name, text in _steps(ROLLBACK_WORKFLOW)
        if "deploy/edge" in text or "caddy reload" in text
    ]
    assert {"caddy validate", "Upload edge proxy files", "Prepare host"} <= {
        name for name, _ in edge_steps
    }
    for name, text in edge_steps:
        assert EDGE_GUARD in text.splitlines(), f"step {name!r} lacks the edge guard"


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


def test_failed_deploy_then_refused_auto_rollback_preserves_rollback_entry(host: Host) -> None:
    assert host.deploy(IMAGE_A, bundle=_bundle(CI_SECRETS)).returncode == 0
    assert host.deploy(IMAGE_B).returncode == 0
    entry = host.log.parent / "rollback-entry"
    calls_before = len(host.calls())
    result = host.deploy(
        IMAGE_BAD,
        env={
            "STUB_FAILING_IMAGE": IMAGE_BAD,
            "STUB_ROLLBACK_ENTRY": str(entry),
            "STUB_PROBE_STATUS": "1",
            "STUB_PROBE_OUTPUT": "rollback revision check: refused: database revision is unknown",
            "STUB_PROBE_STDERR": "Container Creating\nSECRET-CANARY",
        },
    )
    assert result.returncode != 0
    assert "compose up failed; rolling back" in result.stderr
    assert "rollback refused:" in result.stderr and "database revision is unknown" in result.stderr
    assert "SECRET-CANARY" not in result.stdout + result.stderr
    assert "rolled back" not in result.stdout and "environment keys:" not in result.stdout
    assert host.files() == {p.name: p.read_bytes() for p in entry.iterdir()}
    assert {p.name: _mode(p) for p in host.app_dir.iterdir()} == {
        p.name: _mode(p) for p in entry.iterdir()
    }
    calls = host.calls()[calls_before:]
    pull = calls.index(f"pull {IMAGE_B}")
    assert not any(set(c.split()) & {"up", "rm", "down", "exec"} for c in calls[pull:])
    assert list(host.tmp.iterdir()) == []


# CI runs deploy A, B and C as runs 101-1, 102-1 and 103-1 (DEPLOY_RUN); in the re-run case C is
# attempt 2 of B's run 102, whose attempt 1 crossed the switch. The DB probe stays compatible
# throughout: guard #126 never decides these cases.
@pytest.mark.parametrize(
    ("marked", "attempt"),
    [(True, "103-1"), (True, "102-2"), (False, "103-1")],
    ids=["marked-host", "re-run-attempt", "legacy-host"],
)
@pytest.mark.parametrize(
    ("deploy_mode", "bundle", "stubs", "reason"),
    [
        ("edge", _bundle({"PATH": "/tmp"}), {}, "PATH is not in the allowlist"),
        ("bogus", None, {}, "DEPLOY_MODE must be ports or edge, got: bogus"),
        (
            "edge",
            None,
            {"GHCR_TOKEN": "test-ghcr-token", "STUB_LOGIN_FAILURE": "registry login denied"},
            "registry login failed",
        ),
    ],
    ids=["invalid-bundle", "bad-deploy-mode", "registry-login"],
)
def test_ci_auto_rollback_keeps_the_release_when_deploy_stops_before_the_switch(
    host: Host,
    marked: bool,
    attempt: str,
    deploy_mode: str,
    bundle: str | None,
    stubs: dict[str, str],
    reason: str,
) -> None:
    # A legacy host was deployed before DEPLOY_RUN existed and has no switch marker yet.
    runs = ({"DEPLOY_RUN": "101-1"}, {"DEPLOY_RUN": "102-1"}) if marked else ({}, {})
    assert host.deploy(IMAGE_A, bundle=_bundle(CI_SECRETS), env=runs[0]).returncode == 0
    assert host.deploy(IMAGE_B, env=runs[1]).returncode == 0
    before = host.files()
    calls_before = len(host.calls())

    deploy = host.deploy(
        IMAGE_C, bundle=bundle, env={"DEPLOY_MODE": deploy_mode, "DEPLOY_RUN": attempt, **stubs}
    )
    assert deploy.returncode != 0
    assert reason in deploy.stderr
    rollback_calls = len(host.calls())
    result = _ci_auto_rollback(host, attempt, deploy_mode=deploy_mode)

    assert result.returncode == 0, result.stderr
    assert result.stdout == STOPPED_BEFORE_SWITCH.format(attempt)
    assert host.calls()[rollback_calls:] == []
    calls = host.calls()[calls_before:]
    assert not any(set(c.split()) & {"pull", "run", "up", "down", "rm"} for c in calls)
    # Pre-switch refusal leaves the previous-release record untouched too.
    assert host.files() == before
    state = _read_dotenv(host.app_dir / ".deploy-state")
    assert state["current_image"] == IMAGE_B and "rolled_back" not in state
    assert list(host.tmp.iterdir()) == []


def test_ci_auto_rollback_keeps_the_only_release_when_deploy_stops_before_the_switch(
    host: Host,
) -> None:
    first = host.deploy(IMAGE_A, bundle=_bundle(CI_SECRETS), env={"DEPLOY_RUN": "101-1"})
    assert first.returncode == 0
    before = host.files()
    assert ".deploy-state" in before and ".deploy-state.previous" not in before
    calls_before = len(host.calls())

    deploy = host.deploy(IMAGE_C, bundle=_bundle({"PATH": "/tmp"}), env={"DEPLOY_RUN": "102-1"})
    assert deploy.returncode != 0
    assert "PATH is not in the allowlist" in deploy.stderr
    result = _ci_auto_rollback(host, "102-1")

    assert result.returncode == 0, result.stderr
    assert result.stdout == STOPPED_BEFORE_SWITCH.format("102-1")
    # No compose down and no bootstrap container: the running release A stays.
    calls = host.calls()[calls_before:]
    assert not any(set(c.split()) & {"pull", "run", "up", "down", "rm"} for c in calls)
    assert host.files() == before


def test_failed_stack_start_rolls_back_once_and_the_ci_rollback_is_idempotent(host: Host) -> None:
    assert host.deploy(IMAGE_A, bundle="", env={"DEPLOY_RUN": "101-1"}).returncode == 0
    assert host.deploy(IMAGE_B, env={"DEPLOY_RUN": "102-1"}).returncode == 0
    state_b = (host.app_dir / ".deploy-state").read_bytes()

    deploy = host.deploy(IMAGE_C, env={"DEPLOY_RUN": "103-1", "STUB_FAILING_IMAGE": IMAGE_C})

    assert deploy.returncode != 0
    assert "compose up failed; rolling back" in deploy.stderr
    assert f"rolled back to {IMAGE_B}" in deploy.stdout
    after_deploy = host.files()
    state_after_deploy = _read_dotenv(host.app_dir / ".deploy-state")
    assert state_after_deploy["current_image"] == IMAGE_B
    assert state_after_deploy["rolled_back"] == "true"

    result = _ci_auto_rollback(host, "103-1")

    assert result.returncode == 0, result.stderr
    assert f"rolled back to {IMAGE_B}" in result.stdout
    state = _read_dotenv(host.app_dir / ".deploy-state")
    assert state == {**state_after_deploy, "deployed_at": state["deployed_at"]}
    assert _read_dotenv(host.env_file)["IMAGE"] == IMAGE_B
    assert (host.app_dir / ".deploy-state.previous").read_bytes() == state_b
    after = host.files()
    del after[".deploy-state"], after_deploy[".deploy-state"]
    assert after == after_deploy


def test_ci_auto_rollback_after_a_failed_image_pull_keeps_the_working_release(host: Host) -> None:
    assert host.deploy(IMAGE_A, bundle="", env={"DEPLOY_RUN": "101-1"}).returncode == 0
    assert host.deploy(IMAGE_B, env={"DEPLOY_RUN": "102-1"}).returncode == 0
    before = host.files()
    calls_before = len(host.calls())
    deploy = host.deploy(IMAGE_C, env={"DEPLOY_RUN": "103-1", "STUB_PULL_FAILURE": IMAGE_C})
    assert deploy.returncode != 0
    assert f"pull {IMAGE_C}" in host.calls()[calls_before:]
    assert host.files() == before
    rollback_calls = len(host.calls())
    result = _ci_auto_rollback(host, "103-1")
    assert result.returncode == 0, result.stderr
    assert result.stdout == STOPPED_BEFORE_SWITCH.format("103-1")
    assert host.calls()[rollback_calls:] == []
    assert host.files() == before


def test_ci_auto_rollback_after_a_failed_health_check_restores_the_previous_release(
    host: Host,
) -> None:
    assert host.deploy(IMAGE_A, bundle="", env={"DEPLOY_RUN": "101-1"}).returncode == 0
    assert host.deploy(IMAGE_B, env={"DEPLOY_RUN": "102-1"}).returncode == 0
    assert host.deploy(IMAGE_C, env={"DEPLOY_RUN": "103-1"}).returncode == 0
    previous = (host.app_dir / ".deploy-state.previous").read_bytes()
    calls_before = len(host.calls())

    result = _ci_auto_rollback(host, "103-1")  # the health check of run 103-1 failed

    assert result.returncode == 0, result.stderr
    assert f"rolled back to {IMAGE_B}" in result.stdout
    calls = host.calls()[calls_before:]
    pull = calls.index(f"pull {IMAGE_B}")
    probe = next(i for i, call in enumerate(calls) if " run " in call)
    up = next(i for i, call in enumerate(calls) if " up " in call)
    assert pull < probe < up
    assert _read_dotenv(host.env_file)["IMAGE"] == IMAGE_B
    state = _read_dotenv(host.app_dir / ".deploy-state")
    assert state["current_image"] == IMAGE_B and state["rolled_back"] == "true"
    assert (host.app_dir / ".deploy-state.previous").read_bytes() == previous


def test_ci_auto_rollback_after_a_failed_health_check_keeps_the_revision_guard(
    host: Host,
) -> None:
    assert host.deploy(IMAGE_A, bundle="", env={"DEPLOY_RUN": "101-1"}).returncode == 0
    assert host.deploy(IMAGE_B, env={"DEPLOY_RUN": "102-1"}).returncode == 0
    assert host.deploy(IMAGE_C, env={"DEPLOY_RUN": "103-1"}).returncode == 0
    before = host.files()
    calls_before = len(host.calls())

    result = _ci_auto_rollback(
        host,
        "103-1",
        stubs={
            "STUB_PROBE_STATUS": "1",
            "STUB_PROBE_OUTPUT": "rollback revision check: refused: database revision is unknown",
        },
    )

    assert result.returncode != 0
    assert "rollback refused:" in result.stderr
    assert "database revision is unknown" in result.stderr
    assert "rolled back" not in result.stdout
    assert host.files() == before
    assert not any(" up " in call for call in host.calls()[calls_before:])


# The gate is for CI auto rollbacks only: a DEPLOY_RUN in an operator's shell (or a future
# rollback.yml) with no matching switch marker must not turn a manual rollback into a no-op.
@pytest.mark.parametrize("mode", [{"ROLLBACK_MODE": "manual"}, {}], ids=["explicit", "default"])
def test_manual_rollback_ignores_the_deploy_run_and_restores_the_previous_release(
    host: Host, mode: dict[str, str]
) -> None:
    assert host.deploy(IMAGE_A, bundle="", env={"DEPLOY_RUN": "101-1"}).returncode == 0
    assert host.deploy(IMAGE_B, env={"DEPLOY_RUN": "102-1"}).returncode == 0
    state_b = (host.app_dir / ".deploy-state").read_bytes()

    result = host.run("rollback.sh", env={**mode, "DEPLOY_RUN": "999-1"})

    assert result.returncode == 0, result.stderr
    assert f"rolled back to {IMAGE_A}" in result.stdout
    assert STOPPED_BEFORE_SWITCH.format("999-1") not in result.stdout
    assert _read_dotenv(host.env_file)["IMAGE"] == IMAGE_A
    state = _read_dotenv(host.app_dir / ".deploy-state")
    assert state["current_image"] == IMAGE_A and state["rolled_back"] == "true"
    assert (host.app_dir / ".deploy-state.previous").read_bytes() == state_b


@pytest.mark.parametrize("mode", ["manual", "auto"])
def test_environment_key_protocol_ignores_secret_bearing_exec_stderr(host: Host, mode: str) -> None:
    assert host.deploy(IMAGE_A, bundle="").returncode == 0
    assert host.deploy(IMAGE_B).returncode == 0
    result = host.run(
        "rollback.sh",
        env={"ROLLBACK_MODE": mode, "STUB_EXEC_STDERR": "Compose progress\nEXEC-SECRET-CANARY"},
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.count("environment keys:") == 3
    assert f"rolled back to {IMAGE_A}" in result.stdout
    assert "Compose progress" not in result.stdout + result.stderr
    assert "EXEC-SECRET-CANARY" not in result.stdout + result.stderr


@pytest.mark.parametrize(
    "status, output", [(1, '["A_KEY"]'), (0, '["A_KEY"]\n'), (0, '["A_KEY"]\n[]')]
)
def test_environment_key_protocol_rejects_failed_exit_and_extra_stdout(
    host: Host, status: int, output: str
) -> None:
    assert host.deploy(IMAGE_A, bundle="").returncode == 0
    assert host.deploy(IMAGE_B).returncode == 0
    result = host.run(
        "rollback.sh",
        env={
            "STUB_EXEC_FAILURE_SERVICE": "api",
            "STUB_EXEC_STATUS": str(status),
            "STUB_EXEC_ERROR": output,
        },
    )
    assert result.returncode == 1
    assert "api environment key diagnostics failed" in result.stderr
    assert "environment keys:" not in result.stdout
    assert "rolled back" not in result.stdout
    assert _read_dotenv(host.app_dir / ".deploy-state")["current_image"] == IMAGE_A


@pytest.mark.parametrize("status", [1, 125, 126, 127])
def test_generic_probe_failure_reports_only_controlled_numeric_exit(
    host: Host, status: int
) -> None:
    assert host.deploy(IMAGE_A, bundle="").returncode == 0
    assert host.deploy(IMAGE_B).returncode == 0
    before = host.files()
    result = host.run(
        "rollback.sh",
        env={
            "STUB_PROBE_STATUS": str(status),
            "STUB_PROBE_OUTPUT": "PROBE-SECRET-CANARY",
            "STUB_PROBE_STDERR": "DRIVER-SECRET-CANARY",
        },
    )
    assert result.returncode == 1
    assert f"rollback refused: target revision probe failed (exit {status})\n" in result.stderr
    assert "PROBE-SECRET-CANARY" not in result.stdout + result.stderr
    assert "DRIVER-SECRET-CANARY" not in result.stdout + result.stderr
    assert host.files() == before


@pytest.mark.parametrize("compose_path", ["docker-compose.yml", "deploy/compose/staging.yml"])
def test_worker_stop_grace_exceeds_the_application_shutdown_deadline(compose_path: str) -> None:
    compose = _read(compose_path)
    match = re.search(r"(?ms)^  worker:\n(.*?)(?=^  [a-z][a-z-]*:\n|\Z)", compose)
    assert match is not None
    worker = match.group(1)
    assert re.search(r"^    stop_grace_period: 40s$", worker, re.MULTILINE)
    # SIGTERM must reach the Python worker, whose first-signal deadline includes Runner.close.
    assert '    command: ["python", "-m", "app.worker"]' in worker


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


CI_PREFLIGHT_DOCKER = """\
#!__PYTHON__
import base64
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

args = sys.argv[1:]
record = {"argv": args, "docker_config": os.environ["DOCKER_CONFIG"]}
if args[0] == "login":
    record["token_stdin_correct"] = sys.stdin.read() == "REGISTRY_TOKEN_CANARY\\n"
    Path(os.environ["DOCKER_CONFIG"], "config.json").write_text("auth fixture")
if args[0] == "run":
    path = Path(args[args.index("--env-file") + 1])
    record["env_mode"] = stat.S_IMODE(path.stat().st_mode)
    fields = dict(line.split("=", 1) for line in path.read_text().splitlines())
    record["env_names"] = sorted(fields)
    bundle = base64.b64decode(fields["GITLAB_DEPLOYMENT_BUNDLE"]).decode()
    record["gitlab_names"] = sorted(line.split("=", 1)[0] for line in bundle.splitlines())
    record["gitlab_values"] = {
        name: base64.b64decode(value).decode()
        for name, _, value in (line.partition("=") for line in bundle.splitlines())
    }
    record["allowed_names"] = fields.get("GITLAB_DEPLOYMENT_KEYS", "").split()
    record["file_modes"] = [
        stat.S_IMODE(candidate.stat().st_mode)
        for candidate in path.parent.iterdir() if candidate.is_file()
    ]
with Path(os.environ["STUB_LOG"]).open("a") as log:
    log.write(json.dumps(record) + "\\n")
if os.environ.get("STUB_FAIL_STAGE") == args[0]:
    print("synthetic docker failure", file=sys.stderr)
    raise SystemExit(1)
if args[0] == "run":
    image_index = args.index(os.environ["STUB_EXPECTED_IMAGE"])
    command = args[image_index + 1:]
    assert command[:2] == ["python", "-c"]
    if "STUB_PAYLOAD_OVERRIDE" in os.environ:
        fields["GITLAB_DEPLOYMENT_BUNDLE"] = os.environ["STUB_PAYLOAD_OVERRIDE"]
    result = subprocess.run([sys.executable, *command[1:]], env=fields, check=False)
    raise SystemExit(result.returncode)
"""


def _run_ci_gitlab_pipeline(
    tmp_path: Path, values: Mapping[str, str], *, fail_stage: str = ""
) -> tuple[subprocess.CompletedProcess[str], list[str], list[dict[str, object]], Path]:
    """Run actual deploy-job local shell; record remote actions instead of contacting a host."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text(CI_PREFLIGHT_DOCKER.replace("__PYTHON__", sys.executable))
    docker.chmod(0o755)
    temporary = tmp_path / "runner-temp"
    temporary.mkdir()
    output = tmp_path / "github-output"
    log = tmp_path / "docker.jsonl"
    environment = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "RUNNER_TEMP": str(temporary),
        "GITHUB_OUTPUT": str(output),
        "GHCR_TOKEN": "REGISTRY_TOKEN_CANARY",
        "GHCR_USER": "fixture-user",
        "REGISTRY": "ghcr.io",
        "IMAGE": IMAGE_A,
        "STUB_EXPECTED_IMAGE": IMAGE_A,
        "STUB_LOG": str(log),
        "STUB_FAIL_STAGE": fail_stage,
        **values,
    }
    deploy_job = (
        WORKFLOW.read_text()
        .split("  deploy-staging:\n", 1)[1]
        .split("\n  deploy-monitoring:", 1)[0]
    )
    mutations = []
    result = subprocess.CompletedProcess[str]([], 0, "", "")
    for chunk in deploy_job.split("\n      - name: ")[1:]:
        name, _, step = chunk.partition("\n")
        if "uses: appleboy/" in step:
            mutations.append(name)
        if name not in {
            "Bundle application secrets",
            "Validate application bundle and GitLab configuration",
        }:
            continue
        result = subprocess.run(
            ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", _run_script(step)],
            cwd=REPO_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode:
            break
        if name == "Bundle application secrets":
            environment["APP_SECRETS_B64"] = output.read_text().strip().removeprefix("bundle=")
    calls = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    return result, mutations, calls, temporary


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


GITLAB_PROBE_HELPER = """\
from __future__ import annotations
import base64
import hashlib
import importlib.abc
import json
import os
import stat
import sys
from pathlib import Path

args = sys.argv[1:]
path = Path(args[args.index('--env-file') + 1])
image = args[args.index('--env-file') + 2]
fields = dict(line.split('=', 1) for line in path.read_text().splitlines())
record = {
    'image': image,
    'gitlab_values': {
        name: base64.b64decode(value).decode()
        for name, _, value in (
            line.partition('=') for line in
            base64.b64decode(fields['GITLAB_DEPLOYMENT_BUNDLE']).decode().splitlines()
        )
    },
    'allowed_names': fields.get('GITLAB_DEPLOYMENT_KEYS', '').split(),
    'env_mode': stat.S_IMODE(path.stat().st_mode),
    'before': {
        file.name: [
            hashlib.sha256(file.read_bytes()).hexdigest(),
            stat.S_IMODE(file.stat().st_mode),
        ]
        for file in Path(os.environ['APP_DIR']).iterdir() if file.is_file()
    },
}
with Path(os.environ['STUB_GITLAB_LOG']).open('a') as log:
    log.write(json.dumps(record) + '\\n')
kind = os.environ.get('STUB_GITLAB_IMAGE_KIND', 'current')
class ImageModules(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        missing = {'app.bootstrap.gitlab_deployment'}
        if kind == 'unsupported':
            missing.add('app.modules.integrations.gitlab.infrastructure.settings')
        if kind in {'legacy', 'unsupported'} and fullname in missing:
            raise ModuleNotFoundError(name=fullname)
sys.meta_path.insert(0, ImageModules())
# Only external Docker/image behavior is faked: target parsers execute real code.
os.environ.update(fields)
print(os.environ.get('STUB_GITLAB_STDERR', ''), file=sys.stderr, end='')
if os.environ.get('STUB_MOVE_TAG_AT_GITLAB') == 'true':
    Path(os.environ['STUB_TAG_MOVED']).touch()
assert args[-3:-1] == ['python', '-c']
exec(compile(args[-1], '<target-gitlab-preflight>', 'exec'))
"""


def _gitlab_enabled_settings() -> dict[str, str]:
    return {
        "GITLAB_CREDENTIAL_KEYS": _keyring(),
        "GITLAB_ACTIVE_KEY_ID": "current",
        "GITLAB_ALLOWED_ORIGINS": '["https://gitlab.com"]',
        "GITLAB_WEBHOOK_INTAKE_ENABLED": "true",
    }


@pytest.mark.parametrize("kind", ["current", "legacy"])
def test_host_gitlab_preflight_rejects_candidate_before_any_live_mutation(
    host: Host, kind: str
) -> None:
    assert (
        host.deploy(IMAGE_A, bundle=_bundle(CI_SECRETS), env={"DEPLOY_RUN": "101-1"}).returncode
        == 0
    )
    (host.app_dir / "api.env").chmod(0o640)
    before = host.files()
    modes = {path.name: _mode(path) for path in host.app_dir.iterdir()}
    calls_before = len(host.calls())
    result = host.deploy(
        IMAGE_B,
        bundle=_bundle({**CI_SECRETS, "GITLAB_WEBHOOK_INTAKE_ENABLED": "true"}),
        env={
            "DEPLOY_RUN": "102-1",
            "STUB_GITLAB_STDERR": "PROBE_SECRET_CANARY",
            "STUB_GITLAB_IMAGE_KIND": kind,
        },
    )
    assert result.returncode != 0
    assert "configuration_invalid" in result.stderr
    assert "PROBE_SECRET_CANARY" not in result.stdout + result.stderr
    assert host.files() == before
    assert {path.name: _mode(path) for path in host.app_dir.iterdir()} == modes
    assert not any(
        call.startswith("compose ") or call.startswith("rm ")
        for call in host.calls()[calls_before:]
    )
    assert list(host.tmp.iterdir()) == []


def test_host_gitlab_deploy_accepts_legacy_parsers_without_new_helper(host: Host) -> None:
    result = host.deploy(
        IMAGE_A,
        bundle=_bundle(_gitlab_enabled_settings()),
        env={"STUB_GITLAB_IMAGE_KIND": "legacy"},
    )
    assert result.returncode == 0, result.stderr
    assert _read_env_file(host.app_dir / "gitlab.env")["GITLAB_ACTIVE_KEY_ID"] == "current"
    assert (host.log.parent / "up-image").read_text().strip() == A_ID


@pytest.mark.parametrize("mode", ["manual", "auto"])
def test_host_gitlab_rollback_refuses_invalid_retained_configuration_without_mutation(
    host: Host, mode: str
) -> None:
    assert host.deploy(IMAGE_A, bundle=_bundle(_gitlab_enabled_settings())).returncode == 0
    assert host.deploy(IMAGE_B).returncode == 0
    path = host.app_dir / "gitlab.env"
    path.write_text("GITLAB_CREDENTIAL_KEYS='CONFIG_CANARY'\n")
    path.chmod(0o640)
    before = host.files()
    modes = {file.name: _mode(file) for file in host.app_dir.iterdir()}
    calls_before = len(host.calls())
    result = host.run(
        "rollback.sh", env={"ROLLBACK_MODE": mode, "STUB_GITLAB_STDERR": "PROBE_SECRET_CANARY"}
    )
    assert result.returncode != 0
    assert "configuration_invalid" in result.stderr
    assert "PROBE_SECRET_CANARY" not in result.stdout + result.stderr
    assert "CONFIG_CANARY" not in result.stdout + result.stderr
    assert host.files() == before
    assert {file.name: _mode(file) for file in host.app_dir.iterdir()} == modes
    assert not any(" up " in call or call.startswith("rm ") for call in host.calls()[calls_before:])
    assert list(host.tmp.iterdir()) == []


@pytest.mark.parametrize("kind", ["current", "legacy"])
def test_host_gitlab_deploy_accepts_supported_target(host: Host, kind: str) -> None:
    assert host.deploy(IMAGE_A, bundle=_bundle(CI_SECRETS)).returncode == 0
    settings = _gitlab_enabled_settings()
    settings["GITLAB_CREDENTIAL_KEYS"] = _keyring(indent=2)
    result = host.deploy(IMAGE_B, bundle=_bundle(settings), env={"STUB_GITLAB_IMAGE_KIND": kind})
    assert result.returncode == 0, result.stderr
    assert (
        _read_env_file(host.app_dir / "gitlab.env")["GITLAB_CREDENTIAL_KEYS"]
        == settings["GITLAB_CREDENTIAL_KEYS"]
    )
    assert list(host.tmp.iterdir()) == []


def test_host_gitlab_deploy_rejects_unsupported_target_without_live_changes(host: Host) -> None:
    assert host.deploy(IMAGE_A, bundle=_bundle(CI_SECRETS)).returncode == 0
    before = host.files()
    modes = {path.name: _mode(path) for path in host.app_dir.iterdir()}
    calls_before = len(host.calls())
    result = host.deploy(
        IMAGE_B,
        bundle=_bundle(_gitlab_enabled_settings()),
        env={"STUB_GITLAB_IMAGE_KIND": "unsupported"},
    )
    assert result.returncode != 0
    assert "configuration_invalid" in result.stderr
    assert host.files() == before
    assert {path.name: _mode(path) for path in host.app_dir.iterdir()} == modes
    assert not any(" up " in call or call.startswith("rm ") for call in host.calls()[calls_before:])
    assert list(host.tmp.iterdir()) == []


def test_host_gitlab_retained_files_are_validated_as_data_before_replacement(host: Host) -> None:
    assert host.deploy(IMAGE_A, bundle=_bundle(_gitlab_enabled_settings())).returncode == 0
    marker = host.log.parent / "must-not-execute"
    (host.app_dir / "gitlab.env").write_text(f"GITLAB_ACTIVE_KEY_ID=$(touch {marker})\n")
    (host.app_dir / "gitlab.env").chmod(0o640)
    before = host.files()
    modes = {path.name: _mode(path) for path in host.app_dir.iterdir()}
    result = host.deploy(IMAGE_B)
    assert result.returncode != 0
    assert "configuration_invalid" in result.stderr
    assert not marker.exists()
    assert host.files() == before
    assert {path.name: _mode(path) for path in host.app_dir.iterdir()} == modes
    assert list(host.tmp.iterdir()) == []


def test_host_gitlab_deploy_pins_tag_probe_and_startup_to_one_image(host: Host) -> None:
    tag = "ghcr.io/test/api:mutable"
    result = host.deploy(
        tag,
        bundle=_bundle(_gitlab_enabled_settings()),
        env={
            "STUB_SELECTED_REF": tag,
            "STUB_MOVE_TAG_AT_GITLAB": "true",
            "STUB_REPODIGESTS": "ghcr.io/unrelated/api@sha256:" + "d" * 64 + "\n" + IMAGE_A,
        },
    )
    assert result.returncode == 0, result.stderr
    probes = [
        json.loads(line)
        for line in (host.log.parent / "gitlab-probes.jsonl").read_text().splitlines()
    ]
    assert probes[-1]["image"] == A_ID
    assert (host.log.parent / "tag-moved").exists()
    assert (host.log.parent / "up-image").read_text().strip() == A_ID
    assert _read_dotenv(host.env_file)["IMAGE"] == IMAGE_A
    assert _read_dotenv(host.app_dir / ".deploy-state")["current_image"] == IMAGE_A
    assert host.calls().count(f"pull {tag}") == 1
    assert any(" up --pull missing " in call for call in host.calls())


@pytest.mark.parametrize("mode", ["manual", "auto"])
@pytest.mark.parametrize("kind", ["current", "legacy"])
def test_host_gitlab_rollback_retains_config_and_uses_the_admitted_image(
    host: Host, mode: str, kind: str
) -> None:
    assert host.deploy(IMAGE_A, bundle=_bundle(_gitlab_enabled_settings())).returncode == 0
    assert host.deploy(IMAGE_B).returncode == 0
    before_config = {name: (host.app_dir / name).read_bytes() for name in ENV_FILES}
    result = host.run("rollback.sh", env={"ROLLBACK_MODE": mode, "STUB_GITLAB_IMAGE_KIND": kind})
    assert result.returncode == 0, result.stderr
    probes = [
        json.loads(line)
        for line in (host.log.parent / "gitlab-probes.jsonl").read_text().splitlines()
    ]
    assert probes[-1]["image"] == A_ID
    assert (host.log.parent / "probe-image").read_text().strip() == A_ID
    assert (host.log.parent / "up-image").read_text().strip() == A_ID
    assert {name: (host.app_dir / name).read_bytes() for name in ENV_FILES} == before_config
    assert list(host.tmp.iterdir()) == []


@pytest.mark.parametrize("mode", ["manual", "auto"])
def test_host_gitlab_rollback_rejects_unsupported_target_without_live_changes(
    host: Host, mode: str
) -> None:
    assert host.deploy(IMAGE_A, bundle=_bundle(_gitlab_enabled_settings())).returncode == 0
    assert host.deploy(IMAGE_B).returncode == 0
    before = host.files()
    modes = {path.name: _mode(path) for path in host.app_dir.iterdir()}
    calls_before = len(host.calls())
    result = host.run(
        "rollback.sh", env={"ROLLBACK_MODE": mode, "STUB_GITLAB_IMAGE_KIND": "unsupported"}
    )
    assert result.returncode != 0
    assert "configuration_invalid" in result.stderr
    assert host.files() == before
    assert {path.name: _mode(path) for path in host.app_dir.iterdir()} == modes
    assert not any(" up " in call or call.startswith("rm ") for call in host.calls()[calls_before:])
    assert list(host.tmp.iterdir()) == []


@pytest.mark.parametrize("mode", ["manual", "auto"])
@pytest.mark.parametrize("kind", ["current", "legacy"])
def test_host_gitlab_rollback_rejects_enabled_intake_without_runtime(
    host: Host, mode: str, kind: str
) -> None:
    assert host.deploy(IMAGE_A, bundle=_bundle(_gitlab_enabled_settings())).returncode == 0
    assert host.deploy(IMAGE_B).returncode == 0
    # An otherwise generated retained configuration has intake enabled in api.env,
    # but no worker runtime in gitlab.env. Both target parser generations must refuse it.
    (host.app_dir / "gitlab.env").write_text("")
    (host.app_dir / "gitlab.env").chmod(0o640)
    (host.app_dir / "api.env").chmod(0o640)
    before = host.files()
    modes = {path.name: _mode(path) for path in host.app_dir.iterdir()}
    calls_before = len(host.calls())
    result = host.run("rollback.sh", env={"ROLLBACK_MODE": mode, "STUB_GITLAB_IMAGE_KIND": kind})
    assert result.returncode != 0
    assert "configuration_invalid" in result.stderr
    assert host.files() == before
    assert {path.name: _mode(path) for path in host.app_dir.iterdir()} == modes
    assert not any(" up " in call or call.startswith("rm ") for call in host.calls()[calls_before:])
    assert list(host.tmp.iterdir()) == []


def test_host_gitlab_redeploy_disable_and_explicit_removal_preserve_bundle_semantics(
    host: Host,
) -> None:
    settings = _gitlab_enabled_settings()
    assert host.deploy(IMAGE_A, bundle=_bundle(settings)).returncode == 0
    keyring = (host.app_dir / "gitlab.env").read_bytes()
    assert host.deploy(IMAGE_B).returncode == 0
    assert (host.app_dir / "gitlab.env").read_bytes() == keyring
    assert _read_env_file(host.app_dir / "api.env")["GITLAB_WEBHOOK_INTAKE_ENABLED"] == "true"
    settings["GITLAB_WEBHOOK_INTAKE_ENABLED"] = "false"
    assert host.deploy(IMAGE_C, bundle=_bundle(settings)).returncode == 0
    assert (host.app_dir / "gitlab.env").read_bytes() == keyring
    assert _read_env_file(host.app_dir / "api.env")["GITLAB_WEBHOOK_INTAKE_ENABLED"] == "false"
    assert host.deploy(IMAGE_A, bundle="").returncode == 0
    for name in ENV_FILES:
        assert (host.app_dir / name).read_bytes() == b""
        assert _mode(host.app_dir / name) == 0o600


def test_host_gitlab_failed_stack_start_retains_current_configuration_after_auto_rollback(
    host: Host,
) -> None:
    settings = _gitlab_enabled_settings()
    assert host.deploy(IMAGE_A, bundle=_bundle(settings)).returncode == 0
    assert host.deploy(IMAGE_B).returncode == 0
    before_config = {name: (host.app_dir / name).read_bytes() for name in ENV_FILES}
    result = host.deploy(IMAGE_BAD, env={"STUB_FAILING_IMAGE": IMAGE_BAD})
    assert result.returncode != 0
    assert f"rolled back to {IMAGE_B}" in result.stdout
    assert {name: (host.app_dir / name).read_bytes() for name in ENV_FILES} == before_config
    assert list(host.tmp.iterdir()) == []


IO_FAILURE_WRAPPER = """\
#!/usr/bin/env bash
command_name="$(basename "$0")"
case "$STUB_IO_FAILURE:$command_name" in
  temporary-directory:mktemp)
    if [[ "$*" == *gitlab-validation.* ]]; then
      printf '%s\\n' "$STUB_OUTSIDE_DIR"
      exit 1
    fi
    ;;
  temporary-file:mktemp)
    if [[ "$*" == *gitlab-validation.* && "$*" != *"-d "* ]]; then
      printf '%s\\n' "$STUB_OUTSIDE_DIR/nonexistent/candidate"
      exit 1
    fi
    ;;
  retained-read:cat)
    if [[ "${1:-}" == */gitlab.env ]]; then exit 1; fi
    ;;
  permission:chmod)
    if [[ "$*" == *gitlab-validation.* ]]; then exit 1; fi
    ;;
  rename:mv)
    destination="${@: -1}"
    if [[ "${destination}" == */gitlab-validation.*/gitlab.env ]]; then exit 1; fi
    ;;
  filter:tr|encode:base64)
    cat >/dev/null
    exit 1
    ;;
esac
exec "__REAL_COMMAND__" "$@"
"""


def _inject_io_failure(host: Host, failure: str) -> dict[str, str]:
    command = {
        "temporary-directory": "mktemp",
        "temporary-file": "mktemp",
        "write": "printf",
        "preflight-write": "printf",
        "retained-read": "cat",
        "permission": "chmod",
        "rename": "mv",
        "filter": "tr",
        "encode": "base64",
    }[failure]
    outside = host.tmp.parent / "outside-private-temporary-area"
    outside.mkdir()
    environment = {"STUB_IO_FAILURE": failure, "STUB_OUTSIDE_DIR": str(outside)}
    if command == "printf":
        bash_environment = host.tmp.parent / "fail-write.bash"
        bash_environment.write_text(
            "printf() {\n"
            "  if [[ $STUB_IO_FAILURE == write && $1 == \"%s='%s'\\n\" ]] || "
            "[[ $STUB_IO_FAILURE == preflight-write && "
            "$1 == 'GITLAB_DEPLOYMENT_BUNDLE=%s\\n' ]]; then\n"
            "    return 1\n"
            "  fi\n"
            '  builtin printf "$@"\n'
            "}\n"
        )
        environment["BASH_ENV"] = str(bash_environment)
    else:
        real_command = shutil.which(command)
        assert real_command is not None
        wrapper = host.bin_dir / command
        wrapper.write_text(IO_FAILURE_WRAPPER.replace("__REAL_COMMAND__", real_command))
        wrapper.chmod(0o755)
    return environment


@pytest.mark.parametrize("operation", ["deploy", "manual-rollback", "auto-rollback"])
@pytest.mark.parametrize(
    "failure",
    [
        "temporary-directory",
        "temporary-file",
        "write",
        "preflight-write",
        "retained-read",
        "permission",
        "rename",
        "filter",
        "encode",
    ],
)
def test_host_refuses_gitlab_io_failures_before_validation_or_live_changes(
    host: Host, operation: str, failure: str
) -> None:
    assert host.deploy(IMAGE_A, bundle=_bundle(_gitlab_enabled_settings())).returncode == 0
    assert host.deploy(IMAGE_B).returncode == 0
    before = host.files()
    modes = {path.name: _mode(path) for path in host.app_dir.iterdir()}
    calls_before = len(host.calls())
    environment = _inject_io_failure(host, failure)
    # Supply infrastructure settings independently of GitLab transport failure injection.
    environment.update(
        {
            "POSTGRES_PASSWORD": "db",
            "RABBITMQ_PASSWORD": "broker",
            "REDIS_PASSWORD": "cache",
            "DEPLOY_MODE": "edge",
            "EDGE_ALIAS": "api-staging",
            "POSTGRES_USER": "app",
            "POSTGRES_DB": "app",
            "RABBITMQ_USER": "app",
        }
    )
    if operation == "deploy":
        result = host.deploy(IMAGE_EXPLICIT, env=environment)
    else:
        result = host.run(
            "rollback.sh",
            IMAGE_A,
            env={**environment, "ROLLBACK_MODE": operation.removesuffix("-rollback")},
        )
    assert result.returncode != 0
    assert host.files() == before
    assert {path.name: _mode(path) for path in host.app_dir.iterdir()} == modes
    assert not any(
        call == "run" or call.startswith("run ") or " up " in call or call.startswith("rm ")
        for call in host.calls()[calls_before:]
    )
    outside = Path(environment["STUB_OUTSIDE_DIR"])
    assert outside.is_dir()
    assert list(outside.iterdir()) == []
    assert list(host.tmp.iterdir()) == []


@pytest.mark.parametrize("operation", ["deploy", "manual-rollback", "auto-rollback"])
def test_host_rejects_unavailable_docker_config_directory_without_live_changes(
    host: Host, operation: str
) -> None:
    # Missing TMPDIR refuses Docker auth setup before any Docker command can run.
    assert host.deploy(IMAGE_A, bundle=_bundle(_gitlab_enabled_settings())).returncode == 0
    assert host.deploy(IMAGE_B).returncode == 0
    before = host.files()
    modes = {path.name: _mode(path) for path in host.app_dir.iterdir()}
    calls_before = len(host.calls())
    environment = {"TMPDIR": str(host.tmp / "missing")}
    if operation == "deploy":
        result = host.deploy(IMAGE_EXPLICIT, env=environment)
    else:
        result = host.run(
            "rollback.sh",
            IMAGE_A,
            env={**environment, "ROLLBACK_MODE": operation.removesuffix("-rollback")},
        )
    assert result.returncode != 0
    assert host.files() == before
    assert {path.name: _mode(path) for path in host.app_dir.iterdir()} == modes
    assert host.calls()[calls_before:] == []
    assert list(host.tmp.iterdir()) == []


def test_host_stages_recipient_files_on_destination_filesystem(host: Host) -> None:
    real_mv = shutil.which("mv")
    assert real_mv is not None
    wrapper = host.bin_dir / "mv"
    wrapper.write_text(
        "#!/usr/bin/env bash\n"
        'destination="${@: -1}"\n'
        'if [[ "${destination%/*}" == "$APP_DIR" && "${destination##*/}" == *.env ]]; then\n'
        '  printf "%s\\n" "${@: -2:1}" >> "$STUB_STAGE_SOURCES"\n'
        "fi\n"
        f'exec "{real_mv}" "$@"\n'
    )
    wrapper.chmod(0o755)
    sources = host.tmp.parent / "stage-sources"
    result = host.deploy(
        IMAGE_A,
        bundle=_bundle(_gitlab_enabled_settings()),
        env={"STUB_STAGE_SOURCES": str(sources)},
    )
    assert result.returncode == 0, result.stderr
    paths = [Path(value) for value in sources.read_text().splitlines()]
    assert len(paths) == 5
    assert all(
        path.parent.parent == host.app_dir and path.parent.name.startswith(".stage.")
        for path in paths
    )
    assert not list(host.app_dir.glob(".stage.*"))
    assert list(host.tmp.iterdir()) == []


@pytest.mark.parametrize(
    "failure",
    [
        "temporary-directory",
        "temporary-file",
        "write",
        "preflight-write",
        "permission",
        "rename",
        "filter",
    ],
)
def test_gitlab_validation_helper_fails_in_conditional_context(host: Host, failure: str) -> None:
    environment = _inject_io_failure(host, failure)
    result = subprocess.run(
        [
            "bash",
            "-c",
            'source "$ENV_HELPER"; '
            'if ! validate_gitlab_deployment "$TARGET_IMAGE" "$APPLICATION_BUNDLE"; '
            "then exit 23; fi",
        ],
        env={
            "PATH": f"{host.bin_dir}{os.pathsep}{os.environ['PATH']}",
            "ENV_HELPER": str(host.app_dir / "env-file.sh"),
            "TARGET_IMAGE": IMAGE_A,
            "APPLICATION_BUNDLE": _bundle(_gitlab_enabled_settings()),
            "TMPDIR": str(host.tmp),
            "STUB_LOG": str(host.log),
            **environment,
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 23
    assert not host.calls()
    assert list(host.tmp.iterdir()) == []
    outside = Path(environment["STUB_OUTSIDE_DIR"])
    assert outside.is_dir()
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize("kind", ["current", "legacy"])
@pytest.mark.parametrize("variant", ["default-off", "key-newline", "multiline-newline", "partial"])
def test_ci_and_host_validate_identical_post_generator_values(
    tmp_path: Path, host: Host, kind: str, variant: str
) -> None:
    complete = _gitlab_enabled_settings()
    multiline = _keyring(indent=2)
    settings, expected, valid = {
        "default-off": ({}, {}, True),
        "key-newline": ({**complete, "GITLAB_ACTIVE_KEY_ID": "current\n"}, complete, True),
        "multiline-newline": (
            {**complete, "GITLAB_CREDENTIAL_KEYS": multiline + "\n"},
            {**complete, "GITLAB_CREDENTIAL_KEYS": multiline},
            True,
        ),
        "partial": (
            {"GITLAB_WEBHOOK_INTAKE_ENABLED": "true"},
            {"GITLAB_WEBHOOK_INTAKE_ENABLED": "true"},
            False,
        ),
    }[variant]
    ci_directory = tmp_path / "ci"
    ci_directory.mkdir()
    result, mutations, calls, temporary = _run_ci_gitlab_pipeline(
        ci_directory, {**CI_SECRETS, **settings}
    )
    assert (result.returncode == 0) is valid
    assert bool(mutations) is valid
    assert calls[-1]["gitlab_values"] == expected
    expected_names = [
        "GITLAB_CREDENTIAL_KEYS",
        "GITLAB_ACTIVE_KEY_ID",
        "GITLAB_ALLOWED_ORIGINS",
        "GITLAB_PRIVATE_ORIGINS",
        "GITLAB_WEBHOOK_INTAKE_ENABLED",
    ]
    assert calls[-1]["allowed_names"] == expected_names
    host_result = host.deploy(
        IMAGE_A,
        bundle=_bundle({**CI_SECRETS, **settings}),
        env={"STUB_GITLAB_IMAGE_KIND": kind},
    )
    assert (host_result.returncode == 0) is valid
    probes = [
        json.loads(line)
        for line in (host.log.parent / "gitlab-probes.jsonl").read_text().splitlines()
    ]
    assert probes[-1]["gitlab_values"] == expected
    assert probes[-1]["allowed_names"] == expected_names
    assert list(temporary.iterdir()) == []
    assert list(host.tmp.iterdir()) == []


@pytest.mark.parametrize("operation", ["deploy", "manual", "auto"])
@pytest.mark.parametrize("filename", ["gitlab.env", "api.env"])
@pytest.mark.parametrize("failure", ["unquoted", "missing-newline", "read"])
def test_retained_role_files_report_only_static_basename_and_category(
    host: Host, operation: str, filename: str, failure: str
) -> None:
    assert host.deploy(IMAGE_A, bundle=_bundle(CI_SECRETS)).returncode == 0
    assert host.deploy(IMAGE_B).returncode == 0
    key = "GITLAB_ACTIVE_KEY_ID" if filename == "gitlab.env" else "GITHUB_CLIENT_ID"
    contents = {
        "unquoted": f"{key}=CONFIG_SECRET_CANARY\n",
        "missing-newline": f"{key}='CONFIG_SECRET_CANARY'",
        "read": "",
    }
    (host.app_dir / filename).write_text(contents[failure])
    (host.app_dir / filename).chmod(0o640)
    environment = {}
    if failure == "read":
        real_cat = shutil.which("cat")
        assert real_cat is not None
        wrapper = host.bin_dir / "cat"
        wrapper.write_text(
            "#!/usr/bin/env bash\n"
            'if [[ "${1:-}" == */"$STUB_UNREADABLE_FILE" ]]; then\n'
            '  echo "IO_SECRET_CANARY $1" >&2; exit 1\n'
            "fi\n"
            f'exec "{real_cat}" "$@"\n'
        )
        wrapper.chmod(0o755)
        environment["STUB_UNREADABLE_FILE"] = filename
    before = host.files()
    modes = {path.name: _mode(path) for path in host.app_dir.iterdir()}
    calls_before = len(host.calls())
    if operation == "deploy":
        result = host.deploy(IMAGE_EXPLICIT, env=environment)
    else:
        result = host.run("rollback.sh", env={**environment, "ROLLBACK_MODE": operation})
    category = "read_failed" if failure == "read" else "format_invalid"
    assert f"GitLab configuration_invalid ({filename}: {category})" in result.stderr
    assert result.returncode != 0
    assert "SECRET_CANARY" not in result.stdout + result.stderr
    assert str(host.app_dir) not in result.stdout + result.stderr
    assert host.files() == before
    assert {path.name: _mode(path) for path in host.app_dir.iterdir()} == modes
    assert not any(
        call == "run" or call.startswith("run ") or " up " in call or call.startswith("rm ")
        for call in host.calls()[calls_before:]
    )
    assert list(host.tmp.iterdir()) == []


@pytest.mark.parametrize("operation", ["deploy", "manual", "auto"])
@pytest.mark.parametrize("kind", ["current", "legacy"])
def test_host_validates_exact_retained_values_without_second_normalization(
    host: Host, operation: str, kind: str
) -> None:
    assert host.deploy(IMAGE_A, bundle=_bundle(_gitlab_enabled_settings())).returncode == 0
    assert host.deploy(IMAGE_B).returncode == 0
    path = host.app_dir / "gitlab.env"
    content = path.read_text()
    assert "GITLAB_ACTIVE_KEY_ID='current'\n" in content
    path.write_text(
        content.replace("GITLAB_ACTIVE_KEY_ID='current'\n", "GITLAB_ACTIVE_KEY_ID='current\n'\n")
    )
    path.chmod(0o640)
    before = host.files()
    modes = {file.name: _mode(file) for file in host.app_dir.iterdir()}
    calls_before = len(host.calls())
    environment = {"STUB_GITLAB_IMAGE_KIND": kind}
    if operation == "deploy":
        result = host.deploy(IMAGE_EXPLICIT, env=environment)
    else:
        result = host.run("rollback.sh", env={**environment, "ROLLBACK_MODE": operation})
    assert result.returncode != 0
    assert "configuration_invalid" in result.stderr
    assert host.files() == before
    assert {file.name: _mode(file) for file in host.app_dir.iterdir()} == modes
    assert not any(" up " in call or call.startswith("rm ") for call in host.calls()[calls_before:])
    assert list(host.tmp.iterdir()) == []


REGISTRY_CAPTURE_PROBE = """\
from __future__ import annotations
import json
import os
import stat
import sys
from pathlib import Path
mode = os.fstat(2).st_mode
record = {
    'command': sys.argv[1],
    'regular': stat.S_ISREG(mode),
    'mode': stat.S_IMODE(mode),
}
if sys.argv[1] == 'login':
    record['token_stdin_correct'] = (
        sys.stdin.read() == os.environ['STUB_EXPECTED_REGISTRY_TOKEN'] + '\\n'
    )
with Path(os.environ['STUB_REGISTRY_CAPTURE_LOG']).open('a') as log:
    log.write(json.dumps(record) + '\\n')
"""


def _registry_failure_with_safety_checks(
    host: Host, operation: str, stage: str, message: str
) -> subprocess.CompletedProcess[str]:
    assert host.deploy(IMAGE_A, bundle=_bundle(CI_SECRETS)).returncode == 0
    assert host.deploy(IMAGE_B).returncode == 0
    helper = host.log.parent / "registry-capture-probe.py"
    helper.write_text(REGISTRY_CAPTURE_PROBE)
    capture_log = host.log.parent / "registry-capture.jsonl"
    shared_config = host.log.parent / "shared-docker-config"
    shared_config.mkdir(mode=0o700)
    error = f"REGISTRY_STDERR_CANARY https://user:URL_SECRET_CANARY@registry.test/ {message}"
    environment = {
        "GHCR_TOKEN": "REGISTRY_TOKEN_CANARY",
        "STUB_EXPECTED_REGISTRY_TOKEN": "REGISTRY_TOKEN_CANARY",
        "STUB_REGISTRY_HELPER": str(helper),
        "STUB_REGISTRY_CAPTURE_LOG": str(capture_log),
        "DOCKER_CONFIG": str(shared_config),
    }
    target = IMAGE_EXPLICIT if operation == "deploy" else IMAGE_A
    if stage == "login":
        environment["STUB_LOGIN_FAILURE"] = error
    else:
        environment.update({"STUB_PULL_FAILURE": target, "STUB_UNSAFE_ERROR": error})
    before = host.files()
    modes = {path.name: _mode(path) for path in host.app_dir.iterdir()}
    calls_before = len(host.calls())
    if operation == "deploy":
        result = host.deploy(target, env=environment)
    else:
        result = host.run("rollback.sh", env={**environment, "ROLLBACK_MODE": operation})
    assert result.returncode != 0
    for canary in ("REGISTRY_STDERR_CANARY", "URL_SECRET_CANARY", "REGISTRY_TOKEN_CANARY"):
        assert canary not in result.stdout + result.stderr + "\n".join(host.calls())
    captures = [json.loads(line) for line in capture_log.read_text().splitlines()]
    assert captures and all(record["regular"] and record["mode"] == 0o600 for record in captures)
    assert captures[0]["token_stdin_correct"] is True
    assert host.files() == before
    assert {path.name: _mode(path) for path in host.app_dir.iterdir()} == modes
    assert not any(
        call.startswith("run ") or " up " in call or call.startswith("rm ")
        for call in host.calls()[calls_before:]
    )
    assert list(shared_config.iterdir()) == []
    assert list(host.tmp.iterdir()) == []
    return result


@pytest.mark.parametrize("operation", ["deploy", "manual", "auto"])
@pytest.mark.parametrize(
    ("stage", "message", "expected_diagnostic"),
    [
        (
            "login",
            "unauthorized: authentication required",
            "registry login failed (authentication_denied:",
        ),
        ("login", "dial tcp: i/o timeout", "registry login failed (network_unavailable:"),
        (
            "pull",
            "denied: requested access to the resource is denied",
            "target image pull failed (authentication_denied:",
        ),
        (
            "pull",
            "manifest unknown: requested manifest was not found",
            "target image pull failed (manifest_missing:",
        ),
        ("pull", "no space left on device", "target image pull failed (insufficient_storage:"),
        ("pull", "TLS handshake timeout", "target image pull failed (network_unavailable:"),
    ],
    ids=["auth-login", "network-login", "auth-pull", "manifest", "disk", "network-pull"],
)
def test_known_registry_failures_keep_static_categories_and_stdin_credentials(
    host: Host, operation: str, stage: str, message: str, expected_diagnostic: str
) -> None:
    result = _registry_failure_with_safety_checks(host, operation, stage, message)

    assert expected_diagnostic in result.stderr


@pytest.mark.parametrize(
    ("operation", "expected_prefix"),
    [
        ("deploy", "deployment refused:"),
        ("manual", "rollback refused:"),
        ("auto", "rollback refused:"),
    ],
)
@pytest.mark.parametrize(
    ("stage", "expected_diagnostic"),
    [("login", "registry login failed"), ("pull", "target image pull failed")],
)
def test_unknown_registry_failures_keep_plain_static_diagnostic_and_stdin_credentials(
    host: Host, operation: str, expected_prefix: str, stage: str, expected_diagnostic: str
) -> None:
    result = _registry_failure_with_safety_checks(
        host, operation, stage, "unrecognized registry response"
    )

    assert f"{expected_prefix} {expected_diagnostic}" in result.stderr.splitlines()


def _inject_registry_capture_failure(host: Host, failure: str) -> dict[str, str]:
    environment = {"STUB_CAPTURE_FAILURE": failure}
    if failure == "write":
        bash_environment = host.log.parent / "fail-registry-write.bash"
        bash_environment.write_text(
            "printf() {\n"
            '  if [[ $1 == "" ]]; then echo CAPTURE_IO_CANARY >&2; return 1; fi\n'
            '  builtin printf "$@"\n'
            "}\n"
        )
        environment["BASH_ENV"] = str(bash_environment)
    else:
        command = {"temporary-file": "mktemp", "permission": "chmod", "read": "cat"}[failure]
        real_command = shutil.which(command)
        assert real_command is not None
        wrapper = host.bin_dir / command
        wrapper.write_text(
            "#!/usr/bin/env bash\n"
            'if [[ "$*" == *registry-error.* ]]; then\n'
            "  echo CAPTURE_IO_CANARY >&2; exit 1\n"
            "fi\n"
            f'exec "{real_command}" "$@"\n'
        )
        wrapper.chmod(0o755)
    return environment


@pytest.mark.parametrize("operation", ["deploy", "manual", "auto"])
@pytest.mark.parametrize("failure", ["temporary-file", "permission", "write", "read"])
def test_registry_capture_io_failure_refuses_before_docker_or_live_changes(
    host: Host, operation: str, failure: str
) -> None:
    assert host.deploy(IMAGE_A, bundle=_bundle(CI_SECRETS)).returncode == 0
    assert host.deploy(IMAGE_B).returncode == 0
    shared_config = host.log.parent / "shared-docker-config"
    shared_config.mkdir(mode=0o700)
    environment = {
        **_inject_registry_capture_failure(host, failure),
        "GHCR_TOKEN": "REGISTRY_TOKEN_CANARY",
        "DOCKER_CONFIG": str(shared_config),
    }
    before = host.files()
    modes = {path.name: _mode(path) for path in host.app_dir.iterdir()}
    calls_before = len(host.calls())
    if operation == "deploy":
        result = host.deploy(IMAGE_EXPLICIT, env=environment)
    else:
        result = host.run("rollback.sh", env={**environment, "ROLLBACK_MODE": operation})
    assert result.returncode != 0
    assert "registry login failed" in result.stderr
    assert "CANARY" not in result.stdout + result.stderr
    assert host.files() == before
    assert {path.name: _mode(path) for path in host.app_dir.iterdir()} == modes
    assert not any(
        call.startswith(("login ", "pull ", "run ", "rm ")) or " up " in call
        for call in host.calls()[calls_before:]
    )
    assert list(shared_config.iterdir()) == []
    assert list(host.tmp.iterdir()) == []
