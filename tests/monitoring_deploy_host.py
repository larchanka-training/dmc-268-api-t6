"""The fake monitoring host shared by the monitoring deploy tests (fixture host)."""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
MONITORING = REPO_ROOT / "deploy" / "monitoring"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci-cd.yml"

API_PROJECT = "dmc-268-api-staging"
TOKEN = "123456:TEST-bot_token"
CHAT_ID = "-1001234567890"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _job(name: str) -> str:
    workflow = _read(WORKFLOW)
    start = workflow.index(f"\n  {name}:\n")
    following = re.search(r"\n  [a-z-]+:\n", workflow[start + 1 :])
    return workflow[start : start + 1 + following.start() if following else None]


QUERIES = MONITORING / "postgres-exporter" / "queries.yml"
RUN_COLUMNS = {
    "id",
    "state",
    "trigger",
    "engine",
    "attempt",
    "error_code",
    "available_at",
    "created_at",
    "started_at",
    "finished_at",
}
RUN_ACTION_COLUMNS = {"run_id", "tool", "started_at", "duration_ms"}
# The column grants of the read-only roles, by table and role: all they can read.
METADATA_GRANTS = {
    ("runs", "grafana_reader"): RUN_COLUMNS,
    ("runs", "metrics_exporter"): RUN_COLUMNS,
    ("run_actions", "grafana_reader"): RUN_ACTION_COLUMNS,
    ("run_actions", "metrics_exporter"): RUN_ACTION_COLUMNS,
    ("usage_events", "grafana_reader"): {
        "provider",
        "model",
        "operation",
        "tokens_in",
        "tokens_out",
        "cache_read_tokens",
        "cost_usd",
        "created_at",
    },
    ("usage_events", "metrics_exporter"): {"cost_usd", "created_at"},
}


def _query(name: str) -> str:
    """The SQL of one postgres-exporter query block of queries.yml."""
    block = re.search(rf"^{name}:\n  query: \|\n((?:    .*\n)+)", _read(QUERIES), re.M)
    assert block is not None, name
    return block.group(1)


def _grants(sql: str) -> dict[tuple[str, str], set[str]]:
    """Column grants by table and recipient role: GRANT SELECT (columns) ON table TO roles."""
    statements = re.findall(r"GRANT SELECT \((.*?)\)\s+ON (\w+) TO ([^;]+);", sql, re.DOTALL)
    assert len(statements) == sql.count("GRANT SELECT ("), "a column grant was not read"
    grants: dict[tuple[str, str], set[str]] = {}
    for columns, table, roles in statements:
        for role in roles.split(","):
            grants.setdefault((table, role.strip()), set()).update(
                column.strip() for column in columns.split(",")
            )
    return grants


FAKE_DOCKER = """\
#!/usr/bin/env bash
# Fake docker CLI: logs every call; psql, grafana and rclone obscure stdin go to their own logs.
printf '%s\\n' "$*" >> "${STUB_LOG}"
case "$1" in
  network) [[ " $* " == *" ${STUB_MISSING_NETWORK:-none} "* ]] && exit 1; exit 0 ;;
  run) cat >> "${STUB_OBSCURE_STDIN}"; echo "obscured_${RANDOM}${RANDOM}" ;;
  ps)
    case "$*" in
      *com.docker.compose.project=dmc-268-api-prod*) echo "${STUB_PROD_POSTGRES_ID}" ;;
      *) echo "${STUB_POSTGRES_ID}" ;;
    esac
    ;;
  exec)
    printf -- '-- container %s\\n' "$3" >> "${STUB_PSQL_STDIN}"
    cat >> "${STUB_PSQL_STDIN}"; printf '\\n--\\n' >> "${STUB_PSQL_STDIN}"
    if [[ "$3" == "${STUB_EXEC_FAILS:-none}" ]]; then exit 1; fi
    ;;
  compose)
    if [[ " $* " == *" exec "* ]]; then cat >> "${STUB_GRAFANA_STDIN}"; fi
    if [[ " $* " == *" up "* && -n "${STUB_UP_FAILS:-}" ]]; then exit 1; fi
    ;;
esac
"""
FAKE_CHOWN = """\
#!/usr/bin/env bash
printf '%s\\n' "$*" >> "${STUB_CHOWN_LOG}"
"""


@dataclass
class Host:
    root: Path
    api_dir: Path
    monitoring_dir: Path
    stubs: Path

    @property
    def env_file(self) -> Path:
        return self.monitoring_dir / ".env"

    def env(self) -> dict[str, str]:
        values = {}
        for line in self.env_file.read_text(encoding="utf-8").splitlines():
            key, _, value = line.partition("=")
            values[key] = value
        return values

    def log(self, name: str) -> str:
        path = self.stubs / name
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def deploy(
        self,
        *,
        unset: tuple[str, ...] = (),
        extra: dict[str, str] | None = None,
        **overrides: str,
    ) -> subprocess.CompletedProcess[str]:
        env = {
            "PATH": f"{self.stubs}:{os.environ['PATH']}",
            "HOME": str(self.root),
            "API_DIR": str(self.api_dir),
            "GRAFANA_ROOT_URL": "https://grafana.example.test/",
            "APP_DOMAIN": "example.test",
            # An unset Environment variable reaches the host as an empty value.
            "PROBE_PROD_HOSTS": "",
            # So does an unset repository variable.
            "LLM_MONTHLY_BUDGET_USD": "",
            "ALERT_TELEGRAM_BOT_TOKEN": TOKEN,
            "ALERT_TELEGRAM_CHAT_ID": CHAT_ID,
            "STUB_LOG": str(self.stubs / "docker.log"),
            "STUB_PSQL_STDIN": str(self.stubs / "psql.log"),
            "STUB_GRAFANA_STDIN": str(self.stubs / "grafana.log"),
            "STUB_CHOWN_LOG": str(self.stubs / "chown.log"),
            "STUB_POSTGRES_ID": "pgcontainer",
            "STUB_PROD_POSTGRES_ID": "pgcontainer-prod",
            "STUB_OBSCURE_STDIN": str(self.stubs / "obscure.log"),
            "BACKUP_DIR": str(self.root / "backups"),
            "PROD_BACKUP_DIR": str(self.root / "backups-prod"),
            **(extra or {}),
            **overrides,
        }
        for name in unset:
            env.pop(name, None)
        return subprocess.run(
            [str(self.monitoring_dir / "deploy.sh")],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )


@pytest.fixture
def host(tmp_path: Path) -> Host:
    """A host after the application deploy: its .env and env-file.sh, the uploaded stack."""
    api_dir = tmp_path / "opt" / API_PROJECT
    api_dir.mkdir(parents=True)
    (api_dir / ".env").write_text(
        "POSTGRES_USER=app\nPOSTGRES_PASSWORD=pg-secret\nPOSTGRES_DB=app\n"
        "DEPLOY_MODE=edge\nEDGE_ALIAS=api-staging\nREDIS_PASSWORD=redis-pa$$word\n",
        encoding="utf-8",
    )
    monitoring_dir = tmp_path / "opt" / "dmc-268-monitoring"
    subprocess.run(["cp", "-R", f"{MONITORING}/.", str(monitoring_dir)], check=True)
    for generated in (".env", "alertmanager/alertmanager.yml", "prometheus/targets/probes.json"):
        (monitoring_dir / generated).unlink(missing_ok=True)
    subprocess.run(["rm", "-rf", str(monitoring_dir / "alertmanager" / "secrets")], check=True)
    (monitoring_dir / "env-file.sh").write_text(
        _read(REPO_ROOT / "deploy" / "scripts" / "env-file.sh"), encoding="utf-8"
    )
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    for name, source in (("docker", FAKE_DOCKER), ("chown", FAKE_CHOWN)):
        (stubs / name).write_text(source, encoding="utf-8")
        (stubs / name).chmod(0o755)
    return Host(tmp_path, api_dir, monitoring_dir, stubs)
