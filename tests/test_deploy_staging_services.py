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
  pull)
    if [[ "$2" == "${STUB_PULL_FAILURE:-}" ]]; then
      echo "${STUB_UNSAFE_ERROR:-raw-registry-secret}" >&2
      exit 1
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
        echo "${STUB_PROBE_OUTPUT:-rollback revision check: compatible}"
        exit "${STUB_PROBE_STATUS:-0}"
        ;;
      up)
        printf '%s\\n' "${IMAGE:-}" > "${STUB_UP_IMAGE}"
        # Like Compose: env_file paths resolve against the directory of the first compose file.
        # Every file must exist: deploy and rollback keep each role's file in place.
        for name in app.env api.env worker.env webhook-worker.env; do
          [[ -f "${project_dir}/${name}" ]] || { echo "env file ${name} not found" >&2; exit 1; }
        done
        ! grep -qxF "IMAGE=${STUB_FAILING_IMAGE:-}" "${env_file}"
        ;;
      exec)
        service="${action_args[1]}"
        cp "${APP_DIR}/.deploy-state" "${STUB_DIAGNOSTIC_STATE}"
        printf '%s\\n' "${service}" >> "${STUB_DIAGNOSTIC_CALLS}"
        if [[ "${service}" == "${STUB_EXEC_FAILURE_SERVICE:-}" ]]; then
          printf '%s\\n' "${STUB_EXEC_ERROR-raw-container-secret}"
          exit "${STUB_EXEC_STATUS:-1}"
        fi
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
    (tmp_path / "container-environments.json").write_text(
        json.dumps(
            {service: {"LC_CTYPE": "UTF-8"} for service in ("api", "worker", "webhook-worker")}
        )
    )
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

    result = host.deploy(IMAGE_A, bundle=bundle)

    assert result.returncode == 0, result.stderr
    assert "not set: LLM_FALLBACK_MODEL" in stdout.splitlines()
    assert _read_env_file(host.app_dir / "worker.env") == {
        "LLM_API_KEYS": "sk-test-1,sk-test-2",
        "LLM_BASE_URL": "https://llm.test/api/v1",
        "LLM_MODEL": "gpt-4.1-mini",
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


@pytest.mark.parametrize(
    ("deploy", "health", "rollback"),
    [
        ("failure", "skipped", "failure"),
        ("success", "failure", "failure"),
        ("failure", "skipped", "success"),
    ],
)
def test_failed_forward_deploy_never_promotes_and_reports_failed_auto_rollback(
    deploy: str, health: str, rollback: str, tmp_path: Path
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
    failed = subprocess.run(
        [
            "bash",
            "--noprofile",
            "--norc",
            "-eo",
            "pipefail",
            "-c",
            render(_run_script(_workflow_step("Fail the pipeline after rollback"))),
        ],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert failed.returncode == 1
    assert ("rollback failed" in failed.stderr) is (rollback == "failure")
    auto = _workflow_step("Rollback on failed deploy or health check")
    assert "ROLLBACK_MODE=auto" in auto
    assert (
        "if: always() && (steps.deploy.outcome == 'failure' || steps.health.outcome == 'failure')"
        in auto
    )
    promotion = WORKFLOW.read_text().split("  promote-staging:", 1)[1]
    assert "needs.deploy-staging.outputs.promote == 'true'" in promotion


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
    assert all(secrets.values())  # every role has something to lose
    assert secrets_after_manual == secrets
    for file_name, (_, keys) in ENV_FILES.items():
        assert _read_env_file(host.app_dir / file_name) == {
            name: rotated[name] for name in keys if rotated[name]
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
    assert calls.count(f"pull {selected}") == 1
    assert any(" up --pull never " in call for call in calls)
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
    current_state = (host.app_dir / ".deploy-state").read_bytes()
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
    assert (host.log.parent / "diagnostic-before-state").read_bytes() == current_state
    calls = host.calls()[calls_before:]
    up = next(i for i, call in enumerate(calls) if " up " in call)
    assert all(up < i for i, call in enumerate(calls) if " exec " in call)
    assert sum(" exec -T " in call for call in calls) == 3
    for value in (canary, "PEM-LINE-CANARY", "HOSTILE-KEY-CANARY", *FAKE_PEM.splitlines()):
        assert value not in result.stdout + result.stderr


@pytest.mark.parametrize("mode", ["manual", "auto"])
@pytest.mark.parametrize("service", ["api", "worker", "webhook-worker"])
@pytest.mark.parametrize("failure", ["exec", "noise", "invalid-list", "empty"])
def test_diagnostic_failure_is_secret_safe_and_does_not_record_success(
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
    assert (host.app_dir / ".deploy-state").read_bytes() == state
    assert (host.app_dir / ".deploy-state.previous").read_bytes() == previous
    assert "rolled back" not in result.stdout + result.stderr
    assert f"{service} environment key diagnostics failed" in result.stderr
    assert "rollback completion failed:" in result.stderr
    assert "target stack may be running" in result.stderr
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
