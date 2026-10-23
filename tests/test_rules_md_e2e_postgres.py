"""``.review/rules.md`` of the base revision, from a signed webhook to the review (#163).

One chain per case: a signed ``pull_request`` delivery is posted to ``/webhooks/github``, the
real receiver projects it (``replay_pending``) and the app itself creates the Run and
publishes it to a real RabbitMQ; the real ``run_worker`` consumes it, loads the policy
(``policy.load``), analyses with a fake LLM and publishes to a fake GitHub. The test never
inserts a Run. Expected values are the literals of RULES_FORMAT_SPEC §3, §4, §9 and §11.

The module migrates one schema; each case starts from emptied tables with the same seed and
from a deleted review topology, so the cases stay isolated.

Opt-in: ``TEST_DATABASE_URL`` (disposable PostgreSQL) and ``TEST_RABBITMQ_URL`` (disposable
RabbitMQ: the review topology is deleted around every case); in CI ``RABBITMQ_URL``.
"""

from __future__ import annotations

import asyncio
import base64
import difflib
import hashlib
import hmac
import json
import os
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Literal, NoReturn
from uuid import UUID, uuid4

import httpx
import pytest
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import CreateSchema, DropSchema

from alembic import command
from app.bootstrap.reviews_api import (
    ReviewsApiResources,
    get_github_webhook_receipt_uow_factory,
)
from app.main import app, get_github_webhook_secret
from app.modules.integrations.webhooks.infrastructure.github_installation_tree_provider import (
    StaticGitHubInstallationAccessTokenProvider,
)
from app.modules.reviews.infrastructure.amqp import amqp_channels
from app.worker import WorkerSettings, run_worker
from tests.test_worker_queue_postgres import (
    _APP_PEM,
    LLM_ENV,
    SHORT_DELAYS,
    _reset_topology,
    run_state,
    tools,
)

HEAD = "e" * 40
BASE = "b" * 40
PR_ID = 907
# Our GitHub App, for the webhook gate and the worker alike; the green CI is another App's.
APP_ID = 42
FOREIGN_APP_ID = 7
REPO = "/repos/octo/repo"
RULES = ".review/rules.md"
WEBHOOK_SECRET = "issue163-test-webhook-secret"
EMPTY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
# Unique strings of the two changed files: each one shows whether its file reached the model.
SERVICE_MARKER = "service-marker-5d1e"
CLIENT_MARKER = "client-marker-9c2b"
_ACTIVE_STATES = frozenset({"queued", "running", "publishing"})
# Fixed seed ids: the pins of a run (the review prompt id among them) enter
# `policy_digest`, so two schemas compare only when they are seeded alike.
_WORKSPACE = UUID("00000000-0000-4000-8000-000000000001")
_INSTALLATION = UUID("00000000-0000-4000-8000-000000000002")
_REPOSITORY = UUID("00000000-0000-4000-8000-000000000003")
_RULE_VERSION = UUID("00000000-0000-4000-8000-000000000004")
_REVIEW_PROMPT = UUID("00000000-0000-4000-8000-000000000005")
_CONVENTIONS_PROMPT = UUID("00000000-0000-4000-8000-000000000006")
_CHANGED_FILES = re.compile(r"<changed_files>\n(.*?)\n</changed_files>", re.DOTALL)

_BASE_FILES = {
    "README.md": b"# Service\n",
    "app/service.py": b'def greet(name):\n    return "hi " + name\n',
    # The marker also sits in the base copy: conventions read the base files (§5.1).
    "generated/client.py": f"# {CLIENT_MARKER}\ndef call():\n    return None\n".encode(),
}
_HEAD_FILES = {
    **_BASE_FILES,
    "app/service.py": _BASE_FILES["app/service.py"]
    + f'\n\ndef mark():\n    return "{SERVICE_MARKER}"\n'.encode(),
    "generated/client.py": _BASE_FILES["generated/client.py"]
    + f'\n\ndef mark():\n    return "{CLIENT_MARKER}"\n'.encode(),
}
# The line of ``return "<marker>"`` in the head ``app/service.py``, an added line.
_FINDING_LINE = 6


def _block(*lines: str, fence: str = "```", info: str = "review-ignore") -> str:
    return "\n".join(["# Ignore", "", f"{fence}{info}", *lines, fence, ""])


VALID_RULES = (
    "# Rules\n\n## Style\n\nPrefer small functions.\n\n" + _block("generated/**")
).encode()
ALL_IGNORED_RULES = _block("app/**", "generated/**").encode()
PLAIN_RULES = b"# Rules\n\nPrefer small functions.\n"
IGNORE_EVERYTHING_RULES = _block("**/*").encode()


class Fault(StrEnum):
    """A failure of one policy read; every other GitHub route keeps answering."""

    ROOT_404 = "root-404"
    ROOT_DENIED = "root-denied"
    TREE_404 = "tree-404"
    BLOB_404 = "blob-404"
    DENIED = "denied"
    TRUNCATED = "truncated"


def _blob_sha(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\x00" % len(data) + data, usedforsecurity=False).hexdigest()


@dataclass(frozen=True)
class Revision:
    """The files of one commit; trees and blobs get Git-style ids from their content."""

    files: Mapping[str, bytes]
    # Paths listed with the symlink mode; the blob of a symlink is its target.
    symlinks: frozenset[str] = frozenset()

    def directories(self) -> set[str]:
        return {
            "/".join(parts[:depth])
            for parts in (path.split("/") for path in self.files)
            for depth in range(1, len(parts))
        }

    def tree_sha(self, directory: str) -> str:
        inside = sorted(
            (path, _blob_sha(data))
            for path, data in self.files.items()
            if path.startswith(directory + "/")
        )
        return hashlib.sha1(repr((directory, inside)).encode(), usedforsecurity=False).hexdigest()

    def directory_of(self, tree_sha: str) -> str | None:
        return next((item for item in self.directories() if self.tree_sha(item) == tree_sha), None)

    def listing(self, directory: str, *, recursive: bool) -> list[dict[str, Any]]:
        prefix = f"{directory}/" if directory else ""

        def listed(path: str) -> bool:
            return path.startswith(prefix) and (recursive or "/" not in path[len(prefix) :])

        trees = [
            {
                "path": item[len(prefix) :],
                "mode": "040000",
                "type": "tree",
                "sha": self.tree_sha(item),
            }
            for item in sorted(self.directories())
            if listed(item)
        ]
        blobs = [
            {
                "path": path[len(prefix) :],
                "mode": "120000" if path in self.symlinks else "100644",
                "type": "blob",
                "sha": _blob_sha(data),
                "size": len(data),
            }
            for path, data in sorted(self.files.items())
            if listed(path)
        ]
        return trees + blobs


def _changed_file(path: str, old: bytes | None, new: bytes) -> dict[str, Any]:
    lines = list(
        difflib.unified_diff(
            [] if old is None else old.decode().splitlines(),
            new.decode().splitlines(),
            lineterm="",
        )
    )[2:]
    additions = sum(1 for line in lines if line.startswith("+"))
    deletions = sum(1 for line in lines if line.startswith("-"))
    return {
        "filename": path,
        "status": "added" if old is None else "modified",
        "sha": _blob_sha(new),
        "additions": additions,
        "deletions": deletions,
        "changes": additions + deletions,
        "patch": "\n".join(lines),
    }


def _pull_request(files: list[dict[str, Any]]) -> dict[str, Any]:
    """PR 7 as both the webhook projection and the worker read it."""
    return {
        "id": PR_ID,
        "number": 7,
        "title": "Add a marker",
        "body": None,
        "html_url": "https://github.com/octo/repo/pull/7",
        "user": {"login": "alice"},
        "head": {"ref": "feature", "sha": HEAD, "repo": {"full_name": "octo/repo"}},
        "base": {"ref": "main", "sha": BASE, "repo": {"full_name": "octo/repo"}},
        "state": "open",
        "merged": False,
        "draft": False,
        "updated_at": "2026-10-05T10:00:00Z",
        "labels": [{"name": "ai-review"}],
        "changed_files": len(files),
        "additions": sum(item["additions"] for item in files),
        "deletions": sum(item["deletions"] for item in files),
    }


class FakeGitHub:
    """GitHub REST for the webhook projection and the worker, on one MockTransport.

    An unknown route is recorded and raises, so a test can tell a harness gap from an
    outcome of the code under test.
    """

    def __init__(self, base: Revision, head: Revision, fault: Fault | None) -> None:
        self.base = base
        self.head = head
        self.fault = fault
        self.files = [
            _changed_file(path, base.files.get(path), data)
            for path, data in sorted(head.files.items())
            if base.files.get(path) != data
        ]
        self.requests: list[str] = []
        self.unexpected: list[str] = []
        self.reviews: list[dict[str, Any]] = []
        self.check_runs: list[dict[str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(f"{request.method} {request.url.path}")
        response = self._route(request)
        if response is None:
            self.unexpected.append(f"{request.method} {request.url}")
            raise AssertionError(f"unexpected GitHub call {request.method} {request.url}")
        return response

    def _route(self, request: httpx.Request) -> httpx.Response | None:
        method, path = request.method, request.url.path
        if path == "/app/installations/17/access_tokens":
            return httpx.Response(201, json={"token": "t", "expires_at": "2099-01-01T00:00:00Z"})
        if not path.startswith(REPO + "/"):
            return None
        tail = path.removeprefix(REPO)
        if tail.startswith("/git/trees/"):
            return self._tree(tail.removeprefix("/git/trees/"), request)
        if tail.startswith("/git/blobs/"):
            return self._blob(tail.removeprefix("/git/blobs/"))
        if tail == "/pulls/7":
            return httpx.Response(200, json=_pull_request(self.files))
        if tail == "/pulls/7/files":
            return httpx.Response(200, json=self.files)
        if tail == "/pulls/7/commits":
            return httpx.Response(200, json=[])
        if tail == f"/compare/{BASE}...{HEAD}":
            return httpx.Response(200, json={"merge_base_commit": {"sha": BASE}})
        if tail == f"/commits/{HEAD}/check-suites":
            suite = {"id": 1, "head_sha": HEAD, "app": {"id": FOREIGN_APP_ID}}
            suite |= {"status": "completed", "conclusion": "success"}
            return httpx.Response(200, json={"total_count": 1, "check_suites": [suite]})
        if tail == f"/commits/{HEAD}/status":
            return httpx.Response(200, json={"sha": HEAD, "state": "pending", "total_count": 0})
        if tail == f"/commits/{HEAD}/check-runs":
            return httpx.Response(200, json={"total_count": 0, "check_runs": []})
        if tail.startswith("/check-runs"):
            if method in {"POST", "PATCH"}:
                self.check_runs.append(json.loads(request.content))
            return httpx.Response(201 if method == "POST" else 200, json={"id": 31})
        if tail == "/pulls/7/reviews" and method == "GET":
            return httpx.Response(200, json=[])
        if tail == "/pulls/7/reviews" and method == "POST":
            self.reviews.append(json.loads(request.content))
            return httpx.Response(200, json={"id": 501})
        if tail == "/pulls/7/reviews/501/comments":
            return httpx.Response(200, json=[{"id": 9001}])
        return None

    def _tree(self, sha: str, request: httpx.Request) -> httpx.Response | None:
        recursive = request.url.params.get("recursive") == "1"
        if sha == BASE and self.fault is Fault.ROOT_404:
            # The ambiguous answer of a private repository: lost access or a missing revision.
            return httpx.Response(404, json={"message": "Not Found"})
        if sha == BASE and self.fault is Fault.ROOT_DENIED:
            return httpx.Response(403, json={"message": "Resource not accessible"})
        if sha in {BASE, HEAD}:
            revision = self.base if sha == BASE else self.head
            # A cut listing of the base root: it cannot prove that `.review` is absent.
            truncated = self.fault is Fault.TRUNCATED and sha == BASE
            listing = revision.listing("", recursive=recursive)
            return httpx.Response(200, json={"sha": sha, "tree": listing, "truncated": truncated})
        for revision in (self.base, self.head):
            directory = revision.directory_of(sha)
            if directory is None:
                continue
            if directory == ".review" and self.fault is Fault.TREE_404:
                return httpx.Response(404, json={"message": "Not Found"})
            if directory == ".review" and self.fault is Fault.DENIED:
                # No `X-RateLimit-Remaining: 0` and no `Retry-After`: an access refusal.
                return httpx.Response(403, json={"message": "Resource not accessible"})
            listing = revision.listing(directory, recursive=recursive)
            return httpx.Response(200, json={"sha": sha, "tree": listing, "truncated": False})
        return None

    def _blob(self, sha: str) -> httpx.Response | None:
        for revision in (self.base, self.head):
            for path, data in revision.files.items():
                if _blob_sha(data) != sha:
                    continue
                if path == RULES and revision is self.base and self.fault is Fault.BLOB_404:
                    return httpx.Response(404, json={"message": "Not Found"})
                content = base64.b64encode(data).decode()
                return httpx.Response(
                    200,
                    json={"sha": sha, "encoding": "base64", "content": content, "size": len(data)},
                )
        return None


def _github(
    base_rules: bytes | None,
    *,
    head_rules: bytes | None | Literal["base"] = "base",
    fault: Fault | None = None,
    symlink: bool = False,
) -> FakeGitHub:
    """PR 7 changes ``app/service.py`` and ``generated/client.py``; ``None`` is no rules file.

    ``symlink`` lists the rules file of both revisions as a symlink to ``base_rules``.
    """
    head = base_rules if head_rules == "base" else head_rules
    base_files = dict(_BASE_FILES) | ({} if base_rules is None else {RULES: base_rules})
    head_files = dict(_HEAD_FILES) | ({} if head is None else {RULES: head})
    links = frozenset({RULES}) if symlink else frozenset()
    return FakeGitHub(Revision(base_files, links), Revision(head_files, links), fault)


class FakeLlm:
    """An OpenAI-compatible chat endpoint; each request body is kept as sent.

    A request the fake cannot answer is recorded and raises, so a test reports the harness
    gap instead of an odd ``llm_*`` failure of the run.
    """

    def __init__(self) -> None:
        self.tasks: list[str] = []
        self.bodies: list[str] = []
        self.problems: list[str] = []

    def _fail(self, problem: str) -> NoReturn:
        self.problems.append(problem)
        raise AssertionError(problem)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path != "/v1/chat/completions":
            self._fail(f"unexpected LLM call {request.method} {request.url}")
        body = json.loads(request.content)
        task = body["response_format"]["json_schema"]["name"]
        self.tasks.append(task)
        self.bodies.append(request.content.decode())
        paths: list[str] = []
        if task == "RepoConventionsDraft":
            # The conventions answer must list exactly the paths of <changed_files>, in order.
            prompt = next(item["content"] for item in body["messages"] if item["role"] == "user")
            listed = _CHANGED_FILES.search(prompt)
            if listed is None:
                self._fail(
                    "the RepoConventionsDraft prompt has no <changed_files> block: "
                    "the fake cannot list the files of the pull request"
                )
            paths = listed.group(1).split("\n")
        answer: dict[str, object] = (
            {
                "files": [{"path": path, "relevance": "Changed module."} for path in paths],
                "key_patterns": ["Pattern one.", "Pattern two.", "Pattern three."],
                "recommendations": [
                    f"Recommendation {n} (from: standard/correctness)"
                    for n in ("one", "two", "three", "four", "five")
                ],
            }
            if task == "RepoConventionsDraft"
            else {
                "findings": [
                    {
                        "path": "app/service.py",
                        "line": _FINDING_LINE,
                        "start_line": None,
                        "severity": "high",
                        "category": "correctness",
                        "title": "Constant result",
                        "body": "The new function always returns the same string.",
                        "suggestion": None,
                        "confidence": 0.9,
                        "rule_name": None,
                    }
                ],
                "summary": {
                    "problem": "The new function returns a constant.",
                    "done_well": "The change is small.",
                    "effort": "small",
                },
            }
        )
        return httpx.Response(
            200,
            json={
                "model": "test-model",
                "choices": [{"message": {"content": json.dumps(answer)}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1200, "completion_tokens": 300},
            },
        )


@dataclass(frozen=True)
class Stack:
    database_url: str
    rabbitmq_url: str
    schema: str


_SEED: tuple[tuple[str, dict[str, Any]], ...] = (
    (
        "INSERT INTO workspaces (id, name, daily_budget_usd) VALUES (:id, 'W', 0)",
        {"id": _WORKSPACE},
    ),
    (
        "INSERT INTO provider_installations (id, workspace_id, provider, external_id, metadata) "
        "VALUES (:id, :ws, 'github', 17, '{}'::jsonb)",
        {"id": _INSTALLATION, "ws": _WORKSPACE},
    ),
    (
        "INSERT INTO repositories (id, provider_installation_id, external_id, full_name, "
        "default_branch, web_url) VALUES (:id, :installation, 101, 'octo/repo', 'main', "
        "'https://github.test/octo/repo')",
        {"id": _REPOSITORY, "installation": _INSTALLATION},
    ),
    (
        "INSERT INTO rule_versions (id, repository_id, version, rules, checksum, is_active) "
        "VALUES (:id, :repo, 1, '[]'::jsonb, :checksum, true)",
        {"id": _RULE_VERSION, "repo": _REPOSITORY, "checksum": "a" * 64},
    ),
    (
        "INSERT INTO prompt_versions (id, key, version, content, checksum, is_active) "
        "VALUES (:id, 'review.system', 1, 'system', :checksum, true)",
        {"id": _REVIEW_PROMPT, "checksum": "c" * 64},
    ),
    (
        "INSERT INTO prompt_versions (id, key, version, content, checksum, is_active) "
        "VALUES (:id, 'review.conventions', 1, 'conventions', :checksum, true)",
        {"id": _CONVENTIONS_PROMPT, "checksum": "d" * 64},
    ),
)


@pytest.fixture(scope="module")
def stack() -> Iterator[Stack]:
    """One migrated schema for the module; ``_reset`` empties and re-seeds it per case."""
    database_url = os.environ.get("TEST_DATABASE_URL")
    # CI sets only RABBITMQ_URL; the zero-skips integration step must not skip here.
    rabbitmq_url = os.environ.get("TEST_RABBITMQ_URL") or (
        os.environ.get("RABBITMQ_URL") if os.environ.get("GITHUB_ACTIONS") == "true" else None
    )
    if database_url is None or rabbitmq_url is None:
        pytest.skip("set TEST_DATABASE_URL and TEST_RABBITMQ_URL to run the rules.md E2E tests")
    schema = f"test_rules_e2e_{uuid4().hex}"
    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            connection.execute(CreateSchema(schema))
            connection.execute(text(f'SET search_path TO "{schema}"'))
            connection.commit()
            try:
                config = Config("alembic.ini")
                config.attributes["connection"] = connection
                command.upgrade(config, "head")
                connection.commit()
                yield Stack(database_url, rabbitmq_url, schema)
            finally:
                connection.rollback()
                connection.execute(text("SET search_path TO public"))
                connection.execute(DropSchema(schema, cascade=True))
                connection.commit()
    finally:
        engine.dispose()


def _reset(stack: Stack) -> None:
    """Empty every table but ``alembic_version``, seed it again, delete the topology.

    The seed ids are fixed: the pins of a run (the review prompt id among them) enter
    ``policy_digest``, so the digests of two cases compare only on an equal seed.
    """
    engine = create_engine(
        stack.database_url, connect_args={"options": f"-csearch_path={stack.schema}"}
    )
    try:
        with engine.begin() as connection:
            # A session of the previous case that still holds a lock fails the case loudly.
            connection.execute(text("SET LOCAL lock_timeout = '10s'"))
            tables = connection.scalars(
                text(
                    "SELECT tablename FROM pg_tables "
                    "WHERE schemaname = :schema AND tablename <> 'alembic_version'"
                ),
                {"schema": stack.schema},
            ).all()
            names = ", ".join(f'"{stack.schema}"."{table}"' for table in tables)
            connection.execute(text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))
            for statement, values in _SEED:
                connection.execute(text(statement), values)
    finally:
        engine.dispose()
    asyncio.run(_reset_topology(stack.rabbitmq_url))


@dataclass(frozen=True)
class Outcome:
    state: str
    attempt: int
    error_code: str | None
    # path -> (presence, raw_sha256, byte_count) of the staged run_policy_documents rows.
    documents: dict[str, tuple[str, str | None, int | None]]
    policy_digest: str | None
    policy_load: list[Any]
    tasks: list[str]
    bodies: list[str]
    reviews: list[dict[str, Any]]
    requests: list[str]
    unexpected: list[str]
    llm_problems: list[str]


async def _deliver(resources: ReviewsApiResources, payload: dict[str, Any]) -> None:
    """Post one signed delivery through the ASGI app; it only stores the receipt."""
    body = json.dumps(payload).encode()
    signature = hmac.new(WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
    overrides = app.dependency_overrides.copy()
    app.dependency_overrides[get_github_webhook_secret] = lambda: WEBHOOK_SECRET
    app.dependency_overrides[get_github_webhook_receipt_uow_factory] = lambda: (
        resources.github_webhook_receipts
    )
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/webhooks/github",
                content=body,
                headers={
                    "X-GitHub-Event": "pull_request",
                    "X-GitHub-Delivery": "delivery-163",
                    "X-Hub-Signature-256": f"sha256={signature}",
                    "Content-Type": "application/json",
                },
            )
        assert response.status_code == 202, response.text
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(overrides)


async def _settled(
    factory: async_sessionmaker[AsyncSession],
    run_id: UUID,
    worker: asyncio.Task[None],
    timeout: float = 60,
) -> tuple[Any, ...]:
    """Poll until the run leaves the active states; a retry passes through ``queued``."""
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        if worker.done():
            # The worker never stops by itself: re-raise its crash instead of waiting it out.
            worker.result()
            raise AssertionError(f"run_worker returned before run {run_id} settled")
        current = await run_state(factory, run_id)
        if current[0] not in _ACTIVE_STATES:
            return current
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"run {run_id} stayed {current}")
        await asyncio.sleep(0.05)


async def _completed_check_run(github: FakeGitHub, timeout: float = 15) -> None:
    """Wait for the terminal check-run: the worker writes it after the last commit."""
    deadline = asyncio.get_running_loop().time() + timeout
    while not any(item.get("status") == "completed" for item in github.check_runs):
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"no completed check-run in {github.check_runs}")
        await asyncio.sleep(0.05)


async def _scenario(stack: Stack, github: FakeGitHub, llm: FakeLlm) -> Outcome:
    engine = create_async_engine(
        stack.database_url,
        connect_args={"options": f"-csearch_path={stack.schema}"},
        poolclass=NullPool,
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    client = httpx.AsyncClient(
        base_url="https://api.github.test", transport=httpx.MockTransport(github)
    )
    settings = WorkerSettings.from_environment(
        {
            "DATABASE_URL": f"{stack.database_url}?options=-csearch_path%3D{stack.schema}",
            "RABBITMQ_URL": stack.rabbitmq_url,
            "WORKER_ID": "rules-md-e2e",
            "GITHUB_APP_ID": str(APP_ID),
            "GITHUB_APP_PRIVATE_KEY": _APP_PEM,
            "GITHUB_API_URL": "https://api.github.test",
            **LLM_ENV,
        }
    )
    try:
        resources = ReviewsApiResources(engine, factory)
        async with amqp_channels(stack.rabbitmq_url, SHORT_DELAYS) as channels:
            # Composed here, not through compose_worker + sweep_once: PR #220 rewrites the
            # sweep_* functions. The Run is still created by the app's own projection.
            receiver = resources.github_delivery_receiver(
                client=client,
                token_provider=StaticGitHubInstallationAccessTokenProvider("ghs_t163"),
                bot_login="reviewer[bot]",
                run_publisher=channels.publisher,
                app_id=APP_ID,
            )
            await _deliver(
                resources,
                {
                    "action": "labeled",
                    "number": 7,
                    "installation": {"id": 17},
                    "repository": {"id": 101, "full_name": "octo/repo"},
                    "sender": {"type": "User"},
                    "label": {"name": "ai-review"},
                    "pull_request": _pull_request(github.files),
                },
            )
            # The projection creates the code change and the Run and publishes it.
            await receiver.replay_pending()
        async with factory() as session:
            runs = [
                tuple(row)
                for row in await session.execute(
                    text("SELECT id, state, trigger, message_published_at IS NOT NULL FROM runs")
                )
            ]
        assert [row[1:] for row in runs] == [("queued", "webhook", True)], runs
        run_id = runs[0][0]
        worker = asyncio.create_task(
            run_worker(
                settings,
                delays=SHORT_DELAYS,
                github_transport=httpx.MockTransport(github),
                llm_transport=httpx.MockTransport(llm),
            )
        )
        try:
            state, attempt, error_code = await _settled(factory, run_id, worker)
            await _completed_check_run(github)
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
        async with factory() as session:
            documents = {
                row[0]: (row[1], row[2], row[3])
                for row in await session.execute(
                    text(
                        "SELECT path, presence, raw_sha256, byte_count "
                        "FROM run_policy_documents WHERE run_id = :id"
                    ),
                    {"id": run_id},
                )
            }
            digest = await session.scalar(
                text("SELECT policy_digest FROM run_policy_snapshots WHERE run_id = :id"),
                {"id": run_id},
            )
        actions = await tools(factory, run_id)
    finally:
        await client.aclose()
        await engine.dispose()
    return Outcome(
        state=state,
        attempt=attempt,
        error_code=error_code,
        documents=documents,
        policy_digest=digest,
        policy_load=[response for tool, _, response in actions if tool == "policy.load"],
        tasks=llm.tasks,
        bodies=llm.bodies,
        reviews=github.reviews,
        requests=github.requests,
        unexpected=github.unexpected,
        llm_problems=llm.problems,
    )


def _review(stack: Stack, github: FakeGitHub) -> Outcome:
    """Run one case on emptied tables and a fresh review topology."""
    _reset(stack)
    try:
        outcome = asyncio.run(_scenario(stack, github, FakeLlm()))
    finally:
        asyncio.run(_reset_topology(stack.rabbitmq_url))
    assert outcome.unexpected == []
    assert outcome.llm_problems == []
    # The policy is read by tree and blob SHA of the pinned revision, never by path.
    assert not [item for item in outcome.requests if "/contents/" in item]
    return outcome


def _rules_load(outcome: Outcome) -> dict[str, Any]:
    [summary] = outcome.policy_load
    [document] = [item for item in summary["documents"] if item["path"] == RULES]
    return dict(document)


def _assert_policy_load_failed(outcome: Outcome) -> None:
    # PIPELINE_SPEC §trace: a failed `policy.load` stores `{error: {type, message}}`.
    assert outcome.policy_load
    for response in outcome.policy_load:
        assert set(response) == {"error"}, response
        assert set(response["error"]) == {"type", "message"}, response


def _assert_failed_once(outcome: Outcome, error_codes: frozenset[str]) -> None:
    assert (outcome.state, outcome.attempt) == ("failed", 1)
    assert outcome.error_code in error_codes
    _assert_policy_load_failed(outcome)
    assert outcome.tasks == []
    assert outcome.reviews == []


# §9 for a 404: `policy_revision_unavailable` ("закреплённая ревизия недоступна") or
# `policy_access_denied` ("недоступный repository"); a private repository answers 404 to a
# lost access as well (§3.1), so the code may be either, but never a missing document.
_UNREADABLE_404 = frozenset({"policy_revision_unavailable", "policy_access_denied"})
# §9: a 401/403 without rate-limit signs is `policy_access_denied`, not `cancelled`.
_ACCESS_DENIED = frozenset({"policy_access_denied"})


@pytest.mark.integration
def test_missing_rules_md_lets_the_whole_diff_reach_the_model(stack: Stack) -> None:
    outcome = _review(stack, _github(None))

    assert (outcome.state, outcome.error_code) == ("succeeded", None)
    assert outcome.documents[RULES][:2] == ("missing", None)
    assert _rules_load(outcome)["presence"] == "missing"
    assert len(outcome.tasks) >= 1
    # Without a policy both changed files reach the model: the control of the valid case.
    assert any(SERVICE_MARKER in body for body in outcome.bodies)
    assert any(CLIENT_MARKER in body for body in outcome.bodies)
    assert len(outcome.reviews) == 1
    assert outcome.reviews[0]["commit_id"] == HEAD


@pytest.mark.integration
def test_review_ignore_keeps_the_ignored_file_out_of_every_llm_call(stack: Stack) -> None:
    outcome = _review(stack, _github(VALID_RULES))

    assert (outcome.state, outcome.error_code) == ("succeeded", None)
    assert outcome.documents[RULES][0] == "present"
    assert _rules_load(outcome)["presence"] == "present"
    assert outcome.bodies
    assert all(CLIENT_MARKER not in body for body in outcome.bodies)
    assert any(SERVICE_MARKER in body for body in outcome.bodies)
    assert len(outcome.reviews) == 1
    assert outcome.reviews[0]["commit_id"] == HEAD


@pytest.mark.integration
def test_empty_rules_md_is_present_and_has_another_policy_than_a_missing_one(
    stack: Stack,
) -> None:
    missing = _review(stack, _github(None))
    empty = _review(stack, _github(b""))

    # AC2: the empty and the missing file never share the snapshot identity (§3.2).
    assert missing.policy_digest is not None and empty.policy_digest is not None
    assert empty.policy_digest != missing.policy_digest
    assert empty.documents[RULES] == ("present", EMPTY_SHA256, 0)
    assert _rules_load(empty)["presence"] == "present"
    assert (empty.state, empty.error_code) == ("succeeded", None)
    assert len(empty.reviews) == 1
    assert empty.reviews[0]["commit_id"] == HEAD
    assert missing.documents[RULES][:2] == ("missing", None)


@pytest.mark.integration
def test_rules_md_at_the_size_limit_is_present(stack: Stack) -> None:
    # 65 536 bytes is the limit itself (§3.3): the document is loaded whole.
    outcome = _review(stack, _github(b"a" * 65_536))

    assert (outcome.state, outcome.error_code) == ("succeeded", None)
    presence, _, byte_count = outcome.documents[RULES]
    assert (presence, byte_count) == ("present", 65_536)
    assert len(outcome.reviews) == 1
    assert outcome.reviews[0]["commit_id"] == HEAD


@pytest.mark.integration
def test_all_changes_ignored_skips_the_run_without_llm_or_review(stack: Stack) -> None:
    outcome = _review(stack, _github(ALL_IGNORED_RULES))

    assert (outcome.state, outcome.error_code) == ("skipped", "all_changes_ignored")
    assert outcome.tasks == []
    assert outcome.reviews == []


@pytest.mark.integration
@pytest.mark.parametrize(
    ("rules", "error_code"),
    [
        pytest.param(b"ok\n\xff\xfe", "policy_document_decode_error", id="decode"),
        # ESC (U+001B): only TAB, LF and CR are allowed below U+0020 (§3.2 item 5).
        pytest.param(b"\x1bfirst", "policy_document_control_character", id="control"),
        pytest.param(
            (_block("a/**") + _block("b/**")).encode(),
            "policy_ignore_block_duplicate",
            id="duplicate",
        ),
        pytest.param(
            b"- item\n\n  ```review-ignore\n  a/**\n  ```\n",
            "policy_ignore_block_nested",
            id="nested",
        ),
        pytest.param(b"```review-ignore\na/**\n", "policy_ignore_block_unclosed", id="unclosed"),
        pytest.param(
            _block("a/**", info="review-ignore extra").encode(),
            "policy_ignore_info_invalid",
            id="info",
        ),
        pytest.param(
            _block("ok/**", "a?.py").encode(), "policy_ignore_pattern_invalid", id="pattern"
        ),
        # 65 536 bytes is the limit itself (§3.3); one more byte is over it.
        pytest.param(b"a" * 65_537, "policy_document_too_large", id="too-large"),
        pytest.param(
            _block(*[f"d{index}/**" for index in range(257)]).encode(),
            "policy_ignore_limit_exceeded",
            id="limit",
        ),
    ],
)
def test_invalid_rules_md_fails_the_run_once_without_llm(
    stack: Stack, rules: bytes, error_code: str
) -> None:
    outcome = _review(stack, _github(rules))

    _assert_failed_once(outcome, frozenset({error_code}))


@pytest.mark.integration
def test_symlinked_rules_md_fails_the_run_once_without_llm(stack: Stack) -> None:
    # §3.1: a symlink is never dereferenced; the document must be a regular Git blob.
    outcome = _review(stack, _github(b"../docs/rules.md", symlink=True))

    _assert_failed_once(outcome, frozenset({"policy_document_type_unsupported"}))


@pytest.mark.integration
@pytest.mark.parametrize(
    ("base_rules", "fault", "error_codes"),
    [
        # The base root itself: without a `.review` entry, a 404 read as "absent" would pass.
        pytest.param(None, Fault.ROOT_404, _UNREADABLE_404, id="root-404"),
        pytest.param(None, Fault.ROOT_DENIED, _ACCESS_DENIED, id="root-denied"),
        pytest.param(PLAIN_RULES, Fault.TREE_404, _UNREADABLE_404, id="tree-404"),
        pytest.param(PLAIN_RULES, Fault.BLOB_404, _UNREADABLE_404, id="blob-404"),
        # No `X-RateLimit-Remaining: 0` and no `Retry-After` on the refusal.
        pytest.param(PLAIN_RULES, Fault.DENIED, _ACCESS_DENIED, id="denied"),
    ],
)
def test_unreadable_rules_md_fails_the_run_and_is_never_missing(
    stack: Stack, base_rules: bytes | None, fault: Fault, error_codes: frozenset[str]
) -> None:
    outcome = _review(stack, _github(base_rules, fault=fault))

    # Failing proves the read was not taken for a missing document (§3.1, §11).
    _assert_failed_once(outcome, error_codes)


@pytest.mark.integration
def test_truncated_base_tree_fails_the_run_and_is_never_missing(stack: Stack) -> None:
    # No `.review` entry in a cut listing: only completeness tells absence from a gap.
    outcome = _review(stack, _github(None, fault=Fault.TRUNCATED))

    assert outcome.state == "failed"
    # Both stay allowed until #247 ч.1 decides: the code says terminal
    # `policy_revision_unavailable`, §9 lists an incomplete load under `policy_fetch_transient`
    # (retried, so the attempt count is not pinned either).
    assert outcome.error_code in {"policy_revision_unavailable", "policy_fetch_transient"}
    _assert_policy_load_failed(outcome)
    assert outcome.tasks == []
    assert outcome.reviews == []


@pytest.mark.integration
def test_rules_md_added_by_the_head_is_not_the_policy(stack: Stack) -> None:
    outcome = _review(stack, _github(None, head_rules=IGNORE_EVERYTHING_RULES))

    assert (outcome.state, outcome.error_code) == ("succeeded", None)
    assert outcome.documents[RULES][:2] == ("missing", None)
    assert len(outcome.tasks) >= 1
    assert len(outcome.reviews) == 1


@pytest.mark.integration
def test_rules_md_changed_by_the_head_keeps_the_base_policy(stack: Stack) -> None:
    outcome = _review(stack, _github(PLAIN_RULES, head_rules=IGNORE_EVERYTHING_RULES))

    assert (outcome.state, outcome.error_code) == ("succeeded", None)
    assert outcome.documents[RULES][:2] == ("present", hashlib.sha256(PLAIN_RULES).hexdigest())
    assert len(outcome.tasks) >= 1
    assert len(outcome.reviews) == 1
