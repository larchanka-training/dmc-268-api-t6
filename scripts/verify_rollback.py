"""Opt-in disposable Docker verification. Run with uv; never uses shared staging."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Mapping
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

REPO = Path(__file__).resolve().parents[1]
M = "20261007_0028"
N = "20261010_0110"
ROLES = ("api", "worker", "webhook-worker")
SERVICES = (*ROLES, "postgres", "rabbitmq", "redis", "bootstrap")
HOST_FILES = (
    ".env",
    "api.env",
    "app.env",
    "worker.env",
    "webhook-worker.env",
    ".deploy-state",
    ".deploy-state.previous",
)


class VerificationError(Exception):
    """Controlled failure text; subprocess output never enters this exception."""


def assert_refusal(
    before: Mapping[str, object],
    after: Mapping[str, object],
    result: subprocess.CompletedProcess[str],
    reason: str,
) -> None:
    if before != after:
        raise VerificationError("refusal invariants changed")
    output = result.stdout + result.stderr
    if (
        result.returncode == 0
        or "rollback refused:" not in result.stderr
        or reason not in result.stderr
        or "rolled back" in output
        or "environment keys:" in output
    ):
        raise VerificationError("refusal outcome was unexpected")


def validate_diagnostics(output: str, expected: dict[str, list[str]]) -> dict[str, list[str]]:
    actual: dict[str, list[str]] = {}
    for line in output.splitlines():
        if "environment keys:" not in line:
            continue
        match = re.fullmatch(r"(api|worker|webhook-worker) environment keys: (\[.*\])", line)
        if match is None or match[1] in actual:
            raise VerificationError("environment key evidence was unexpected")
        try:
            keys: object = json.loads(match[2])
        except ValueError:
            raise VerificationError("environment key evidence was unexpected") from None
        if (
            not isinstance(keys, list)
            or any(
                not isinstance(key, str) or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) is None
                for key in keys
            )
            or keys != expected[match[1]]
        ):
            raise VerificationError("environment key evidence was unexpected")
        actual[match[1]] = keys
    if actual != expected:
        raise VerificationError("environment key evidence was unexpected")
    return actual


def write_docker_wrapper(
    path: Path, real: str, images: list[str], compose_plugin: str | None = None
) -> None:
    if not images or any(re.fullmatch(r"sha256:[a-f0-9]{64}", image) is None for image in images):
        raise VerificationError("local image identity was invalid")
    allowlist = "|".join(images)
    plugin_setup = ""
    if compose_plugin:
        plugin_setup = (
            '  if [[ -n "${DOCKER_CONFIG:-}" ]]; then\n'
            '    mkdir -p "$DOCKER_CONFIG/cli-plugins"\n'
            '    [[ -e "$DOCKER_CONFIG/cli-plugins/docker-compose" ]] || '
            f'ln -s {shlex.quote(compose_plugin)} "$DOCKER_CONFIG/cli-plugins/docker-compose"\n'
            "  fi\n"
        )
    path.write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\n"
        f"real={shlex.quote(real)}\n"
        'if [[ "$1" == pull && $# == 2 ]]; then\n'
        '  case "$2" in\n'
        f"    {allowlist})\n"
        '      actual="$("$real" image inspect --format "{{.Id}}" "$2" 2>/dev/null)"\n'
        '      [[ "$actual" == "$2" ]]\n'
        "      exit $? ;;\n"
        "  esac\n"
        "fi\n"
        'if [[ "$1" == compose ]]; then\n'
        + plugin_setup
        + '  has_pull=false; for arg in "$@"; do\n'
        '    case "$arg" in --pull|--pull=*) has_pull=true ;; esac\n'
        "  done\n"
        '  args=(); for arg in "$@"; do\n'
        '    args+=("$arg"); '
        '[[ "$arg" != up || "$has_pull" == true ]] || args+=(--pull never)\n'
        "  done\n"
        '  exec "$real" "${args[@]}"\n'
        "fi\n"
        'exec "$real" "$@"\n'
    )
    path.chmod(0o700)


def require_fresh_project(docker: str, project: str) -> None:
    if re.fullmatch(r"rollback110-[a-f0-9]{16}", project) is None:
        raise VerificationError("resource isolation could not be established")
    checks = [
        [
            "network",
            "ls",
            "--filter",
            f"name=^{project}(-offline)?_default$",
            "--format",
            "{{.Name}}",
        ],
        ["volume", "ls", "--filter", f"name=^{project}_", "--format", "{{.Name}}"],
        [
            "ps",
            "-a",
            "--filter",
            f"label=com.docker.compose.project={project}",
            "--format",
            "{{.ID}}",
        ],
        [
            "ps",
            "-a",
            "--filter",
            f"label=com.docker.compose.project={project}-offline",
            "--format",
            "{{.ID}}",
        ],
    ]
    for args in checks:
        result = subprocess.run([docker, *args], capture_output=True, text=True, check=False)
        if result.returncode or result.stdout.strip():
            raise VerificationError("resource isolation could not be established")


class LiveVerifier:
    """One invocation owns one fresh project, offline network, B tag and private temp root."""

    def __init__(self, docker: str, project: str, image_a: str, expected_a: str) -> None:
        self.docker = str(Path(docker).resolve())
        self.project = project
        self.image_a = image_a
        self.a = expected_a
        self.b = ""
        self.b_tag = f"dmc268-rollback110:{project.removeprefix('rollback110-')}-b"
        self.root = Path(tempfile.mkdtemp(prefix=project + "-"))
        self.app = self.root / "host"
        self.offline = self.root / "offline"
        self.environment = {
            key: value
            for key, value in os.environ.items()
            if key in {"PATH", "HOME", "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG", "TMPDIR"}
        }
        self.environment.update(APP_DIR=str(self.app), COMPOSE_PROJECT=project, DEPLOY_MODE="ports")
        self.secrets: list[str] = []
        self.transcript = self.root / "transcript.txt"
        self.transcript.touch(mode=0o600)
        self.offline_created = False
        self.b_owned = False
        self.compose: list[str] = []
        self.report: dict[str, object] = {
            "project": project,
            "revision_M": M,
            "revision_N": N,
            "cases": [],
        }
        self.cases: list[dict[str, object]] = []

    def command(
        self,
        args: list[str],
        label: str,
        *,
        environment: dict[str, str] | None = None,
        stdin: str | None = None,
        check: bool = True,
        timeout: int = 240,
    ) -> subprocess.CompletedProcess[str]:
        try:
            result = subprocess.run(
                args,
                env=environment or self.environment,
                input=stdin,
                capture_output=True,
                text=True,
                check=False,
                timeout=timeout,
            )
        except (OSError, subprocess.TimeoutExpired):
            raise VerificationError(f"{label}: command unavailable or timed out") from None
        with self.transcript.open("a") as transcript:
            transcript.write(
                f"\n{label}: exit {result.returncode}\n{result.stdout}{result.stderr}\n"
            )
        self.scan(result.stdout + result.stderr)
        if check and result.returncode:
            raise VerificationError(f"{label}: command failed (exit {result.returncode})")
        return result

    def scan(self, output: str) -> None:
        if any(secret and secret in output for secret in self.secrets):
            raise VerificationError("secret scan failed; raw output withheld")

    def docker_command(
        self, args: list[str], label: str, *, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        return self.command([self.docker, *args], label, check=check)

    def image_id(self, image: str) -> str:
        value = self.docker_command(
            ["image", "inspect", "--format", "{{.Id}}", image], "image identity"
        ).stdout.strip()
        if re.fullmatch(r"sha256:[a-f0-9]{64}", value) is None:
            raise VerificationError("image identity was invalid")
        return value

    def prepare(self) -> None:
        plugin = self.docker_command(
            [
                "info",
                "--format",
                '{{range .ClientInfo.Plugins}}{{if eq .Name "compose"}}{{.Path}}{{end}}{{end}}',
            ],
            "Compose plugin discovery",
        ).stdout.strip()
        if not Path(plugin).is_file():
            raise VerificationError("real Compose plugin is unavailable")
        private_config = self.root / "docker-config"
        (private_config / "cli-plugins").mkdir(parents=True, mode=0o700)
        (private_config / "cli-plugins/docker-compose").symlink_to(plugin)
        self.environment["DOCKER_CONFIG"] = str(private_config)
        self.report["compose_version"] = self.docker_command(
            ["compose", "version", "--short"], "real Compose version"
        ).stdout.strip()
        if self.image_id(self.image_a) != self.a:
            raise VerificationError("prepared A image identity did not match")
        existing = self.docker_command(
            ["image", "ls", "--filter", f"reference={self.b_tag}", "-q"], "B ownership"
        )
        if existing.stdout.strip():
            raise VerificationError("derived B tag already exists")
        graph_source = (
            "from alembic.config import Config; from alembic.script import ScriptDirectory; "
            "import json; s=ScriptDirectory.from_config(Config('/srv/alembic.ini')); "
            "print(json.dumps({'heads':list(s.get_heads()),"
            "'revisions':sorted(r.revision for r in s.walk_revisions())}))"
        )
        graph_a = json.loads(
            self.docker_command(
                ["run", "--rm", "--network", "none", self.a, "python", "-c", graph_source],
                "A graph",
            ).stdout
        )
        if graph_a["heads"] != [M]:
            raise VerificationError("A graph does not have the expected head")
        context = self.root / "build"
        context.mkdir(mode=0o700)
        shutil.copy(REPO / "tests/fixtures/rollback/new_revision.py", context / "new_revision.py")
        (context / "Dockerfile").write_text(
            f"FROM {self.image_a}\n"
            "COPY new_revision.py /srv/alembic/versions/20261010_0110_rollback_fixture.py\n"
        )
        self.b_owned = True
        self.docker_command(["build", "--pull=false", "-t", self.b_tag, str(context)], "build B")
        self.b = self.image_id(self.b_tag)
        if self.a == self.b:
            raise VerificationError("A and B images must differ")
        graph_b = json.loads(
            self.docker_command(
                ["run", "--rm", "--network", "none", self.b, "python", "-c", graph_source],
                "B graph",
            ).stdout
        )
        if graph_b["heads"] != [N] or M not in graph_b["revisions"]:
            raise VerificationError("B graph is not the synthetic child of A")
        self.report.update(
            image_A=self.a,
            image_B=self.b,
            local_pull_substitution=(
                "Only verified A/B image IDs; Compose up --pull never; "
                "probes and health run unchanged"
            ),
        )
        self.app.mkdir(mode=0o700)
        shutil.copy(REPO / "deploy/compose/staging.yml", self.app / "compose.yml")
        shutil.copy(REPO / "tests/fixtures/rollback/compose.yml", self.app / "compose.ports.yml")
        for name in ("deploy.sh", "rollback.sh", "env-file.sh", "check-rollback-revision.py"):
            shutil.copy(REPO / "deploy/scripts" / name, self.app / name)
            (self.app / name).chmod(0o700)
        wrapper_dir = self.root / "bin"
        wrapper_dir.mkdir(mode=0o700)
        write_docker_wrapper(wrapper_dir / "docker", self.docker, [self.a, self.b], plugin)
        self.environment["PATH"] = str(wrapper_dir) + os.pathsep + self.environment["PATH"]
        self.compose = [
            str(wrapper_dir / "docker"),
            "compose",
            "-p",
            self.project,
            "-f",
            str(self.app / "compose.yml"),
            "-f",
            str(self.app / "compose.ports.yml"),
            "--env-file",
            str(self.app / ".env"),
        ]
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pem = (
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
            .decode()
            .rstrip("\n")
        )
        public_pem = (
            key.public_key()
            .public_bytes(
                serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
            )
            .decode()
            .rstrip("\n")
        )
        values = {
            "GITHUB_APP_ID": "424242",
            "GITHUB_APP_PRIVATE_KEY": pem,
            "GITHUB_APP_BOT_LOGIN": "rollback110-fixture[bot]",
            "AUTH_JWT_PRIVATE_KEY": pem,
            "AUTH_JWT_PUBLIC_KEY": public_pem,
            "GITHUB_WEBHOOK_SECRET": uuid.uuid4().hex + uuid.uuid4().hex,
            "GITHUB_CLIENT_SECRET": uuid.uuid4().hex + uuid.uuid4().hex,
            "LLM_API_KEYS": uuid.uuid4().hex + uuid.uuid4().hex,
        }
        passwords = {
            name: uuid.uuid4().hex + uuid.uuid4().hex
            for name in ("POSTGRES_PASSWORD", "RABBITMQ_PASSWORD", "REDIS_PASSWORD")
        }
        self.secrets = [
            *passwords.values(),
            values["GITHUB_WEBHOOK_SECRET"],
            values["GITHUB_CLIENT_SECRET"],
            values["LLM_API_KEYS"],
            values["GITHUB_APP_BOT_LOGIN"],
            pem,
            public_pem,
            *pem.splitlines(),
            *public_pem.splitlines(),
        ]
        bundle = base64.b64encode(
            "\n".join(
                f"{name}={base64.b64encode(value.encode()).decode()}"
                for name, value in values.items()
            ).encode()
        ).decode()
        initialization = {
            **self.environment,
            **passwords,
            "APP_SECRETS_B64": bundle,
            "IMAGE": self.a,
        }
        helper = (
            'source "$APP_DIR/env-file.sh"; '
            'write_app_env_files "$APP_DIR" "$APP_SECRETS_B64"; '
            'write_compose_env_file "$APP_DIR/.env" "$IMAGE" app "$POSTGRES_PASSWORD" '
            'app ports "" app "$RABBITMQ_PASSWORD" "$REDIS_PASSWORD"'
        )
        self.command(
            ["bash", "-c", helper], "write fixture credentials", environment=initialization
        )
        base_keys = json.loads(
            self.docker_command(
                [
                    "run",
                    "--rm",
                    "--network",
                    "none",
                    self.a,
                    "python",
                    "-c",
                    "import os,json; print(json.dumps(sorted(os.environ.keys())))",
                ],
                "base image key names",
            ).stdout
        )
        common = {"DATABASE_URL", "RABBITMQ_URL"}
        app_keys = {"GITHUB_APP_ID", "GITHUB_APP_PRIVATE_KEY"}
        self.expected_keys = {
            "api": sorted(
                set(base_keys)
                | common
                | {
                    "POSTGRES_HOST",
                    "POSTGRES_PORT",
                    "POSTGRES_USER",
                    "POSTGRES_PASSWORD",
                    "POSTGRES_DB",
                    "REDIS_URL",
                    "AUTH_JWT_ISSUER",
                    "AUTH_JWT_AUDIENCE",
                    "AUTH_JWT_PRIVATE_KEY",
                    "AUTH_JWT_PUBLIC_KEY",
                    "GITHUB_WEBHOOK_SECRET",
                    "GITHUB_CLIENT_SECRET",
                }
            ),
            "worker": sorted(
                set(base_keys) | common | app_keys | {"WORKER_HEARTBEAT_FILE", "LLM_API_KEYS"}
            ),
            "webhook-worker": sorted(
                set(base_keys)
                | common
                | app_keys
                | {"WORKER_HEARTBEAT_FILE", "GITHUB_APP_BOT_LOGIN"}
            ),
        }

    def revision(self) -> str:
        return self.command(
            [
                *self.compose,
                "exec",
                "-T",
                "postgres",
                "psql",
                "-U",
                "app",
                "-d",
                "app",
                "-Atc",
                "SELECT version_num FROM alembic_version",
            ],
            "database revision",
        ).stdout.strip()

    def sql(self, statement: str) -> str:
        return self.command(
            [
                *self.compose,
                "exec",
                "-T",
                "postgres",
                "psql",
                "-U",
                "app",
                "-d",
                "app",
                "-Atc",
                statement,
            ],
            "fixture data",
        ).stdout.strip()

    def health(self, image: str) -> dict[str, object]:
        statuses = self.containers()
        for service in ROLES:
            selected = statuses[service]
            if (
                selected["image"] != image
                or selected["health"] != "healthy"
                or selected["status"] != "running"
            ):
                raise VerificationError(f"{service}: image or health was unexpected")
        source = (
            "import json,urllib.request; "
            "r=urllib.request.urlopen('http://127.0.0.1:8000/healthcheck',timeout=2); "
            "assert r.status==200; assert json.load(r)=={'status':'ok'}; print('healthcheck: ok')"
        )
        deadline = time.monotonic() + 30
        while True:
            result = self.command(
                [*self.compose, "exec", "-T", "api", "python", "-c", source],
                "literal API health",
                check=False,
            )
            if result.returncode == 0 and result.stdout.strip() == "healthcheck: ok":
                break
            if time.monotonic() >= deadline:
                raise VerificationError("API literal healthcheck timed out")
            time.sleep(1)
        for role in ("worker", "webhook-worker"):
            path = "/tmp/worker.heartbeat" if role == "worker" else "/tmp/webhook-worker.heartbeat"
            self.command(
                [
                    *self.compose,
                    "exec",
                    "-T",
                    role,
                    "python",
                    "-m",
                    "app.common.infrastructure.heartbeat",
                    path,
                    "30",
                ],
                f"{role} heartbeat",
            )
        return {
            "API": {"status_code": 200, "json": {"status": "ok"}},
            "workers": "heartbeat age <=30 seconds",
        }

    def containers(self) -> dict[str, dict[str, str]]:
        statuses: dict[str, dict[str, str]] = {}
        for service in SERVICES:
            ids = self.command(
                [*self.compose, "ps", "-a", "-q", service], "service identity"
            ).stdout.split()
            if len(ids) != 1:
                raise VerificationError(f"{service}: expected exactly one container")
            selected = (
                self.docker_command(
                    [
                        "inspect",
                        "--format",
                        "{{.Id}}|{{.Image}}|{{.State.Status}}|"
                        "{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}",
                        ids[0],
                    ],
                    "selected container fields",
                )
                .stdout.strip()
                .split("|")
            )
            if len(selected) != 4:
                raise VerificationError("selected container fields were unexpected")
            statuses[service] = dict(
                zip(("id", "image", "status", "health"), selected, strict=True)
            )
        return statuses

    def files(self, app: Path | None = None) -> dict[str, dict[str, object]]:
        directory = app or self.app
        return {
            name: {
                "sha256": hashlib.sha256((directory / name).read_bytes()).hexdigest(),
                "mode": oct((directory / name).stat().st_mode & 0o777),
            }
            for name in HOST_FILES
        }

    def snapshot(self) -> dict[str, object]:
        revision = self.revision()
        sentinel = (
            self.sql("SELECT sentinel FROM rollback110_fixture ORDER BY sentinel")
            if revision == N
            else self.sql("SELECT to_regclass('public.rollback110_fixture') IS NULL")
        )
        return {
            "files": self.files(),
            "containers": self.containers(),
            "revision": revision,
            "sentinel": sentinel,
        }

    def set_history(self) -> None:
        (self.app / ".deploy-state").write_text(f"current_image={self.b}\nfixture=B-at-M\n")
        (self.app / ".deploy-state.previous").write_text(
            f"current_image={self.a}\nfixture=previous-A\n"
        )
        for name in (".deploy-state", ".deploy-state.previous"):
            (self.app / name).chmod(0o600)

    def compatible(self, mode: str) -> None:
        # Deliberate B-at-M fixture: bootstrap is skipped only for this controlled replacement.
        self.command(
            [
                "bash",
                "-c",
                'source "$APP_DIR/env-file.sh"; '
                'write_compose_env_file "$APP_DIR/.env" "$NEXT_IMAGE" app '
                '"$(read_compose_env_var POSTGRES_PASSWORD "$APP_DIR/.env")" app ports "" app '
                '"$(read_compose_env_var RABBITMQ_PASSWORD "$APP_DIR/.env")" '
                '"$(read_compose_env_var REDIS_PASSWORD "$APP_DIR/.env")"',
            ],
            "prepare B at M",
            environment={**self.environment, "NEXT_IMAGE": self.b},
        )
        self.command(
            [*self.compose, "up", "-d", "--no-deps", "--wait", "--wait-timeout", "180", *ROLES],
            "B at M application replacement",
        )
        self.health(self.b)
        if (
            self.revision() != M
            or self.sql("SELECT to_regclass('public.rollback110_fixture') IS NULL") != "t"
        ):
            raise VerificationError("controlled B fixture did not retain M")
        self.set_history()
        current = (self.app / ".deploy-state").read_bytes()
        previous = (self.app / ".deploy-state.previous").read_bytes()
        before = self.snapshot()
        result = self.command(
            [str(self.app / "rollback.sh")],
            f"compatible {mode}",
            environment={**self.environment, "ROLLBACK_MODE": mode},
        )
        keys = validate_diagnostics(result.stdout, self.expected_keys)
        after = self.snapshot()
        expected_previous = current if mode == "manual" else previous
        if (
            self.app / ".deploy-state.previous"
        ).read_bytes() != expected_previous or self.revision() != M:
            raise VerificationError("compatible state semantics were unexpected")
        state = (self.app / ".deploy-state").read_text()
        if (
            f"current_image={self.a}\n" not in state
            or "rolled_back=true\n" not in state
            or f"rolled back to {self.a}" not in result.stdout
        ):
            raise VerificationError("compatible completion metadata was unexpected")
        self.cases.append(
            {
                "case": f"compatible-{mode}",
                "exit_code": result.returncode,
                "before": before,
                "after": after,
                "keys": keys,
                "health": self.health(self.a),
                "history_rule": "previous becomes B" if mode == "manual" else "previous remains A",
            }
        )
        print(f"compatible {mode}: passed", flush=True)

    def incompatible(self, mode: str) -> None:
        before = self.snapshot()
        result = self.command(
            [str(self.app / "rollback.sh")],
            f"incompatible {mode}",
            environment={**self.environment, "ROLLBACK_MODE": mode},
            check=False,
        )
        after = self.snapshot()
        assert_refusal(before, after, result, "database revision is unknown")
        self.cases.append(
            {
                "case": f"incompatible-{mode}",
                "exit_code": result.returncode,
                "reason": "database revision is unknown",
                "before": before,
                "after": after,
                "health": self.health(self.b),
            }
        )
        print(f"incompatible {mode}: passed", flush=True)

    def unreachable(self) -> None:
        self.offline.mkdir(mode=0o700)
        for path in self.app.iterdir():
            if path.is_file():
                shutil.copy(path, self.offline / path.name)
        project = self.project + "-offline"
        self.docker_command(
            ["network", "create", "--internal", project + "_default"], "offline network"
        )
        self.offline_created = True
        for mode in ("manual", "auto"):
            primary = self.snapshot()
            before: dict[str, object] = {"files": self.files(self.offline)}
            started = time.monotonic()
            result = self.command(
                [str(self.offline / "rollback.sh")],
                f"unreachable {mode}",
                environment={
                    **self.environment,
                    "APP_DIR": str(self.offline),
                    "COMPOSE_PROJECT": project,
                    "ROLLBACK_MODE": mode,
                },
                check=False,
                timeout=45,
            )
            elapsed = round(time.monotonic() - started, 2)
            after: dict[str, object] = {"files": self.files(self.offline)}
            assert_refusal(before, after, result, "cannot inspect target graph or database")
            if primary != self.snapshot():
                raise VerificationError("offline probe changed the primary project")
            self.cases.append(
                {
                    "case": f"unreachable-{mode}",
                    "exit_code": result.returncode,
                    "reason": "cannot inspect target graph or database",
                    "elapsed_seconds": elapsed,
                    "before": before,
                    "after": after,
                    "primary_unchanged": True,
                    "health": self.health(self.b),
                }
            )
            print(f"unreachable {mode}: passed", flush=True)

    def execute(self) -> None:
        self.prepare()
        self.command([str(self.app / "deploy.sh"), self.a], "deploy A")
        if self.revision() != M:
            raise VerificationError("A normal bootstrap did not reach M")
        self.report["initial_A_health"] = self.health(self.a)
        print("A normal deployment: passed", flush=True)
        self.compatible("manual")
        self.compatible("auto")
        self.command([str(self.app / "deploy.sh"), self.b], "deploy B")
        if self.revision() != N:
            raise VerificationError("B normal bootstrap did not reach N")
        self.sql(
            "INSERT INTO rollback110_fixture(sentinel) VALUES ('rollback110-nonsecret-sentinel')"
        )
        self.health(self.b)
        print("B normal deployment to N: passed", flush=True)
        self.incompatible("manual")
        self.incompatible("auto")
        self.unreachable()
        for project in (self.project, self.project + "-offline"):
            probes = self.docker_command(
                [
                    "ps",
                    "-a",
                    "--filter",
                    f"label=com.docker.compose.project={project}",
                    "--filter",
                    "label=com.docker.compose.oneoff=True",
                    "--format",
                    "{{.ID}}",
                ],
                "probe removal",
            )
            if probes.stdout.strip():
                raise VerificationError("ephemeral probe containers remained")
        self.report.update(
            cases=self.cases,
            secret_scan="all command output passed, including each PEM line",
            outcome="passed",
        )

    def cleanup(self) -> bool:
        good = True
        operations: list[tuple[list[str], str]] = []
        if self.compose:
            operations.append(
                ([*self.compose, "down", "-v", "--remove-orphans"], "cleanup project")
            )
        if self.offline_created:
            operations.append(
                (
                    [self.docker, "network", "rm", self.project + "-offline_default"],
                    "cleanup offline network",
                )
            )
        if self.b_owned:
            operations.append(([self.docker, "image", "rm", self.b_tag], "cleanup B tag"))
        for args, label in operations:
            try:
                result = self.command(args, label, check=False)
                good = good and result.returncode == 0
            except (VerificationError, OSError):
                good = False
        try:
            require_fresh_project(self.docker, self.project)
        except (VerificationError, OSError):
            good = False
        self.report["cleanup"] = "passed" if good else "failed"
        return good


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--docker", default=shutil.which("docker"))
    parser.add_argument("--project", default="rollback110-" + uuid.uuid4().hex[:16])
    parser.add_argument("--image-a", required=True)
    parser.add_argument("--expect-image-a", required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    try:
        if not args.docker:
            raise VerificationError("Docker is unavailable")
        require_fresh_project(args.docker, args.project)
        if args.report.exists() or args.report.is_symlink() or not args.report.parent.is_dir():
            raise VerificationError(
                "report destination must be a new file in an existing directory"
            )
        verifier = LiveVerifier(args.docker, args.project, args.image_a, args.expect_image_a)
        try:
            verifier.execute()
        except VerificationError as error:
            verifier.report.update(outcome="failed", failure=str(error), cases=verifier.cases)
            raise
        except Exception:
            verifier.report.update(
                outcome="failed",
                failure="unexpected verifier failure; raw diagnostics withheld",
                cases=verifier.cases,
            )
            raise VerificationError(
                "unexpected verifier failure; raw diagnostics withheld"
            ) from None
        finally:
            cleaned = verifier.cleanup()
            if not cleaned:
                verifier.report["outcome"] = "failed"
            rendered = json.dumps(verifier.report, indent=2) + "\n"
            verifier.scan(rendered)
            with args.report.open("x") as report:
                report.write(rendered)
            if cleaned and verifier.report.get("outcome") == "passed":
                shutil.rmtree(verifier.root)
            elif not cleaned:
                print(
                    "verification cleanup failed; owned resources need inspection", file=sys.stderr
                )
            if verifier.report.get("outcome") != "passed":
                print(f"Private diagnostic transcript: {verifier.transcript}", file=sys.stderr)
        if not cleaned:
            return 1
        print("verification and cleanup: passed", flush=True)
        return 0
    except VerificationError as error:
        print(f"verification failed: {error}", file=sys.stderr)
        return 1
    except OSError:
        print(
            "verification failed: file or Docker access failed; raw diagnostics withheld",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
