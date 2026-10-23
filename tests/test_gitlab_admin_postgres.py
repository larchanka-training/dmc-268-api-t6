"""Real CLI/PG lifecycle with only third-party HTTP replaced by MockTransport.

This proves subprocess/config/use-case/SQL composition, not real HTTP/TLS. The
coordinator runs independent real CLI + PostgreSQL + verified local TLS proof.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.schema import CreateSchema, DropSchema

from alembic import command

pytestmark = pytest.mark.integration
_TOKEN = "SYNTHETIC_PAT_CANARY_169"
_SECRET = "SYNTHETIC_WEBHOOK_CANARY_169_256BITS"


@dataclass
class Operator:
    env: dict[str, str]
    launcher: Path
    workspace: UUID
    engine: Engine
    schema: str
    other_workspace: UUID

    def sql(
        self, statement: str, params: Mapping[str, object] | None = None
    ) -> list[tuple[Any, ...]]:
        with self.engine.begin() as connection:
            connection.execute(text(f'SET LOCAL search_path TO "{self.schema}"'))
            result = connection.execute(text(statement), params or {})
            return [tuple(row) for row in result] if result.returns_rows else []

    def run(
        self,
        args: list[str],
        secrets: dict[str, str] | None = None,
        *,
        recovery: bool = False,
        fd_path: Path | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        env = dict(self.env)
        if recovery:
            env.update(
                GITLAB_CREDENTIAL_KEYS="broken",
                GITLAB_ACTIVE_KEY_ID="absent",
                GITLAB_ALLOWED_ORIGINS="broken",
                GITLAB_PRIVATE_ORIGINS="broken",
                SSL_CERT_FILE="/nonexistent/CA_CANARY",
            )
        payload = json.dumps(secrets).encode() if secrets is not None else b""
        if fd_path is None:
            result = subprocess.run(
                [sys.executable, str(self.launcher), *args],
                input=payload,
                env=env,
                capture_output=True,
                timeout=15,
                check=False,
            )
        else:
            fd_path.write_bytes(payload)
            fd_path.chmod(0o600)
            with fd_path.open("rb") as stream:
                result = subprocess.run(
                    [
                        sys.executable,
                        str(self.launcher),
                        *args,
                        "--secrets-fd",
                        str(stream.fileno()),
                    ],
                    stdin=subprocess.DEVNULL,
                    pass_fds=(stream.fileno(),),
                    env=env,
                    capture_output=True,
                    timeout=15,
                    check=False,
                )
        for canary in (_TOKEN, _SECRET, "DSN_CANARY", "CA_CANARY"):
            assert canary.encode() not in result.stdout + result.stderr
        return result

    def ok(
        self,
        args: list[str],
        secrets: dict[str, str] | None = None,
        *,
        recovery: bool = False,
        fd_path: Path | None = None,
    ) -> dict[str, Any]:
        result = self.run(args, secrets, recovery=recovery, fd_path=fd_path)
        assert result.returncode == 0, result.stderr
        assert result.stderr == b""
        value: dict[str, Any] = json.loads(result.stdout)
        return value


@pytest.fixture
def operator(tmp_path: Path) -> Iterator[Operator]:
    database_url = os.environ.get("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("set TEST_DATABASE_URL to run PostgreSQL CLI tests")
    schema = f"test_gitlab_admin_{uuid4().hex}"
    workspace, other_workspace = uuid4(), uuid4()
    engine = create_engine(database_url, hide_parameters=True)
    launcher = tmp_path / "operator.py"
    launcher.write_text("""import httpx
from app.gitlab_admin import main
original = httpx.AsyncClient

def response(request):
    token = request.headers.get("PRIVATE-TOKEN")
    if token == "invalid":
        return httpx.Response(401, json={"private": token})
    path = request.url.path
    if path == "/api/v4/user":
        return httpx.Response(200, json={"id": 1, "state": "active"})
    if path.endswith("/languages"):
        return httpx.Response(200, json={"Python": 49.9, "TypeScript": 50.1})
    return httpx.Response(200, json={"id": int(path.rsplit("/", 1)[1]),
        "path_with_namespace": "parent/nested/project", "default_branch": "main",
        "web_url": str(request.url.copy_with(path="/parent/nested/project")),
        "archived": False, "empty_repo": False})

def client(*args, **kwargs):
    kwargs["transport"] = httpx.MockTransport(response)
    return original(*args, **kwargs)

httpx.AsyncClient = client
raise SystemExit(main())
""")
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("GITLAB_", "DATABASE_", "SSL_CERT_", "TEST_DATABASE_"))
    }
    env.update(
        DATABASE_URL=make_url(database_url)
        .update_query_dict({"options": f"-csearch_path={schema}"})
        .render_as_string(hide_password=False),
        GITLAB_ACTIVE_KEY_ID="current",
        GITLAB_CREDENTIAL_KEYS=json.dumps({"current": base64.b64encode(b"k" * 32).decode()}),
        GITLAB_ALLOWED_ORIGINS='["https://gitlab.com","https://example.com"]',
        PYTHONPATH=str(Path.cwd()),
    )
    try:
        with engine.connect() as connection:
            connection.execute(CreateSchema(schema))
            connection.execute(text(f'SET search_path TO "{schema}"'))
            connection.commit()
            config = Config("alembic.ini")
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
            connection.execute(
                text(
                    "INSERT INTO workspaces (id, name, daily_budget_usd) "
                    "VALUES (:id, 'existing CLI workspace', 7)"
                ),
                {"id": workspace},
            )
            connection.execute(
                text(
                    "INSERT INTO workspaces (id, name, daily_budget_usd) "
                    "VALUES (:id, 'other CLI tenant', 19)"
                ),
                {"id": other_workspace},
            )
            connection.execute(
                text(
                    "INSERT INTO prompt_versions (id, key, version, content, "
                    "checksum, is_active) VALUES (:id, 'review.system', 1, "
                    "'seeded CLI prompt', :checksum, true)"
                ),
                {"id": uuid4(), "checksum": "a" * 64},
            )
            connection.commit()
            yield Operator(env, launcher, workspace, engine, schema, other_workspace)
    finally:
        with engine.begin() as connection:
            connection.execute(DropSchema(schema, cascade=True))
        engine.dispose()


def create(
    operator: Operator,
    installation: UUID,
    *,
    origin: str = "https://gitlab.com",
    fd_path: Path | None = None,
    workspace: UUID | None = None,
) -> dict[str, Any]:
    return operator.ok(
        [
            "create",
            "--workspace-id",
            str(workspace or operator.workspace),
            "--installation-id",
            str(installation),
            "--origin",
            origin,
        ],
        {"token": _TOKEN, "webhook_secret": _SECRET},
        fd_path=fd_path,
    )


def test_cli_create_fd_bind_rotate_and_keyless_revoke_preserve_other_tenant(
    operator: Operator,
    tmp_path: Path,
) -> None:
    first, second, third = uuid4(), uuid4(), uuid4()
    create(operator, second, origin="https://example.com", workspace=operator.other_workspace)
    operator.ok(["bind-project", "--installation-id", str(second), "--project-id", "42"])
    second_before = operator.ok(["inspect", "--installation-id", str(second)], recovery=True)
    created = create(operator, first, fd_path=tmp_path / "protected.json")
    assert created == {
        "installation_id": str(first),
        "workspace_id": str(operator.workspace),
        "instance_origin": "https://gitlab.com",
        "generation": 1,
        "intake_enabled": False,
        "revoked": False,
    }
    bound = operator.ok(["bind-project", "--installation-id", str(first), "--project-id", "42"])
    assert bound["created"] is True
    assert bound["rules_created"] is True
    assert create(operator, first) == created
    assert operator.ok(["bind-project", "--installation-id", str(first), "--project-id", "42"]) == {
        "repository_id": bound["repository_id"],
        "created": False,
        "rules_created": False,
    }
    before = operator.ok(["inspect", "--installation-id", str(first)], recovery=True)
    assert before["connection"] == created
    assert before["repositories"] == [
        {
            "repository_id": bound["repository_id"],
            "project_id": 42,
            "enabled": True,
            "available": True,
            "rules_ready": True,
        }
    ]
    assert before["prompt_ready"] is True
    operator.env["GITLAB_ACTIVE_KEY_ID"] = "replacement"
    operator.env["GITLAB_CREDENTIAL_KEYS"] = json.dumps(
        {"replacement": base64.b64encode(b"n" * 32).decode()}
    )
    missing_key = operator.run(
        ["bind-project", "--installation-id", str(first), "--project-id", "43"]
    )
    assert missing_key.returncode == 1
    assert missing_key.stdout == b""
    assert missing_key.stderr == b"credential_operation_failed\n"
    rotated = operator.ok(
        ["rotate-token", "--installation-id", str(first), "--expected-generation", "1"],
        {"token": _TOKEN + "_new"},
    )
    assert rotated["generation"] == 2
    assert (
        operator.ok(["bind-project", "--installation-id", str(first), "--project-id", "42"])[
            "created"
        ]
        is False
    )
    stale = operator.run(
        ["rotate-token", "--installation-id", str(first), "--expected-generation", "1"],
        {"token": _TOKEN},
    )
    assert stale.stderr == b"connection_changed\n"
    revoked = operator.ok(["revoke-installation", "--installation-id", str(first)], recovery=True)
    assert revoked["generation"] == 3
    assert revoked["revoked"] is True
    assert revoked["intake_enabled"] is False
    assert (
        operator.ok(["revoke-installation", "--installation-id", str(first)], recovery=True)
        == revoked
    )
    assert operator.ok(["inspect", "--installation-id", str(first)], recovery=True)[
        "repositories"
    ] == [
        {
            "repository_id": bound["repository_id"],
            "project_id": 42,
            "enabled": True,
            "available": False,
            "rules_ready": True,
        }
    ]
    create(operator, third, origin="https://example.com")
    workspace_result = operator.ok(
        ["revoke-workspace", "--workspace-id", str(operator.workspace)], recovery=True
    )
    assert len(workspace_result["connections"]) == 2
    assert all(item["revoked"] for item in workspace_result["connections"])
    assert (
        operator.ok(["inspect", "--installation-id", str(second)], recovery=True) == second_before
    )
    assert operator.sql("SELECT daily_budget_usd FROM workspaces ORDER BY daily_budget_usd") == [
        (7,),
        (19,),
    ]


def test_cli_repeat_bind_rotation_revoke_preserve_custom_settings_and_terminal_history(
    operator: Operator,
) -> None:
    installation, change, run = uuid4(), uuid4(), uuid4()
    create(operator, installation)
    binding = operator.ok(
        ["bind-project", "--installation-id", str(installation), "--project-id", "42"]
    )
    repository = UUID(binding["repository_id"])
    assert operator.sql(
        "SELECT enabled, installation_available, default_engine, wait_for_ci, "
        "rag_enabled, review_event, max_comments, branch_filter, severity_threshold "
        "FROM repositories"
    ) == [(True, True, "fast", "never", False, "comment", 10, [], "all")]
    operator.sql(
        "UPDATE repositories SET enabled=false, max_comments=3, "
        "branch_filter=ARRAY['release/*'], severity_threshold='critical'"
    )
    operator.sql("UPDATE rule_versions SET is_active=false")
    operator.sql(
        "INSERT INTO rule_versions (id, repository_id, version, rules, checksum, is_active) "
        'VALUES (:id, :repository, 2, \'[{"name":"custom"}]\', :checksum, true)',
        {"id": uuid4(), "repository": repository, "checksum": "c" * 64},
    )
    operator.sql(
        "INSERT INTO code_changes (id, repository_id, external_id, external_number, title, "
        "source_branch, target_branch, base_sha, head_sha, state, web_url) VALUES "
        "(:id,:repository,1,1,'CLI history','feature','main',:base,:head,'open',"
        "'https://gitlab.com/parent/nested/project/-/merge_requests/1')",
        {"id": change, "repository": repository, "base": "a" * 40, "head": "b" * 40},
    )
    operator.sql(
        "INSERT INTO runs (id, code_change_id, origin_provider_installation_id, base_sha, "
        "head_sha,state,trigger,idempotency_key,engine,rule_version_id,prompt_version_id, "
        "available_at, finished_at) SELECT :id,:change,:origin,:base,:head, "
        "'succeeded','manual',:key, "
        "'fast',r.id,p.id,clock_timestamp(),'2026-01-01'::timestamptz "
        "FROM rule_versions r, prompt_versions p "
        "WHERE r.is_active AND p.is_active",
        {
            "id": run,
            "change": change,
            "origin": installation,
            "base": "a" * 40,
            "head": "b" * 40,
            "key": uuid4().hex * 2,
        },
    )
    rules_before = operator.sql("SELECT * FROM rule_versions ORDER BY version")
    terminal_before = operator.sql("SELECT * FROM runs WHERE id=:id", {"id": run})
    repeated = operator.ok(
        ["bind-project", "--installation-id", str(installation), "--project-id", "42"]
    )
    assert repeated == {"repository_id": str(repository), "created": False, "rules_created": False}
    invalid = operator.run(
        ["rotate-token", "--installation-id", str(installation)], {"token": "invalid"}
    )
    assert invalid.returncode == 1
    assert invalid.stdout == b""
    assert invalid.stderr == b"invalid_token\n"
    operator.ok(
        ["rotate-token", "--installation-id", str(installation)], {"token": _TOKEN + "_new"}
    )
    operator.ok(["revoke-workspace", "--workspace-id", str(operator.workspace)], recovery=True)
    assert operator.sql("SELECT * FROM rule_versions ORDER BY version") == rules_before
    assert operator.sql("SELECT * FROM runs WHERE id=:id", {"id": run}) == terminal_before
    assert operator.sql(
        "SELECT enabled, installation_available, default_engine, wait_for_ci, "
        "rag_enabled, max_comments, branch_filter, severity_threshold "
        "FROM repositories"
    ) == [(False, False, "fast", "never", False, 3, ["release/*"], "critical")]
    assert operator.sql("SELECT count(*) FROM gitlab_project_bindings") == [(0,)]
    assert operator.sql("SELECT count(*) FROM github_user_workspace_access") == [(0,)]
    assert operator.sql("SELECT intake_enabled FROM gitlab_connections") == [(False,)]


def test_activate_subprocess_safe_negatives_and_keyless_recovery(operator: Operator) -> None:
    installation = uuid4()
    created = create(operator, installation)
    args = ["activate", "--installation-id", str(installation), "--expected-generation", "1"]
    no_bindings = operator.run(args, {"token": _TOKEN})
    assert (no_bindings.returncode, no_bindings.stdout, no_bindings.stderr) == (
        1,
        b"",
        b"activation_no_bindings\n",
    )
    operator.ok(["bind-project", "--installation-id", str(installation), "--project-id", "42"])
    operator.sql("UPDATE repositories SET enabled=false")
    paused = operator.run(args)
    assert (paused.returncode, paused.stdout, paused.stderr) == (1, b"", b"activation_not_ready\n")
    stale = operator.run(args[:-1] + ["2"])
    assert stale.returncode == 1 and stale.stderr == b"connection_changed\n"
    assert (
        operator.ok(["inspect", "--installation-id", str(installation)], recovery=True)[
            "connection"
        ]
        == created
    )
    invalid_ca = operator.run(args, recovery=True)
    assert invalid_ca.returncode == 1 and invalid_ca.stderr == b"configuration_invalid\n"
    revoked = operator.ok(
        ["revoke-installation", "--installation-id", str(installation)], recovery=True
    )
    assert revoked["revoked"] is True and revoked["intake_enabled"] is False
    assert operator.sql("SELECT intake_enabled FROM gitlab_connections") == [(False,)]
