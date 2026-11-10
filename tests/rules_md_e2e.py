"""What the ``.review/rules.md`` E2E tests share: the chain, its fakes and seeds (#163).

One chain per case: a signed ``pull_request`` delivery is posted to ``/webhooks/github``, the
real receiver projects it (``replay_pending``) and the app itself creates the Run and
publishes it to a real RabbitMQ; the real ``run_worker`` consumes it, loads the policy
(``policy.load``), analyses with a fake LLM and publishes to a fake GitHub. The test never
inserts a Run. Expected values are the literals of RULES_FORMAT_SPEC §3, §4, §9 and §11.

Two seeds pick the review path through the active review prompt the run pins: ``legacy``
(review.system v1) and ``v4`` (the packaged review.system v4 with conventions v2 and verifier
v2: the snapshot path, where the rules.md text reaches the reviewer and the verifier). A case
that fails while ``policy.load`` reads the documents runs on the v4 seed only.

Each test module gets one migrated database; each case starts from emptied tables with the same seed
and from a deleted review topology, so the cases stay isolated. The ``stack`` and ``seed``
fixtures reach a test file through ``pytest_plugins``; the rest is imported.

Opt-in: ``TEST_DATABASE_URL`` (disposable PostgreSQL) and ``TEST_RABBITMQ_URL`` (disposable
RabbitMQ: the review topology is deleted around every case); in CI ``RABBITMQ_URL``.
"""

from __future__ import annotations

import asyncio
import difflib
import hashlib
import hmac
import json
import os
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, NoReturn
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.bootstrap.reviews_api import (
    ReviewsApiResources,
    get_github_webhook_receipt_uow_factory,
)
from app.bootstrap.seed_prompts import load_prompt_assets
from app.main import app, get_github_webhook_secret
from app.modules.integrations.webhooks.infrastructure.github_installation_tree_provider import (
    StaticGitHubInstallationAccessTokenProvider,
)
from app.modules.reviews.application.prompt_builder import file_block_id
from app.modules.reviews.infrastructure.amqp import amqp_channels
from app.worker import WorkerSettings, run_worker
from tests.github_raw_blobs import blob_answer
from tests.template_database import migrated_database
from tests.worker_queue_postgres import (
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


# Unique strings of the two changed files: each one shows whether its file reached the model.
SERVICE_MARKER = "service-marker-5d1e"
SERVICE_PATH = "app/service.py"
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
_VERIFIER_PROMPT = UUID("00000000-0000-4000-8000-000000000007")
_CHANGED_FILES = re.compile(r"<changed_files>\n(.*?)\n</changed_files>", re.DOTALL)
_CANDIDATE = re.compile(r'<candidate candidate_id="([^"]+)" path="([^"]+)" line="(\d+)"')


class Seed(StrEnum):
    """The active review prompt of the seed, which picks the review path of the run."""

    LEGACY = "legacy"
    V4 = "v4"


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


# `&`, `<` and `>` reach the model escaped (RFS §8.1).
VALID_RULES = (
    "# Rules\n\n## Style\n\nPrefer small functions.\nKeep `a < b && b > c` checks explicit.\n\n"
    + _block("generated/**")
).encode()

PLAIN_RULES = b"# Rules\n\nPrefer small functions.\n"


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
    # Paths listed as a gitlink: mode 160000, type commit.
    gitlinks: frozenset[str] = frozenset()

    def directories(self) -> set[str]:
        return {
            "/".join(parts[:depth])
            for parts in (path.split("/") for path in (*self.files, *self.gitlinks))
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
        gitlinks = [
            {
                "path": path[len(prefix) :],
                "mode": "160000",
                "type": "commit",
                "sha": _blob_sha(path.encode()),
            }
            for path in sorted(self.gitlinks)
            if listed(path)
        ]
        return trees + blobs + gitlinks


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
            return self._blob(tail.removeprefix("/git/blobs/"), request)
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

    def _blob(self, sha: str, request: httpx.Request) -> httpx.Response | None:
        for revision in (self.base, self.head):
            for path, data in revision.files.items():
                if _blob_sha(data) != sha:
                    continue
                if path == RULES and revision is self.base and self.fault is Fault.BLOB_404:
                    return httpx.Response(404, json={"message": "Not Found"})
                return blob_answer(request, data)
        return None


def _github(
    base_rules: bytes | None,
    *,
    head_rules: bytes | None | Literal["base"] = "base",
    fault: Fault | None = None,
    symlink: str | None = None,
    agents: bytes | None = None,
    files: Mapping[str, bytes] | None = None,
    gitlink: str | None = None,
) -> FakeGitHub:
    """PR 7 changes ``app/service.py`` and ``generated/client.py``; ``None`` is no rules file.

    ``symlink`` lists that policy document of both revisions as a symlink, its bytes the
    target; ``agents`` is a root ``AGENTS.md`` the PR leaves unchanged. ``files`` are more
    paths of both revisions; ``gitlink`` lists that path of both revisions as a submodule.
    """
    head = base_rules if head_rules == "base" else head_rules
    common = dict(files or {}) | ({} if agents is None else {"AGENTS.md": agents})
    base_files = _BASE_FILES | common | ({} if base_rules is None else {RULES: base_rules})
    head_files = _HEAD_FILES | common | ({} if head is None else {RULES: head})
    links = frozenset() if symlink is None else frozenset({symlink})
    gitlinks = frozenset() if gitlink is None else frozenset({gitlink})
    return FakeGitHub(
        Revision(base_files, links, gitlinks), Revision(head_files, links, gitlinks), fault
    )


class FakeLlm:
    """An OpenAI-compatible chat endpoint; each request body is kept as sent.

    It answers by the response schema: conventions list the changed paths, a review flags
    the added line of ``app/service.py`` (a v2 review cites it in the file's block), and the
    verifier supports every candidate. A request the fake cannot answer is recorded and
    raises, so a test reports the harness gap instead of an odd ``llm_*`` failure of the run.
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
        prompt = _user_message(body)
        if task == "RepoConventionsDraft":
            answer = self._conventions(prompt)
        elif task == "ReviewOutput":
            answer = {"findings": [_FINDING], "summary": _SUMMARY}
        elif task == "ReviewOutputV2":
            answer = self._review_v2(prompt)
        elif task == "VerificationOutput":
            answer = self._verification(prompt)
        else:
            self._fail(f"the fake has no answer for the {task} schema")
        return httpx.Response(
            200,
            json={
                "model": "test-model",
                "choices": [{"message": {"content": json.dumps(answer)}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1200, "completion_tokens": 300},
            },
        )

    def _conventions(self, prompt: str) -> dict[str, object]:
        # The conventions answer must list exactly the paths of <changed_files>, in order.
        listed = _CHANGED_FILES.search(prompt)
        if listed is None:
            self._fail(
                "the RepoConventionsDraft prompt has no <changed_files> block: "
                "the fake cannot list the files of the pull request"
            )
        return {
            "files": [
                {"path": path, "relevance": "Changed module."}
                for path in listed.group(1).split("\n")
            ],
            "key_patterns": ["Pattern one.", "Pattern two.", "Pattern three."],
            "recommendations": [
                f"Recommendation {n} (from: standard/correctness)"
                for n in ("one", "two", "three", "four", "five")
            ],
        }

    def _review_v2(self, prompt: str) -> dict[str, object]:
        # A v2 finding needs evidence: the added line in the block of its file.
        block = file_block_id(SERVICE_PATH)
        if f'<file block_id="{block}" path="{SERVICE_PATH}"' not in prompt:
            self._fail(f"the ReviewOutputV2 prompt does not show {SERVICE_PATH}")
        evidence = {"block_id": block, "start_line": _FINDING_LINE, "end_line": _FINDING_LINE}
        return {"findings": [{**_FINDING, "evidence_refs": [evidence]}], "summary": _SUMMARY}

    def _verification(self, prompt: str) -> dict[str, object]:
        # One decision per candidate of the prompt, citing the line the candidate stands on.
        candidates = _CANDIDATE.findall(prompt)
        if not candidates:
            self._fail("the VerificationOutput prompt has no candidate")
        return {
            "decisions": [
                {
                    "candidate_id": candidate,
                    "status": "supported",
                    "reason": "The added function returns a constant.",
                    "evidence_refs": [
                        {
                            "block_id": file_block_id(path),
                            "start_line": int(line),
                            "end_line": int(line),
                        }
                    ],
                    "requirement_refs": [],
                }
                for candidate, path, line in candidates
            ]
        }


_FINDING: dict[str, object] = {
    "path": SERVICE_PATH,
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
_SUMMARY = {
    "problem": "The new function returns a constant.",
    "done_well": "The change is small.",
    "effort": "small",
}


def _user_message(body: Mapping[str, Any]) -> str:
    """The last user message of a chat request: the input envelope of the call."""
    return str(
        next(item["content"] for item in reversed(body["messages"]) if item["role"] == "user")
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
)
_PROMPT_ROW = (
    "INSERT INTO prompt_versions (id, key, version, content, checksum, is_active) "
    "VALUES (:id, :key, :version, :content, :checksum, true)"
)
# The packaged prompt files, as the deploy seed stores them (content and source checksum).
_PACKAGED = {
    (asset.key, asset.version): asset
    for asset in load_prompt_assets(Path(__file__).resolve().parents[1] / "review" / "prompts")
}


def _packaged_row(prompt_id: UUID, key: str, version: int) -> dict[str, Any]:
    asset = _PACKAGED[(key, version)]
    return {
        "id": prompt_id,
        "key": key,
        "version": version,
        "content": asset.content,
        "checksum": asset.checksum,
    }


# One active row per key: the projection pins the active review.system row (the repository
# has no override) and the active conventions and verifier rows (or NULL).
_PROMPTS: dict[Seed, tuple[dict[str, Any], ...]] = {
    Seed.LEGACY: (
        {
            "id": _REVIEW_PROMPT,
            "key": "review.system",
            "version": 1,
            "content": "system",
            "checksum": "c" * 64,
        },
        {
            "id": _CONVENTIONS_PROMPT,
            "key": "review.conventions",
            "version": 1,
            "content": "conventions",
            "checksum": "d" * 64,
        },
    ),
    # The v4 run needs a verifier pin whose checksum is the packaged file's: the verifier
    # builder reads that file and compares (policy_snapshot_integrity_error otherwise).
    Seed.V4: (
        _packaged_row(_REVIEW_PROMPT, "review.system", 4),
        _packaged_row(_CONVENTIONS_PROMPT, "review.conventions", 2),
        _packaged_row(_VERIFIER_PROMPT, "review.verifier", 2),
    ),
}


@pytest.fixture(scope="module")
def stack() -> Iterator[Stack]:
    """One migrated database for the module; ``_reset`` empties and re-seeds it per case."""
    database_url = os.environ.get("TEST_DATABASE_URL")
    # CI sets only RABBITMQ_URL; the zero-skips integration step must not skip here.
    rabbitmq_url = os.environ.get("TEST_RABBITMQ_URL") or (
        os.environ.get("RABBITMQ_URL") if os.environ.get("GITHUB_ACTIONS") == "true" else None
    )
    if database_url is None or rabbitmq_url is None:
        pytest.skip("set TEST_DATABASE_URL and TEST_RABBITMQ_URL to run the rules.md E2E tests")
    schema = f"test_rules_e2e_{uuid4().hex}"
    with migrated_database(database_url, schema) as clone_url:
        yield Stack(clone_url, rabbitmq_url, schema)


@pytest.fixture(params=list(Seed), ids=str)
def seed(request: pytest.FixtureRequest) -> Seed:
    """A catalog case on both review paths: ``[legacy]`` and ``[v4]``."""
    chosen: Seed = request.param
    return chosen


def _reset(stack: Stack, seed: Seed) -> None:
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
            for row in _PROMPTS[seed]:
                connection.execute(text(_PROMPT_ROW), row)
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
    # run_policy_snapshots rows of the run: a failed load stores none.
    snapshots: int
    policy_load: list[Any]
    # Every run_actions row of the run, in order: (tool, response).
    steps: list[tuple[str, Any]]
    tasks: list[str]
    bodies: list[str]
    reviews: list[dict[str, Any]]
    requests: list[str]
    unexpected: list[str]
    llm_problems: list[str]
    # The terminal check-run the worker wrote last.
    check_run: dict[str, Any]


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


async def _completed_check_run(github: FakeGitHub, timeout: float = 15) -> dict[str, Any]:
    """Wait for the terminal check-run: the worker writes it after the last commit."""
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        completed = [item for item in github.check_runs if item.get("status") == "completed"]
        if completed:
            return completed[-1]
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
            check_run = await _completed_check_run(github)
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
            snapshots = await session.scalar(
                text("SELECT count(*) FROM run_policy_snapshots WHERE run_id = :id"),
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
        snapshots=int(snapshots or 0),
        policy_load=[response for tool, _, response in actions if tool == "policy.load"],
        steps=[(tool, response) for tool, _, response in actions],
        tasks=llm.tasks,
        bodies=llm.bodies,
        reviews=github.reviews,
        requests=github.requests,
        unexpected=github.unexpected,
        llm_problems=llm.problems,
        check_run=check_run,
    )


def _review(stack: Stack, github: FakeGitHub, seed: Seed) -> Outcome:
    """Run one case on emptied tables and a fresh review topology."""
    _reset(stack, seed)
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


_AT_LIMIT = b"a" * 65_536


# §3.3 counts the two rendered blocks together, `(len + 2) // 3` per block by counter/v2
# (§7.2). One line of letters each: the rules.md block adds 106 characters of markup to its
# text and the AGENTS.md block 124, so these bytes render to 6 000 tokens each, 12 000
# together. Either document alone is half the budget: only the sum can reach it.
_RULES_HALF = b"r" * 17_894
_AGENTS_HALF = b"g" * 17_876
