"""Authorized actual-network component proof; imported adapters run only under main.

The backend runs for real against local consumed-contract dependencies. No transport,
provider factory, SQL pool factory or model is replaced. Runtime requires separate GO.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import http.client
import http.server
import json
import os
import socket
import ssl
import subprocess
import sys
import threading
import time
import traceback
from collections.abc import AsyncIterator, Callable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import aio_pika
import httpx
import psycopg
from alembic.config import Config
from gitlab_network_proof_launcher import identify_child, retain_owned, stop_owned
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import CreateSchema, DropSchema

from alembic import command
from app.bootstrap.gitlab import gitlab_webhook_replay_resources
from app.bootstrap.gitlab_reviews import build_gitlab_review_worker_resources
from app.bootstrap.llm_gateway import build_gateway
from app.bootstrap.seed_prompts import load_prompt_assets, seed_prompt_versions
from app.modules.integrations.gitlab.application.request_credentials import GitLabRequestCredentials
from app.modules.integrations.gitlab.application.webhook_receipts import prepare_gitlab_receipt
from app.modules.integrations.gitlab.infrastructure.connection_store import CredentialCipherAdapter
from app.modules.integrations.gitlab.infrastructure.credential_cipher import CredentialCipher
from app.modules.integrations.gitlab.infrastructure.request_credentials import (
    SqlAlchemyGitLabTargetReader,
)
from app.modules.integrations.gitlab.infrastructure.run_credentials import GitLabRunCredentials
from app.modules.integrations.gitlab.infrastructure.run_http import GitLabRunHttp
from app.modules.integrations.gitlab.infrastructure.settings import GitLabRuntimeSettings
from app.modules.reviews.application.conventions import (
    GenerateRepoConventions,
    PolicyFilteredConventionsSource,
    derive_languages,
)
from app.modules.reviews.application.handle_review_run import AttemptCheckpoint, ClaimedAttempt
from app.modules.reviews.application.llm import RunCallContext
from app.modules.reviews.application.queue_messages import ReviewPublishPointer
from app.modules.reviews.application.run_failures import (
    FAST_ATTEMPT_DEADLINE,
    LEASE,
    RequiredSourceUnavailable,
    RunCancelled,
    RunFailure,
)
from app.modules.reviews.application.vcs_diff import FetchVcsReviewInput
from app.modules.reviews.infrastructure.amqp import LazyAmqpPublisher
from app.modules.reviews.infrastructure.conventions_unit_of_work import (
    SqlAlchemyRepositoryConventionsUnitOfWork,
)
from app.modules.reviews.infrastructure.gitlab_repository_tree import GitLabRepositoryTree
from app.modules.reviews.infrastructure.gitlab_revision_source import GitLabRevisionSource
from app.modules.reviews.infrastructure.gitlab_run_source import GitLabRunSource
from app.modules.reviews.infrastructure.gitlab_vcs import HttpGitLabVcsProvider
from app.modules.reviews.infrastructure.llm.models import GatewayConventionsModel
from app.modules.reviews.infrastructure.llm.settings import LlmSettings
from app.modules.reviews.infrastructure.run_lifecycle_store import SqlAlchemyRunLifecycleUnitOfWork
from app.modules.reviews.infrastructure.run_repository import SqlAlchemyRunRepository
from app.worker import WorkerSettings, run_worker

GROUPS = [
    "trust-authority",
    "trees-policy-revisions",
    "incomplete-before-model",
    "gl-only-success",
    "configured-gh-retry-redelivery",
    "retirement-sibling",
    "new-run-generation2",
]


def write_private(path: Path, value: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".new")
    temporary.write_text(json.dumps(value, indent=2, default=str) + "\n")
    temporary.chmod(0o600)
    temporary.replace(path)


def install_network_guard(config: dict[str, Any]) -> None:
    """Process-local Python socket allowlist; not a claim about Postman/CLI internals."""
    ports = {49323, 58447, config["postman_port"], config["tls_port"]}

    def observe(event: str, values: tuple[Any, ...]) -> None:
        if event == "socket.getaddrinfo":
            host, port = values[:2]
            if isinstance(host, bytes):
                host = host.decode()
            if host not in {"localhost", "127.0.0.1", "::1"} or port not in ports:
                raise RuntimeError("application DNS refused outside reserved loopback scope")
        elif event == "socket.connect":
            address = values[1]
            if isinstance(address, tuple) and (
                address[0] not in {"127.0.0.1", "::1"} or address[1] not in ports
            ):
                raise RuntimeError("application connect refused outside reserved loopback scope")

    sys.addaudithook(observe)


class Proof:
    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self.root = Path(config["source"])
        self.scratch = Path(config["scratch"])
        self.manifest = json.loads((self.scratch / "manifest.json").read_text())
        self.schema = "t15_gitlab_" + uuid4().hex
        self.url = (
            make_url(config["database_url"])
            .update_query_dict(
                {"options": "-csearch_path=" + self.schema, "application_name": self.schema}
            )
            .render_as_string(hide_password=False)
        )
        self.env = dict(os.environ)
        self.env.update(
            DATABASE_URL=self.url,
            RABBITMQ_URL=config["rabbitmq_url"],
            GITLAB_ACTIVE_KEY_ID="proof",
            GITLAB_CREDENTIAL_KEYS=json.dumps({"proof": config["credential_key"]}),
            GITLAB_ALLOWED_ORIGINS=json.dumps(
                sorted({row["origin"] for row in config["tenants"].values()})
            ),
            GITLAB_PRIVATE_ORIGINS=json.dumps(
                sorted({row["origin"] for row in config["tenants"].values()})
            ),
            SSL_CERT_FILE=str(self.scratch / "ca.pem"),
            LLM_MODEL="test-model",
            LLM_BASE_URL=f"https://localhost:{config['tls_port']}/v1",
            LLM_API_KEYS=config["model_key"],
            LLM_CONTEXT_WINDOW="100000",
            LLM_MAX_OUTPUT_TOKENS="8000",
            LLM_PRICE_INPUT_PER_MTOK="1",
            LLM_PRICE_OUTPUT_PER_MTOK="2",
            WORKER_ID="t15-public-worker",
        )
        self.state: dict[str, Any] = {
            "schema": self.schema,
            "checks": [],
            "groups": {name: "unreached" for name in GROUPS},
            "processes": [],
            "wire": [],
            "tenants": {},
            "runs": [],
            "notifications": [],
            "cleanup_errors": [],
            "owned_declarations": {"queues": [], "exchanges": []},
            "closed_resources": [],
            "failure": None,
        }
        self.lock = threading.RLock()
        self.boundary: Callable[[dict[str, Any]], None] | None = None
        self.server: http.server.ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None
        self.worker: subprocess.Popen[bytes] | None = None
        self.simulation: subprocess.Popen[bytes] | None = None
        self.sessions: async_sessionmaker[AsyncSession] | None = None
        self.publisher: LazyAmqpPublisher | None = None
        self.created_schema = False
        self.owned_topology = False
        self.python = [
            "uv",
            "run",
            "--no-project",
            "--python",
            str(self.root / ".venv/bin/python"),
            "python",
        ]
        self.save()

    def save(self) -> None:
        with self.lock:
            write_private(self.scratch / "results.json", self.state)
            write_private(self.scratch / "processes.json", self.state["processes"])

    def check(self, name: str, passed: bool) -> None:
        self.state["checks"].append({"name": name, "passed": bool(passed)})
        self.save()
        print(json.dumps({"check": name, "passed": bool(passed)}), flush=True)
        if not passed:
            raise AssertionError(name)

    def spawn(
        self,
        argv: list[str],
        name: str,
        *,
        env: dict[str, str] | None = None,
        stdin: bool = False,
        stdout_name: str | None = None,
        stderr_name: str | None = None,
    ) -> subprocess.Popen[bytes]:
        log = (self.scratch / (stdout_name or name + ".log")).open("ab")
        error_log = (self.scratch / stderr_name).open("ab") if stderr_name else None
        try:
            process = subprocess.Popen(
                argv,
                cwd=self.scratch,
                env=env or self.env,
                stdout=log,
                stderr=error_log or subprocess.STDOUT,
                stdin=subprocess.PIPE if stdin else subprocess.DEVNULL,
                start_new_session=True,
            )
        finally:
            log.close()
            if error_log is not None:
                error_log.close()
        self.state["processes"].append(
            {
                "name": name,
                "argv": argv,
                "pid": process.pid,
                "pgid": process.pid,
                "identity": "",
                "exit": None,
                "group_empty": False,
                "started": time.time(),
                "environment_keys": sorted((env or self.env).keys()),
            }
        )
        try:
            self.save()
            identify_child(self.state["processes"][-1], process, time.monotonic() + 5)
            self.retain_members(process)
        except BaseException:
            try:
                self.stopped(process)
            except BaseException as cleanup_error:
                self.state["cleanup_errors"].append(
                    {
                        "resource": name,
                        "error_type": type(cleanup_error).__name__,
                    }
                )
                self.save()
            raise
        return process

    def stopped(self, process: subprocess.Popen[bytes]) -> None:
        record = next(row for row in self.state["processes"] if row["pid"] == process.pid)
        try:
            stop_owned(record, process, deadline=time.monotonic() + 20)
        finally:
            record["exit"] = process.poll()
            record["cleanup_actual_end"] = time.time()
            if record["exit"] is not None and record.get("group_empty"):
                record["actual_end"] = time.time()
            self.save()

    def retain_members(self, process: subprocess.Popen[bytes]) -> None:
        record = next(row for row in self.state["processes"] if row["pid"] == process.pid)
        try:
            retain_owned(record)
        finally:
            self.save()

    def docker(self, kind: str, *fields: str) -> list[dict[str, Any]]:
        argv = [
            "docker",
            "exec",
            self.config["rabbitmq_container"],
            "rabbitmqctl",
            "-q",
            "list_" + kind,
        ]
        if kind != "connections":
            argv += ["-p", "dmc169-test"]
        completed = subprocess.run(
            argv + list(fields) + ["--formatter=json"],
            cwd=self.scratch,
            env=self.env,
            capture_output=True,
            timeout=8,
        )
        if completed.returncode:
            raise RuntimeError("scoped broker inventory failed")
        return list(json.loads(completed.stdout))

    def control(self, tenant: str, **patch: Any) -> dict[str, Any]:
        connection = http.client.HTTPConnection("127.0.0.1", self.config["postman_port"], timeout=3)
        try:
            connection.request(
                "POST",
                "/__control",
                json.dumps({"tenant": tenant, "patch": patch}),
                {
                    "x-mock-slug": "gitlab",
                    "x-proof-control": self.config["control_key"],
                    "Content-Type": "application/json",
                },
            )
            response = connection.getresponse()
            if response.status != 200:
                raise RuntimeError("local GitLab control rejected")
            return dict(json.loads(response.read()))
        finally:
            connection.close()

    def model_calls(self) -> list[dict[str, Any]]:
        connection = http.client.HTTPConnection("127.0.0.1", self.config["postman_port"], timeout=3)
        try:
            connection.request(
                "POST",
                "/__control",
                b"{}",
                {"x-mock-slug": "llm", "x-proof-control": self.config["control_key"]},
            )
            response = connection.getresponse()
            if response.status != 200:
                raise RuntimeError("local model control rejected")
            return list(json.loads(response.read())["calls"])
        finally:
            connection.close()

    def front(self) -> None:
        proof = self

        class Front(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format: str, *args: Any) -> None:
                pass

            def forward(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 <= length <= 1048576 or self.headers.get("Transfer-Encoding"):
                    self.send_error(400)
                    return
                body = self.rfile.read(length)
                host = self.headers.get("Host", "")
                origin = "https://" + host
                row: dict[str, Any] = {"method": self.command, "path": self.path, "host": host}
                if self.path.startswith("/github-reject/"):
                    row.update(kind="github", status=501)
                    proof.state["wire"].append(row)
                    self.send_error(501)
                    return
                member = (
                    "llm"
                    if self.path == "/v1/chat/completions" and self.command == "POST"
                    else "gitlab"
                )
                token = self.headers.get("PRIVATE-TOKEN")
                tenant = next(
                    (
                        name
                        for name, item in proof.config["tenants"].items()
                        if token in item["tokens"] and item["origin"] == origin
                    ),
                    None,
                )
                valid = (
                    member == "llm"
                    and origin == f"https://localhost:{proof.config['tls_port']}"
                    and self.headers.get("Authorization") == "Bearer " + proof.config["model_key"]
                ) or (
                    member == "gitlab"
                    and tenant is not None
                    and self.path.startswith("/api/v4/")
                    and self.command in {"GET", "HEAD", "POST"}
                    and not self.headers.get("Authorization")
                    and not self.headers.get("Cookie")
                )
                row.update(
                    kind=member,
                    tenant=tenant,
                    generation=None
                    if tenant is None
                    else proof.config["tenants"][tenant]["tokens"].index(token) + 1,
                )
                proof.state["wire"].append(row)
                if not valid:
                    row["status"] = 403
                    self.send_error(403)
                    return
                connection = http.client.HTTPConnection(
                    "127.0.0.1", proof.config["postman_port"], timeout=10
                )
                try:
                    headers = {
                        key: value
                        for key, value in self.headers.items()
                        if key.lower()
                        not in {
                            "connection",
                            "x-mock-slug",
                            "x-fixture-origin",
                            "x-fixture-host",
                            "transfer-encoding",
                        }
                    }
                    headers.update(
                        {"x-mock-slug": member, "x-fixture-origin": origin, "x-fixture-host": host}
                    )
                    connection.request(self.command, self.path, body=body, headers=headers)
                    response = connection.getresponse()
                    data = response.read()
                    row.update(status=response.status, bytes=len(data))
                    row["retire_gate"] = response.getheader("X-Proof-Gate") == "retire"
                    if proof.boundary is not None:
                        proof.boundary(row)
                    self.send_response(response.status)
                    for key, value in response.getheaders():
                        if key.lower() not in {
                            "content-length",
                            "connection",
                            "transfer-encoding",
                            "x-proof-gate",
                            "date",
                            "server",
                        }:
                            self.send_header(key, value)
                    self.send_header("Content-Length", str(len(data)))
                    self.send_header("Connection", "close")
                    self.end_headers()
                    if self.command != "HEAD":
                        self.wfile.write(data)
                except BaseException as error:
                    row["fixture_error"] = type(error).__name__
                    with contextlib.suppress(OSError):
                        self.send_error(502)
                finally:
                    connection.close()
                    self.close_connection = True

            do_GET = forward
            do_HEAD = forward
            do_POST = forward

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", self.config["tls_port"]), Front)
        self.server.daemon_threads = True
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.load_cert_chain(self.scratch / "server.pem", self.scratch / "server.key")
        self.server.socket = tls.wrap_socket(self.server.socket, server_side=True)
        self.thread = threading.Thread(
            target=self.server.serve_forever, name="owned-tls-front", daemon=True
        )
        self.thread.start()

    def data_sessions(self) -> async_sessionmaker[AsyncSession]:
        assert self.sessions is not None
        return self.sessions

    async def sql(self, query: str, values: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        assert self.sessions is not None
        async with self.sessions() as session:
            return [
                dict(row) for row in (await session.execute(text(query), values or {})).mappings()
            ]

    async def cli(
        self, arguments: list[str], secret: dict[str, str] | None = None
    ) -> dict[str, Any]:
        def command_line() -> dict[str, Any]:
            name = "cli-" + str(len(self.state["processes"]))
            process = self.spawn(
                self.python + ["-m", "app.gitlab_admin", *arguments], name, stdin=secret is not None
            )
            try:
                process.communicate(
                    None if secret is None else json.dumps(secret).encode(), timeout=20
                )
            except subprocess.TimeoutExpired:
                self.stopped(process)
                raise
            record = next(row for row in self.state["processes"] if row["pid"] == process.pid)
            record.update(exit=process.returncode, actual_end=time.time())
            self.save()
            if process.returncode:
                raise RuntimeError("supported operator CLI failed; private CLI log retained")
            lines = (self.scratch / (name + ".log")).read_text().splitlines()
            return dict(json.loads(next(line for line in reversed(lines) if line.startswith("{"))))

        return await asyncio.to_thread(command_line)

    async def setup_tenants(self) -> None:
        assert self.sessions is not None
        async with self.sessions.begin() as session:
            await seed_prompt_versions(session, load_prompt_assets(self.root / "review/prompts"))
        for name, config in self.config["tenants"].items():
            workspace, installation = uuid4(), uuid4()
            async with self.sessions.begin() as session:
                await session.execute(
                    text("INSERT INTO workspaces(id,name,daily_budget_usd) VALUES (:id,:name,100)"),
                    {"id": workspace, "name": "Synthetic network " + name},
                )
            await self.cli(
                [
                    "create",
                    "--workspace-id",
                    str(workspace),
                    "--installation-id",
                    str(installation),
                    "--origin",
                    config["origin"],
                ],
                {"token": config["tokens"][0], "webhook_secret": config["webhook_secret"]},
            )
            await self.cli(
                [
                    "bind-project",
                    "--installation-id",
                    str(installation),
                    "--project-id",
                    str(config["project"]),
                ]
            )
            activated = await self.cli(
                ["activate", "--installation-id", str(installation), "--expected-generation", "1"]
            )
            self.check(
                "supported CLI verified-TLS activation " + name,
                activated["intake_enabled"] and activated["generation"] == 1,
            )
            rows = await self.sql(
                "SELECT id FROM repositories WHERE provider_installation_id=:id "
                "AND external_id=:project",
                {"id": installation, "project": config["project"]},
            )
            self.state["tenants"][name] = {
                "workspace": workspace,
                "installation": installation,
                "repository": rows[0]["id"],
            }
        self.save()

    async def component_run(self, tenant: str, iid: int) -> tuple[UUID, ClaimedAttempt]:
        """Labelled component data only; full pipeline cases below use actual receipts."""
        assert self.sessions is not None
        who = self.state["tenants"][tenant]
        change, run = uuid4(), uuid4()
        async with self.sessions.begin() as session:
            await session.execute(
                text(
                    "INSERT INTO code_changes(id,repository_id,external_id,e"
                    "xternal_number,title,source_branch,target_branch,base_s"
                    "ha,head_sha,state,web_url) VALUES (:id,:repo,:external,"
                    ":iid,'Component fixture','feature','main',:base,:head,'"
                    "open',:url)"
                ),
                {
                    "id": change,
                    "repo": who["repository"],
                    "external": 900 + iid,
                    "iid": iid,
                    "base": "b" * 40,
                    "head": "a" * 40,
                    "url": self.config["tenants"][tenant]["origin"]
                    + f"/team/project/-/merge_requests/{iid}",
                },
            )
            rule = await session.scalar(
                text("SELECT id FROM rule_versions WHERE repository_id=:repo AND is_active"),
                {"repo": who["repository"]},
            )
            prompt = await session.scalar(
                text("SELECT id FROM prompt_versions WHERE key='review.system' AND is_active")
            )
            await session.execute(
                text(
                    "INSERT INTO runs(id,code_change_id,origin_provider_inst"
                    "allation_id,base_sha,base_ref,head_sha,state,trigger,id"
                    "empotency_key,engine,rule_version_id,prompt_version_id,"
                    "attempt,available_at) VALUES (:id,:change,:origin,:base"
                    ",'main',:head,'queued','manual',:key,'fast',:rule,:prom"
                    "pt,0,now())"
                ),
                {
                    "id": run,
                    "change": change,
                    "origin": who["installation"],
                    "base": "b" * 40,
                    "head": "a" * 40,
                    "key": uuid4().hex * 2,
                    "rule": rule,
                    "prompt": prompt,
                },
            )
        now = datetime.now(UTC)
        async with SqlAlchemyRunLifecycleUnitOfWork(self.data_sessions()) as uow:
            snapshot = await uow.runs.lock_for_claim(run)
            assert snapshot is not None
            attempt = await uow.runs.claim(
                run, worker_id="component", now=now, lease_until=now + LEASE
            )
            await uow.commit()
        self.state["runs"].append(str(run))
        return run, ClaimedAttempt(
            run,
            who["workspace"],
            attempt,
            "fast",
            now + FAST_ATTEMPT_DEADLINE,
            snapshot.prompt_version_id,
            snapshot.rule_version_id,
            "component",
        )

    @contextlib.asynccontextmanager
    async def attempt_http(
        self, run: UUID, claimed: ClaimedAttempt, *, untrusted: bool = False
    ) -> AsyncIterator[GitLabRunHttp]:
        import base64

        assert self.sessions is not None
        settings = GitLabRuntimeSettings.from_environment(self.env)
        authority = GitLabRequestCredentials(
            SqlAlchemyGitLabTargetReader(self.sessions),
            CredentialCipherAdapter(
                CredentialCipher(
                    active_key_id="proof",
                    keys={"proof": base64.b64decode(self.config["credential_key"])},
                )
            ),
        )
        expected = await SqlAlchemyRunRepository(
            self.sessions, allow_unscoped=True
        ).get_run_vcs_input(run)
        assert expected is not None
        checkpoint = AttemptCheckpoint(
            lambda: SqlAlchemyRunLifecycleUnitOfWork(self.data_sessions()),
            run,
            claimed.deadline,
            lambda: datetime.now(UTC),
        )
        credentials = GitLabRunCredentials(
            session_factory=self.sessions,
            authority=authority,
            run_id=run,
            expected_workspace_id=claimed.workspace_id,
            expected_input=expected,
            checkpoint=checkpoint,
        )
        tls = ssl.create_default_context(
            cafile=str(self.scratch / ("untrusted.pem" if untrusted else "ca.pem"))
        )
        async with GitLabRunHttp(
            credentials=credentials, origin_policy=settings.origin_policy, ssl_context=tls
        ) as http:
            yield http

    async def trust(self, run: UUID, claimed: ClaimedAttempt) -> None:
        assert self.sessions is not None
        expected = await SqlAlchemyRunRepository(
            self.sessions, allow_unscoped=True
        ).get_run_vcs_input(run)
        assert expected is not None
        before = len(self.state["wire"])
        with self.expect(RunCancelled, "wrong trusted workspace"):
            async with self.attempt_http(run, replace(claimed, workspace_id=uuid4())) as http:
                await http.get_canonical_merge_request()
        self.check("wrong workspace refuses before wire", len(self.state["wire"]) == before)
        with self.expect(RunFailure, "unknown CA handshake", error_code="diff_fetch_failed"):
            async with self.attempt_http(run, claimed, untrusted=True) as http:
                await FetchVcsReviewInput(
                    HttpGitLabVcsProvider(http=http), checkpoint=http.checkpoint
                ).execute(expected.locator, expected.head_sha, expected.base_sha)
        self.check("unknown CA emits no provider HTTP request", len(self.state["wire"]) == before)
        for mode in ("wrong-target", "bad-source"):
            self.control("A", mode=mode)
            with self.expect(
                RunFailure, "invalid canonical " + mode, error_code="diff_fetch_failed"
            ):
                async with self.attempt_http(run, claimed) as http:
                    await FetchVcsReviewInput(
                        HttpGitLabVcsProvider(http=http), checkpoint=http.checkpoint
                    ).execute(expected.locator, expected.head_sha, expected.base_sha)
        self.control("A", mode="complete")

    @contextlib.contextmanager
    def expect(self, kind: type[BaseException], name: str, *, error_code: str | None = None) -> Any:
        try:
            yield
        except kind as error:
            self.check(name, error_code is None or getattr(error, "error_code", None) == error_code)
        else:
            self.check(name, False)

    async def sources(self, run: UUID, claimed: ClaimedAttempt) -> None:
        assert self.sessions is not None
        value = await SqlAlchemyRunRepository(self.sessions, allow_unscoped=True).get_run_vcs_input(
            run
        )
        assert value is not None
        async with self.attempt_http(run, claimed) as http:
            trees = GitLabRepositoryTree(http=http, checkpoint=http.checkpoint)
            vcs = HttpGitLabVcsProvider(http=http)
            source = GitLabRunSource(trees=trees, vcs=vcs, run_id=run, conventions_from_base=True)
            self.check(
                "actual policy target AGENTS",
                (await source.fetch_agents_md(value.repository_id)).content == "Use uv.\n",
            )
            tree = await source.fetch_tree(value.repository_id)
            self.check(
                "complete target/start actual sizes 75/25",
                tree.listing_complete
                and tree.sizes_complete
                and derive_languages(tree.files) == {"Python": 75, "JavaScript": 25},
            )
            revision = GitLabRevisionSource(trees=trees, locator=value.locator)
            old = await revision.tree("c" * 40)
            new = await revision.tree("a" * 40)
            old_bytes = await revision.read_blob(old.blob_shas["app/old.py"])
            new_bytes = await revision.read_blob(new.blob_shas["app/proof.py"])
            self.check(
                "actual mergebase-old/source-head bytes distinct",
                old_bytes == self.manifest["blobs"]["old"]["text"].encode()
                and new_bytes == self.manifest["blobs"]["new"]["text"].encode()
                and old_bytes != new_bytes,
            )
        self.control("C", mode="keyset")
        async with self.attempt_http(run, claimed) as http:
            keyset_tree = await GitLabRevisionSource(
                trees=GitLabRepositoryTree(http=http, checkpoint=http.checkpoint),
                locator=value.locator,
            ).tree("a" * 40)
            self.check(
                "actual keyset second page complete",
                keyset_tree.complete
                and keyset_tree.blob_shas == {"app/proof.py": self.manifest["blobs"]["new"]["oid"]},
            )
        self.control("C", mode="complete")

    async def incompleteness(self) -> None:
        assert self.sessions is not None
        settings = GitLabRuntimeSettings.from_environment(self.env)
        async with httpx.AsyncClient() as client:
            gateway = build_gateway(LlmSettings.from_env(self.env), client, self.sessions)
            resources = build_gitlab_review_worker_resources(
                settings=settings, session_factory=self.sessions
            )
            for tenant, mode in (("D", "incomplete-list"), ("E", "incomplete-size")):
                run, claimed = await self.component_run(tenant, 80)
                value = await SqlAlchemyRunRepository(
                    self.sessions, allow_unscoped=True
                ).get_run_vcs_input(run)
                context = await SqlAlchemyRunRepository(
                    self.sessions, allow_unscoped=True
                ).get_run_conventions_input(run)
                assert value is not None and context is not None
                checkpoint = AttemptCheckpoint(
                    lambda: SqlAlchemyRunLifecycleUnitOfWork(self.data_sessions()),
                    run,
                    claimed.deadline,
                    lambda: datetime.now(UTC),
                )
                before = len(self.model_calls())
                self.control(tenant, mode=mode)
                async with resources.attempt(
                    claimed=claimed, expected_input=value, checkpoint=checkpoint
                ) as bundle:
                    await bundle.context_stage.begin(run)
                    model = GatewayConventionsModel(
                        gateway,
                        RunCallContext(
                            run_id=run,
                            workspace_id=claimed.workspace_id,
                            attempt=1,
                            engine="fast",
                            deadline=claimed.deadline,
                            prompt_version_id=claimed.prompt_version_id,
                            rule_version_id=claimed.rule_version_id,
                        ),
                    )
                    generate = GenerateRepoConventions(
                        PolicyFilteredConventionsSource(
                            bundle.source, bundle.context_stage.excluded
                        ),
                        model,
                        lambda: SqlAlchemyRepositoryConventionsUnitOfWork(self.data_sessions()),
                    )
                    with self.expect(
                        RequiredSourceUnavailable, "incomplete source stops real model gate " + mode
                    ):
                        await generate.execute(
                            repository_id=value.repository_id,
                            conventions_prompt=context.conventions_prompt,
                            run_id=run,
                            changed_files=("app/proof.py",),
                            rules=context.rules,
                            policy_key=bundle.context_stage.policy_key,
                        )
                self.check(
                    "incomplete has no model HTTP " + mode, len(self.model_calls()) == before
                )
                self.check(
                    "incomplete has no new SQL cache " + mode,
                    not await self.sql(
                        "SELECT id FROM repo_conventions WHERE repository_id=:id",
                        {"id": value.repository_id},
                    )
                    and not await self.sql(
                        (
                            "SELECT d.id FROM repo_convention_drafts d JOIN repo_con"
                            "ventions c ON c.id=d.repo_conventions_id WHERE c.reposi"
                            "tory_id=:id"
                        ),
                        {"id": value.repository_id},
                    ),
                )
                self.control(tenant, mode="complete")

    async def enqueue(self, tenant: str, iid: int, *, event: str | None = None) -> tuple[UUID, Any]:
        assert self.sessions is not None and self.publisher is not None
        who = self.state["tenants"][tenant]
        receipt = prepare_gitlab_receipt(
            connection_id=who["installation"],
            event_name="Merge Request Hook",
            webhook_id=event or str(uuid4()),
            payload_json=json.dumps(
                {
                    "object_kind": "merge_request",
                    "project": {"id": self.config["tenants"][tenant]["project"]},
                    "object_attributes": {"id": 900 + iid, "iid": iid, "action": "update"},
                }
            ),
        )
        settings = GitLabRuntimeSettings.from_environment(self.env)
        async with gitlab_webhook_replay_resources(
            self.sessions,
            settings=settings,
            run_publisher=self.publisher,
            connect_args={
                "options": "-csearch_path=" + self.schema,
                "application_name": self.schema + "_lock",
            },
        ) as resources:
            await resources.receiver.execute(receipt)
            result = await resources.receiver.replay_batch(limit=100)
            self.check("public durable receipt replay", result.error_count == 0)
        rows = await self.sql(
            (
                "SELECT r.id FROM runs r JOIN code_changes c ON c.id=r.c"
                "ode_change_id WHERE r.origin_provider_installation_id=:"
                "origin AND c.external_number=:iid ORDER BY r.created_at"
                " DESC"
            ),
            {"origin": who["installation"], "iid": iid},
        )
        self.check("public receipt creates original-origin Run", bool(rows))
        run = rows[0]["id"]
        if str(run) not in self.state["runs"]:
            self.state["runs"].append(str(run))
        return run, receipt

    async def wait_run(self, run: UUID, *states: str) -> dict[str, Any]:
        async with asyncio.timeout(45):
            while True:
                if self.worker is not None and self.worker.poll() is not None:
                    raise RuntimeError("production worker exited; private log retained")
                rows = await self.sql("SELECT * FROM runs WHERE id=:id", {"id": run})
                if rows and rows[0]["state"] in states:
                    return rows[0]
                if rows and rows[0]["state"] in {"failed", "skipped", "cancelled"}:
                    raise RuntimeError(
                        "unexpected terminal Run state " + str(rows[0]["error_code"])
                    )
                await asyncio.sleep(0.05)

    async def start_worker(self, github: bool) -> None:
        args = self.python + [
            str(self.root / "scripts/gitlab_network_proof.py"),
            "--config",
            str(self.scratch / "runtime.json"),
            "--worker",
        ]
        if github:
            args.append("--github")
        self.worker = self.spawn(args, "worker-github" if github else "worker-gl")
        # Consumer attachment is the public readiness boundary, not a sleep assumption.
        async with asyncio.timeout(25):
            while True:
                self.retain_members(self.worker)
                if self.worker.poll() is not None:
                    raise RuntimeError("production worker startup failed")
                queues = await asyncio.to_thread(
                    self.docker, "queues", "name", "consumers", "messages"
                )
                if any(
                    row["name"] == "review.run.fast" and row["consumers"] == 1 for row in queues
                ):
                    break
                await asyncio.sleep(0.1)
        self.retain_members(self.worker)
        await self.retain_topology()

    async def verify_success(
        self, tenant: str, iid: int, run: UUID, before: int, *, first: bool
    ) -> dict[str, Any]:
        result = await self.wait_run(run, "succeeded")
        who = self.state["tenants"][tenant]
        self.check(
            "real worker immutable state " + tenant,
            result["attempt"] == 1
            and result["origin_provider_installation_id"] == who["installation"]
            and result["base_sha"] == "b" * 40
            and result["review_body"] is not None,
        )
        findings = await self.sql(
            "SELECT path,line,rule_name,published,inline_comment FROM findings WHERE run_id=:id",
            {"id": run},
        )
        self.check(
            "literal nonempty finding survives real filters " + tenant,
            findings
            == [
                {
                    "path": "app/proof.py",
                    "line": 2,
                    "rule_name": "Error Handling Standards",
                    "published": True,
                    "inline_comment": True,
                }
            ],
        )
        published = self.control(tenant)["discussions"].get(str(iid), [])
        inline = [row for row in published if row["notes"][0].get("position")]
        summary = [row for row in published if not row["notes"][0].get("position")]
        self.check(
            "actual installed inline plus one marked summary " + tenant,
            len(inline) == len(summary) == 1
            and inline[0]["notes"][0]["position"]["new_line"] == 2
            and " summary -->" in summary[0]["notes"][0]["body"],
        )
        calls = self.model_calls()[before:]
        tasks = [row["task"] for row in calls]
        if first:
            self.check(
                "first uncached real conventions and review tasks " + tenant,
                tasks == ["RepoConventionsDraft", "ReviewOutput"],
            )
        else:
            self.check(
                "cache-aware real model tasks " + tenant,
                tasks in (["ReviewOutput"], ["RepoConventionsDraft", "ReviewOutput"]),
            )
        ledger = await self.sql(
            (
                "SELECT u.workspace_id,r.attempt,u.operation,u.tokens_in"
                " AS prompt_tokens, u.tokens_out AS completion_tokens FR"
                "OM usage_events u JOIN runs r ON r.id=u.run_id WHERE u."
                "run_id=:id ORDER BY u.created_at,u.id"
            ),
            {"id": run},
        )
        operations = [
            "conventions" if task == "RepoConventionsDraft" else "review" for task in tasks
        ]
        self.check(
            "owned shared usage ledger matches actual model HTTP " + tenant,
            [row["operation"] for row in ledger] == operations
            and all(
                row["workspace_id"] == who["workspace"]
                and row["attempt"] == 1
                and row["prompt_tokens"] == 1200
                and row["completion_tokens"] == 300
                for row in ledger
            ),
        )
        cached = await self.sql(
            "SELECT languages FROM repo_conventions WHERE repository_id=:id",
            {"id": who["repository"]},
        )
        self.check(
            "persisted target/start language 75/25 " + tenant,
            len(cached) == 1 and cached[0]["languages"] == {"Python": 75, "JavaScript": 25},
        )
        snapshots = await self.sql(
            "SELECT snapshot_id,repository_id,policy_base_sha FROM run_policy_snapshots "
            "WHERE run_id=:id",
            {"id": run},
        )
        documents = await self.sql(
            "SELECT path,presence,blob_oid,raw_sha256,text_sha256,raw_bytes,text,byte_count "
            "FROM run_policy_documents WHERE run_id=:id ORDER BY path",
            {"id": run},
        )
        expected_documents = sorted(
            [
                {
                    "path": self.manifest["paths"][key],
                    "presence": "present",
                    "blob_oid": self.manifest["blobs"][key]["oid"],
                    "raw_sha256": self.manifest["blobs"][key]["sha256"],
                    "text_sha256": self.manifest["blobs"][key]["sha256"],
                    "raw_bytes": self.manifest["blobs"][key]["text"].encode(),
                    "text": self.manifest["blobs"][key]["text"],
                    "byte_count": self.manifest["blobs"][key]["size"],
                }
                for key in ("agents", "rules")
            ],
            key=lambda document: document["path"],
        )
        self.check(
            "literal stored AGENTS/rules policy provenance " + tenant,
            len(snapshots) == 1
            and snapshots[0]["snapshot_id"] is not None
            and snapshots[0]["repository_id"] == who["repository"]
            and snapshots[0]["policy_base_sha"] == self.manifest["refs"]["start"]
            and documents == expected_documents,
        )
        self.check(
            "configured actual GH route remains uncalled",
            not any(row["kind"] == "github" for row in self.state["wire"]),
        )
        async with asyncio.timeout(2):
            while not any(
                row.get("run_id") == str(run) and row.get("status") == "succeeded"
                for row in self.state["notifications"]
            ):
                await asyncio.sleep(0.02)
        states = [
            row["status"] for row in self.state["notifications"] if row.get("run_id") == str(run)
        ]
        self.check(
            "actual run state sequence " + tenant,
            states[-3:] == ["running", "publishing", "succeeded"],
        )
        ids = self.manifest["expected"]["note_ids"][f"{tenant}:{iid}"]
        self.check(
            "literal provider discussion IDs " + tenant,
            [inline[0]["notes"][0]["id"], summary[0]["notes"][0]["id"]] == ids,
        )
        return result

    async def drain_publication(self, label: str, confirmed: float) -> dict[str, Any]:
        # Confirmed publish is insufficient: observe the active worker's ACK,
        # including in-flight deliveries, before accepting unchanged provider state.
        async with asyncio.timeout(15):
            while True:
                assert self.worker is not None and self.worker.poll() is None
                queues = await asyncio.to_thread(
                    self.docker,
                    "queues",
                    "name",
                    "consumers",
                    "messages_ready",
                    "messages_unacknowledged",
                    "messages",
                )
                retry = [
                    row
                    for row in queues
                    if row["name"] in self.config["queues"]
                    and ("retry" in row["name"] or row["name"].endswith(".dlq"))
                ]
                self.check(
                    label + " creates no retry/DLQ work",
                    all(
                        row["messages_ready"]
                        == row["messages_unacknowledged"]
                        == row["messages"]
                        == 0
                        for row in retry
                    ),
                )
                publication = [row for row in queues if row["name"] == "review.publish"]
                if (
                    len(publication) == 1
                    and publication[0]["consumers"] == 1
                    and all(
                        publication[0][key] == 0
                        for key in ("messages_ready", "messages_unacknowledged", "messages")
                    )
                ):
                    break
                await asyncio.sleep(0.05)
        return {
            "publisher_confirmed_at": confirmed,
            "drained_at": time.time(),
            "publication": publication,
            "retry_dlq": retry,
        }

    async def run_groups(self) -> None:
        assert self.sessions is not None
        await self.setup_tenants()
        a_run, a_claimed = await self.component_run("A", 80)
        c_run, c_claimed = await self.component_run("C", 12)

        async def group(name: str, limit: int, operation: Callable[[], Any]) -> None:
            self.state["groups"][name] = "running"
            self.save()
            async with asyncio.timeout(limit):
                await operation()
            self.state["groups"][name] = "passed"
            self.save()

        await group(GROUPS[0], 45, lambda: self.trust(a_run, a_claimed))
        await group(GROUPS[1], 45, lambda: self.sources(c_run, c_claimed))
        await group(GROUPS[2], 60, self.incompleteness)
        self.publisher = LazyAmqpPublisher(self.config["rabbitmq_url"])
        loop = asyncio.get_running_loop()
        observed = False

        async def sample() -> None:
            await self.sql("SELECT 1")
            observer = create_async_engine(
                self.config["database_url"],
                poolclass=NullPool,
                isolation_level="AUTOCOMMIT",
                connect_args={"application_name": self.schema + "_observer"},
            )
            try:
                async with observer.connect() as connection:
                    active = await connection.scalar(
                        text(
                            "SELECT count(*) FROM pg_stat_activity WHERE application"
                            "_name LIKE :prefix AND pid<>pg_backend_pid() AND xact_s"
                            "tart IS NOT NULL"
                        ),
                        {"prefix": self.schema + "%"},
                    )
                self.check(
                    "one real worker wire boundary has no open application SQL transaction",
                    active == 0,
                )
            finally:
                await observer.dispose()

        def boundary(row: dict[str, Any]) -> None:
            nonlocal observed
            if not observed and row["kind"] == "gitlab" and "/repository/tree" in row["path"]:
                observed = True
                asyncio.run_coroutine_threadsafe(sample(), loop).result(5)

        async def first() -> None:
            self.boundary = boundary
            await self.start_worker(False)
            before = len(self.model_calls())
            run, _ = await self.enqueue("A", 12)
            await self.verify_success("A", 12, run, before, first=True)
            self.check("qualified production-pool sample reached", observed)
            self.boundary = None
            assert self.worker is not None
            await asyncio.to_thread(self.stopped, self.worker)
            self.worker = None

        await group(GROUPS[3], 75, first)

        async def retry_redelivery() -> None:
            await self.start_worker(True)
            self.control("A", faults={"13": "once"})
            before = len(self.model_calls())
            run, original = await self.enqueue("A", 13)
            await self.verify_success("A", 13, run, before, first=False)
            state = self.control("A")
            self.check(
                "real summary HTTP500 retry keeps one inline",
                state["attempts"]["13:inline"] == 1 and state["attempts"]["13:summary"] == 2,
            )
            async with SqlAlchemyRunLifecycleUnitOfWork(self.data_sessions()) as uow:
                context = await uow.runs.publish_context(run)
            assert context is not None
            pointer = ReviewPublishPointer(
                run,
                context.head_sha,
                context.findings_hash,
                "COMMENT",
            )
            assert self.publisher is not None
            await self.publisher.publish_review(pointer)
            confirmed = time.time()
            async with gitlab_webhook_replay_resources(
                self.data_sessions(),
                settings=GitLabRuntimeSettings.from_environment(self.env),
                run_publisher=self.publisher,
                connect_args={
                    "options": "-csearch_path=" + self.schema,
                    "application_name": self.schema + "_lock",
                },
            ) as resources:
                duplicate = await resources.receiver.execute(original)
                self.check("original receipt durable duplicate", str(duplicate) == "duplicate")
            same, _ = await self.enqueue("A", 13)
            drain = await self.drain_publication("succeeded pointer redelivery", confirmed)
            terminal = await self.wait_run(run, "succeeded")
            self.state["redelivery_ack"] = {
                **drain,
                "run": str(run),
                "terminal_state": terminal["state"],
            }
            self.save()
            self.check(
                "equivalent event and ACKed pointer preserve terminal Run/markers",
                terminal["state"] == "succeeded"
                and same == run
                and self.control("A")["discussions"]["13"] == state["discussions"]["13"],
            )

        await group(GROUPS[4], 75, retry_redelivery)

        async def retirement() -> None:
            self.control("A", faults={"14": "retire"})
            revoked = False
            original_pointer: ReviewPublishPointer | None = None

            async def revoke() -> None:
                nonlocal revoked, original_pointer
                rows = await self.sql(
                    "SELECT r.id FROM runs r JOIN code_changes c ON c.id=r.code_change_id "
                    "WHERE r.origin_provider_installation_id=:origin AND c.external_number=14 "
                    "AND r.state='publishing'",
                    {"origin": self.state["tenants"]["A"]["installation"]},
                )
                assert len(rows) == 1
                async with SqlAlchemyRunLifecycleUnitOfWork(self.data_sessions()) as uow:
                    context = await uow.runs.publish_context(rows[0]["id"])
                assert context is not None
                original_pointer = ReviewPublishPointer(
                    rows[0]["id"],
                    context.head_sha,
                    context.findings_hash,
                    "COMMENT",
                )
                await self.cli(
                    [
                        "revoke-installation",
                        "--installation-id",
                        str(self.state["tenants"]["A"]["installation"]),
                    ]
                )
                self.state["retirement_boundary"] = {
                    "committed_at": time.time(),
                    "wire_offset": len(self.state["wire"]),
                    "run_id": str(original_pointer.run_id),
                    "head_sha": original_pointer.head_sha,
                    "findings_hash": original_pointer.findings_hash,
                }
                self.save()
                revoked = True

            def retire(row: dict[str, Any]) -> None:
                if row["tenant"] == "A" and row.get("retire_gate"):
                    asyncio.run_coroutine_threadsafe(revoke(), loop).result(20)

            self.boundary = retire
            run, _ = await self.enqueue("A", 14)
            cancelled = await self.wait_run(run, "cancelled")
            self.boundary = None
            self.check(
                "supported retirement fences current publication retry",
                revoked
                and cancelled["error_code"]
                in {"installation_revoked", "installation_unavailable", "cancelled_by_user"}
                and self.control("A")["attempts"]["14:summary"] == 1,
            )
            assert original_pointer is not None and original_pointer.run_id == run
            original_markers = self.control("A")["discussions"].get("14", [])
            assert self.publisher is not None
            await self.publisher.publish_review(original_pointer)
            confirmed = time.time()
            drain = await self.drain_publication("cancelled pointer redelivery", confirmed)
            still_cancelled = await self.wait_run(run, "cancelled")
            offset = self.state["retirement_boundary"]["wire_offset"]
            self.check(
                "committed retirement and ACKed original pointer remain terminal without A HTTP",
                all(
                    still_cancelled[key] == cancelled[key]
                    for key in ("state", "cancel_requested", "error_code", "head_sha")
                )
                and self.control("A")["discussions"].get("14", []) == original_markers
                and not any(
                    row["kind"] == "gitlab" and row["tenant"] == "A"
                    for row in self.state["wire"][offset:]
                ),
            )
            self.state["cancelled_redelivery_ack"] = {
                **drain,
                "run": str(run),
                "terminal_state": still_cancelled["state"],
            }
            self.save()
            before = len(self.model_calls())
            sibling, _ = await self.enqueue("B", 12)
            await self.verify_success("B", 12, sibling, before, first=True)
            self.check(
                "same numeric IDs sibling has distinct original workspace",
                self.state["tenants"]["A"]["workspace"] != self.state["tenants"]["B"]["workspace"],
            )

        await group(GROUPS[5], 75, retirement)

        async def generation_two() -> None:
            who = self.state["tenants"]["C"]
            await self.cli(
                [
                    "rotate-token",
                    "--installation-id",
                    str(who["installation"]),
                    "--expected-generation",
                    "1",
                ],
                {"token": self.config["tenants"]["C"]["tokens"][1]},
            )
            old = (
                await self.sql(
                    "SELECT state,cancel_requested FROM runs WHERE id=:id", {"id": c_run}
                )
            )[0]
            self.check(
                "rotation leaves old legitimate Run cancelled",
                old["state"] == "cancelled" and old["cancel_requested"],
            )
            self.control("C", generation=2, head2=True)
            before = len(self.model_calls())
            fresh, _ = await self.enqueue("C", 12)
            row = await self.verify_success("C", 12, fresh, before, first=True)
            self.check(
                "generation2 is new Run/new head without revival",
                fresh != c_run and row["head_sha"] == "9" * 40,
            )
            current = self.control("C")
            self.check(
                "generation2 execution uses original new credential",
                all(
                    event["generation"] == 2
                    for event in current["events"]
                    if event["path"].endswith("/discussions")
                ),
            )

        await group(GROUPS[6], 60, generation_two)
        self.check(
            "zero A provider requests after committed retirement through final scenario",
            not any(
                row["kind"] == "gitlab" and row["tenant"] == "A"
                for row in self.state["wire"][self.state["retirement_boundary"]["wire_offset"] :]
            ),
        )
        self.check(
            "no unmodelled/credential/substitution HTTP accepted",
            all(
                "fixture_error" not in row
                and (
                    row.get("status", 0) < 400
                    or row["tenant"] == "E"
                    and row["method"] == "HEAD"
                    and row["status"] == 503
                    or row["tenant"] == "A"
                    and row["method"] == "POST"
                    and row["status"] == 500
                )
                for row in self.state["wire"]
            ),
        )
        events = []
        for line in (self.scratch / "simulation.ndjson").read_text().splitlines():
            with contextlib.suppress(ValueError):
                events.append(json.loads(line))
        served = [event for event in events if event.get("event") == "request"]
        self.check("actual Postman served application HTTP", len(served) >= len(self.state["wire"]))
        self.state["postman_request_events"] = len(served)

    async def runtime(self) -> None:
        for port in (self.config["postman_port"], self.config["tls_port"]):
            with socket.socket() as candidate:
                candidate.bind(("127.0.0.1", port))
        existing = await asyncio.to_thread(self.docker, "queues", "name", "consumers", "messages")
        connections = await asyncio.to_thread(
            self.docker, "connections", "name", "vhost", "channels"
        )
        exchanges = await asyncio.to_thread(self.docker, "exchanges", "name")
        self.check(
            "reserved vhost has no queues/connections or colliding named exchanges",
            not existing
            and not any(row.get("vhost") == "dmc169-test" for row in connections)
            and not any(row["name"] in self.config["exchanges"] for row in exchanges),
        )
        self.state["admitted_absent"] = {
            "queues": list(self.config["queues"]),
            "exchanges": list(self.config["exchanges"]),
        }
        self.owned_topology = True
        self.save()
        # All artifacts copied into private cwd; no repository dotenv/ambient service settings.
        self.simulation = self.spawn(
            [
                shutil_which("postman"),
                "simulation",
                "run",
                str(self.scratch / "local.sim.yaml"),
                "--port",
                str(self.config["postman_port"]),
                "--output",
                "ndjson",
                "--no-history",
            ],
            "simulation",
            env=dict(os.environ),
            stdout_name="simulation.ndjson",
            stderr_name="simulation.err",
        )
        async with asyncio.timeout(25):
            while True:
                self.retain_members(self.simulation)
                if self.simulation.poll() is not None:
                    raise RuntimeError("Postman simulation startup failed")
                log = (self.scratch / "simulation.ndjson").read_text()
                events = []
                for line in log.splitlines():
                    with contextlib.suppress(ValueError):
                        events.append(json.loads(line))
                if any(event.get("event") == "listening" for event in events):
                    break
                await asyncio.sleep(0.1)
        self.retain_members(self.simulation)
        self.front()
        admin = create_engine(self.config["database_url"], hide_parameters=True, poolclass=NullPool)
        try:
            with admin.connect() as connection:
                existing_clients = connection.execute(
                    text(
                        "SELECT pid FROM pg_stat_activity WHERE datname=current_database() "
                        "AND backend_type='client backend' AND pid<>pg_backend_pid()"
                    )
                ).all()
                self.check("reserved PostgreSQL has no foreign clients", not existing_clients)
                connection.rollback()
                connection.execute(CreateSchema(self.schema))
                connection.commit()
                self.created_schema = True
                connection.execute(text(f'SET search_path TO "{self.schema}"'))
                connection.commit()
                migration = Config(str(self.root / "alembic.ini"))
                migration.set_main_option("script_location", str(self.root / "alembic"))
                migration.attributes["connection"] = connection
                command.upgrade(migration, "head")
        finally:
            admin.dispose()
        engine = create_async_engine(self.url, hide_parameters=True, pool_size=1, max_overflow=0)
        self.sessions = async_sessionmaker(engine, expire_on_commit=False)
        notification = await psycopg.AsyncConnection.connect(
            self.url.replace("postgresql+psycopg://", "postgresql://", 1), autocommit=True
        )
        await notification.execute("LISTEN run_updated")

        async def notifications() -> None:
            async for event in notification.notifies():
                payload = json.loads(event.payload)
                self.check(
                    "NOTIFY transports only identity and state",
                    set(payload) <= {"run_id", "workspace_id", "status"},
                )
                self.state["notifications"].append(payload)

        watcher = asyncio.create_task(notifications())
        try:
            async with asyncio.timeout(500):
                await self.run_groups()
        finally:
            self.boundary = None
            watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watcher
            await notification.close()
            self.state["closed_resources"].append("notification-listener")
            for label, close in [
                ("publisher", self.publisher.aclose if self.publisher is not None else None),
                ("worker", None),
                ("borrowed-data-pool", engine.dispose),
            ]:
                try:
                    if label == "worker" and self.worker is not None:
                        await asyncio.to_thread(self.stopped, self.worker)
                        self.worker = None
                    elif close is not None:
                        await close()
                    self.state["closed_resources"].append(label)
                except BaseException as error:
                    self.state["cleanup_errors"].append(
                        {"resource": label, "error_type": type(error).__name__}
                    )

    async def retain_topology(self) -> None:
        """Retain declarations observed during this exclusively admitted attempt.

        Every named resource was absent at admission. Also runs after a partial
        startup failure, before any deletion, so cleanup cannot adopt old exchanges.
        """
        assert self.owned_topology and "admitted_absent" in self.state
        for kind in ("queues", "exchanges"):
            rows = await asyncio.to_thread(self.docker, kind, "name")
            names = {row["name"] for row in rows}
            if kind == "queues" and names - set(self.config[kind]):
                raise RuntimeError("foreign queue appeared; refusing topology cleanup")
            retained = set(self.state["owned_declarations"][kind])
            retained.update(names & set(self.state["admitted_absent"][kind]))
            self.state["owned_declarations"][kind] = sorted(retained)
        self.save()

    async def cleanup_topology(self) -> None:
        await self.retain_topology()
        connection = await aio_pika.connect(
            self.config["rabbitmq_url"],
            client_properties={"connection_name": self.schema + "_cleanup"},
        )
        try:
            channel = await connection.channel()
            rows = await asyncio.to_thread(self.docker, "queues", "name", "consumers", "messages")
            for row in rows:
                if row["name"] not in self.state["owned_declarations"]["queues"]:
                    raise RuntimeError("foreign queue appeared; no global purge")
                queue = await channel.declare_queue(row["name"], passive=True)
                while True:
                    message = await queue.get(fail=False, timeout=1)
                    if message is None:
                        break
                    body = json.loads(message.body)
                    if body.get("run_id") not in self.state["runs"]:
                        await message.reject(requeue=True)
                        raise RuntimeError("foreign message; refusing cleanup ack")
                    await message.ack()
                await channel.queue_delete(row["name"], if_unused=True, if_empty=True)
            exchanges = await asyncio.to_thread(self.docker, "exchanges", "name")
            for row in exchanges:
                if row["name"] in self.state["owned_declarations"]["exchanges"]:
                    await channel.exchange_delete(row["name"], if_unused=False)
        finally:
            await connection.close()
            self.state["closed_resources"].append("exact-broker-topology-client")

    def teardown(self) -> None:
        self.boundary = None
        actions: list[tuple[str, Callable[[], Any]]] = [
            ("worker", lambda: self.stopped(self.worker) if self.worker is not None else None),
            ("TLS", self.close_front),
            (
                "Postman",
                lambda: self.stopped(self.simulation) if self.simulation is not None else None,
            ),
            (
                "topology",
                lambda: (
                    asyncio.run(asyncio.wait_for(self.cleanup_topology(), 15))
                    if self.owned_topology
                    else None
                ),
            ),
            ("schema", self.drop_schema),
        ]
        for label, action in actions:
            try:
                action()
                self.state["closed_resources"].append(label)
            except BaseException as error:
                self.state["cleanup_errors"].append(
                    {"resource": label, "error_type": type(error).__name__}
                )
        self.state["actual_end"] = datetime.now(UTC).isoformat()
        self.save()
        write_private(
            self.scratch / "route-counts.json",
            [
                {
                    key: row.get(key)
                    for key in ("method", "path", "kind", "tenant", "generation", "status", "bytes")
                }
                for row in self.state["wire"]
            ],
        )
        write_private(self.scratch / "scenario-results.json", self.state["groups"])

    def close_front(self) -> None:
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
        if self.thread is not None:
            self.thread.join(3)
            if self.thread.is_alive():
                raise RuntimeError("owned TLS thread remains")

    def drop_schema(self) -> None:
        if not self.created_schema:
            return
        engine = create_engine(
            self.config["database_url"], poolclass=NullPool, hide_parameters=True
        )
        try:
            with engine.begin() as connection:
                connection.execute(DropSchema(self.schema, cascade=True))
        finally:
            engine.dispose()


def shutil_which(name: str) -> str:
    import shutil

    path = shutil.which(name)
    if path is None:
        raise RuntimeError("required local executable unavailable: " + name)
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--github", action="store_true")
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    install_network_guard(config)
    if args.worker:
        # Only the parent's scoped explicit environment; never write harness state here.
        env = dict(os.environ)
        if args.github:
            env.update(
                GITHUB_APP_ID="1",
                GITHUB_APP_PRIVATE_KEY=(Path(config["scratch"]) / "github.key").read_text(),
                GITHUB_API_URL=f"https://localhost:{config['tls_port']}/github-reject",
            )
        asyncio.run(run_worker(WorkerSettings.from_environment(env)))
        return 0
    proof = Proof(config)
    code = 0
    try:
        asyncio.run(proof.runtime())
    except BaseException as error:
        code = 1
        proof.state["failure"] = type(error).__name__
        # Private artifacts only; public stdout emits neither credentials nor payloads.
        (proof.scratch / "failure.traceback").write_text(traceback.format_exc())
    finally:
        proof.teardown()
    if proof.state["cleanup_errors"]:
        code = 1
    summary = {
        "exit": code,
        "groups": proof.state["groups"],
        "checks": len(proof.state["checks"]),
        "failure_type": proof.state["failure"],
        "cleanup_errors": proof.state["cleanup_errors"],
        "qualification": (
            "Synthetic predecessor component proof; not integrated171 or live acceptance."
        ),
    }
    write_private(proof.scratch / "summary.json", summary)
    print(json.dumps(summary), flush=True)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
