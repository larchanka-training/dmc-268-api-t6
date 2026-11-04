"""Fakes of the staging host shared by the deploy tests: a fake docker CLI and the real scripts."""

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

STAGING_COMPOSE = REPO_ROOT / "deploy" / "compose" / "staging.yml"
PROJECT = "dmc-268-api-staging"
PROD_PROJECT = "dmc-268-api-prod"
STOPPED = (
    f"stopped compose project {PROD_PROJECT}; no bootstrap container (NO_PREVIOUS_RELEASE=stop)"
)
IMAGE_A = "ghcr.io/test/api@sha256:" + "a" * 64
IMAGE_B = "ghcr.io/test/api@sha256:" + "b" * 64

IMAGE_EXPLICIT = "ghcr.io/test/api@sha256:" + "e" * 64

A_ID = "sha256:" + "1" * 64
B_ID = "sha256:" + "2" * 64
EXPLICIT_ID = "sha256:" + "3" * 64

# GitHub rejects secret names starting with GITHUB_, so the App secrets are GH_* in the
# Environment (comment of the tech lead in #35); the container keeps the names of .env.example.
SECRET_SOURCES = {
    "BITBUCKET_CREDENTIAL_KEYS": "BITBUCKET_CREDENTIAL_KEYS",
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
    "BITBUCKET_ACTIVE_KEY_ID": "BITBUCKET_ACTIVE_KEY_ID",
    "BITBUCKET_WEBHOOK_BASE_URL": "BITBUCKET_WEBHOOK_BASE_URL",
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
    "bitbucket.env": (
        "BITBUCKET_ENV_KEYS",
        {"BITBUCKET_CREDENTIAL_KEYS", "BITBUCKET_ACTIVE_KEY_ID"},
    ),
    "api.env": (
        "API_ENV_KEYS",
        {
            "GITHUB_WEBHOOK_SECRET",
            "GITHUB_CLIENT_ID",
            "GITHUB_CLIENT_SECRET",
            "AUTH_JWT_PRIVATE_KEY",
            "AUTH_JWT_PUBLIC_KEY",
            "GITLAB_WEBHOOK_INTAKE_ENABLED",
            "BITBUCKET_WEBHOOK_BASE_URL",
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

# Multi-line like a PEM, with what a shell or Compose could rewrite: $, ${...} and backslashes.
FAKE_PEM = "-----BEGIN TEST KEY-----\nAAAA$HOME/${PATH}\nBB\\nBB\\\\CC\n-----END TEST KEY-----"
CI_SECRETS = {
    "BITBUCKET_CREDENTIAL_KEYS": '{"test-key":"' + base64.b64encode(b"b" * 32).decode() + '"}',
    "BITBUCKET_ACTIVE_KEY_ID": "test-key",
    "BITBUCKET_WEBHOOK_BASE_URL": "https://staging-api.example.test",
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
        # The replica count the scripts hand to Compose; the shell value beats --env-file there.
        printf '%s\\n' "${WORKER_REPLICAS-}" > "${STUB_UP_REPLICAS}"
        # Likewise the tracing values: endpoint, then environment name ("unset" when absent).
        printf '%s\\n%s\\n' "${OTEL_EXPORTER_OTLP_ENDPOINT-unset}" \\
          "${DEPLOY_ENVIRONMENT-unset}" > "${STUB_UP_TRACING}"
        # Like Compose: env_file paths resolve against the directory of the first compose file.
        # Every file must exist: deploy and rollback keep each role's file in place.
        for name in app.env api.env worker.env webhook-worker.env gitlab.env bitbucket.env; do
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
        # exec -T [--index N] SERVICE python -c SOURCE; the replica defaults to the first.
        index=1
        exec_args=()
        for (( i = 0; i < ${#action_args[@]}; i++ )); do
          if [[ "${action_args[i]}" == --index ]]; then
            index="${action_args[i + 1]}"
            i=$(( i + 1 ))
          else
            exec_args+=("${action_args[i]}")
          fi
        done
        action_args=("${exec_args[@]}")
        service="${action_args[1]}"
        cp "${APP_DIR}/.deploy-state" "${STUB_DIAGNOSTIC_STATE}"
        printf '%s\\n' "${service}" >> "${STUB_DIAGNOSTIC_CALLS}"
        failing_index="${STUB_EXEC_FAILURE_INDEX:-${index}}"
        failing_service="${STUB_EXEC_FAILURE_SERVICE:-}"
        if [[ "${service}" == "${failing_service}" && "${index}" == "${failing_index}" ]]; then
          printf '%s\\n' "${STUB_EXEC_ERROR-raw-container-secret}"
          exit "${STUB_EXEC_STATUS:-1}"
        fi
        printf '%s\\n' "${STUB_EXEC_STDERR:-}" >&2
        STUB_EXEC_INDEX="${index}" uv run python "${STUB_EXEC_HELPER}" "${action_args[@]}"
        ;;
      down) exit "${STUB_DOWN_STATUS:-0}" ;;
      ps)
        [[ -z "${STUB_PS_OUTPUT:-}" ]] || printf '%s\\n' "${STUB_PS_OUTPUT}"
        exit "${STUB_PS_STATUS:-0}"
        ;;
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
index = os.environ['STUB_EXEC_INDEX']
os.environ.clear()
os.environ.update(environments.get(f'{service}#{index}', environments[service]))
assert sys.argv[3:5] == ['python', '-c']
exec(compile(sys.argv[5], '<docker-exec>', 'exec'))
"""

_ENV_FILE_ENTRY = re.compile(r"^([A-Z][A-Z0-9_]*)='([^']*)'\n", re.MULTILINE)


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


def _read_env_file(path: Path) -> dict[str, str]:
    """Parse NAME='value' entries the way Compose reads them; anything else fails the test."""
    text = path.read_text(encoding="utf-8")
    entries = dict(_ENV_FILE_ENTRY.findall(text))
    assert "".join(f"{name}='{value}'\n" for name, value in entries.items()) == text
    return entries


def _read_dotenv(path: Path) -> dict[str, str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    return {name: value for name, _, value in (line.partition("=") for line in lines)}


@dataclass(frozen=True)
class Host:
    """A fake host: APP_DIR with the uploaded deploy files and a fake docker on PATH.

    The basename of APP_DIR is the compose project, as on the course VPS.
    """

    app_dir: Path
    bin_dir: Path
    volumes: Path
    log: Path
    tmp: Path
    project: str = PROJECT

    @property
    def env_file(self) -> Path:
        return self.app_dir / ".env"

    def files(self) -> dict[str, bytes]:
        return {path.name: path.read_bytes() for path in sorted(self.app_dir.iterdir())}

    def calls(self) -> list[str]:
        return self.log.read_text(encoding="utf-8").splitlines() if self.log.exists() else []

    def add_volume(self, name: str) -> None:
        (self.volumes / f"{self.project}_{name}").touch()

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
            "STUB_UP_REPLICAS": str(self.log.parent / "up-replicas"),
            "STUB_UP_TRACING": str(self.log.parent / "up-tracing"),
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


def make_host(tmp_path: Path, project: str = PROJECT) -> Host:
    app_dir = tmp_path / "opt" / project
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
        app_dir=app_dir,
        bin_dir=bin_dir,
        volumes=volumes,
        log=tmp_path / "docker.log",
        tmp=tmp,
        project=project,
    )


def _assert_stopped(host: Host, calls: list[str]) -> None:
    down = calls.index(f"compose -p {PROD_PROJECT} down --remove-orphans")
    assert calls.index(f"compose -p {PROD_PROJECT} ps -aq") > down
    # An nginx left by an earlier bootstrap-mode rollback would keep answering under the alias.
    assert f"rm -f {PROD_PROJECT}-bootstrap" in calls
    assert not any(call.startswith("run -d") for call in calls)
    assert not any(call.startswith("pull nginx") for call in calls)
    assert not (host.app_dir / ".deploy-state").exists()
    assert not (host.app_dir / ".deploy-state.previous").exists()


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

ROLLBACK_WORKFLOW = WORKFLOW.parent / "rollback.yml"

IMAGE_C = "ghcr.io/test/api@sha256:" + "c" * 64

IMAGE_BAD = "ghcr.io/test/api@sha256:" + "f" * 64


def _read(*parts: str) -> str:
    return REPO_ROOT.joinpath(*parts).read_text(encoding="utf-8")


def _service_block(name: str) -> str:
    compose = STAGING_COMPOSE.read_text(encoding="utf-8")
    match = re.search(rf"^  {name}:\n((?:    .*\n|\n)*)", compose, re.MULTILINE)
    assert match is not None, f"service {name} is missing"
    return match.group(1)


def _keyring(
    *, key_id: str = "current", material: bytes = b"k" * 32, indent: int | None = None
) -> str:
    return json.dumps({key_id: base64.b64encode(material).decode()}, indent=indent)


def _bundle(secrets: Mapping[str, str]) -> str:
    lines = "".join(
        f"{name}={base64.b64encode(value.encode()).decode()}\n" for name, value in secrets.items()
    )
    return base64.b64encode(lines.encode()).decode()


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


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


@pytest.fixture
def host(tmp_path: Path) -> Host:
    return make_host(tmp_path)


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


def _write_compose_env_file(path: Path, *args: str) -> None:
    subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; shift; write_compose_env_file "$@"',
            "env-file",
            str(REPO_ROOT / "deploy" / "scripts" / "env-file.sh"),
            str(path),
            *args,
        ],
        check=True,
    )


# With the file first, the ten arguments of a caller from before NO_PREVIOUS_RELEASE
# (scripts/verify_rollback.py).
LEGACY_VALUES = (IMAGE_A, "app", "pg", "app", "edge", "api-prod", "app", "mq", "redis")


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
