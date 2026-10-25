"""A v4-bound review through the worker composition, end to end (#130 F1-14).

``compose_worker_process`` runs one queued run with the production attempt provider (the
gateway's conventions, review and verifier models), the factory's context stage and the real
GitHub run source over in-memory HTTP transports. The run is bound to a review.system v4 row
seeded only here: F1 ships no ``review.system.v4.md`` (that is F2). The v4 run takes the
snapshot path: selector, snapshot, L2-L4 context, a single fit, a manifest per call, the
review, the verifier with the cited excerpts, ``llm.verify`` and the publication input. A v3
run in the same composition stays on the legacy path, and a retried v4 attempt keeps the pins.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bootstrap.llm_gateway import build_gateway
from app.modules.integrations.webhooks.infrastructure.github_installation_tree_provider import (
    GitHubAppInstallationAccessTokenProvider,
)
from app.modules.reviews.application.prompt_builder import file_block_id
from app.modules.reviews.application.queue_messages import ReviewPublishPointer
from app.modules.reviews.application.try_enqueue_webhook_run import (
    PendingRunMessage,
    RunPublicationKind,
)
from app.modules.reviews.infrastructure.github_run_source import GitHubRunSource
from app.modules.reviews.infrastructure.github_vcs import HttpGitHubVcsProvider
from app.modules.reviews.infrastructure.llm.settings import LlmSettings
from app.modules.reviews.infrastructure.run_repository import SqlAlchemyRunRepository
from app.modules.reviews.infrastructure.vcs_failures import ClassifiedVcsProvider
from app.worker import (
    GitHubAdapters,
    compose_worker_process,
    execution_profile_of,
    review_context_stage_factory,
)
from tests.portal_postgres import REPO_A, Env, portal_schema
from tests.test_github_vcs_provider import _pull_payload
from tests.test_llm_gateway import FALLBACK, PRIMARY, completion
from tests.test_retired_installation_postgres import DiscardTokenCache
from tests.test_review_context import _GREET_PATCH, _MODELS, _SERVICE
from tests.test_review_prompt_pins_postgres import _V4_CONTENT, _bind_v4
from tests.test_run_prompt_pins_postgres import _seed
from tests.test_run_publication_settings_postgres import _insert, _ready
from tests.test_snapshot_path_live_pin import _live_assets
from tests.test_worker_queue_postgres import SETTINGS, GitHub

_HEAD = "f" * 40  # the head of PR_NEW after ``_ready``
_REVIEW_TREE = "1" * 40
_RULES = "2" * 40
_AGENTS = "3" * 40
_SERVICE_SHA = "4" * 40
_MODELS_SHA = "5" * 40
_PATH = "pkg/service.py"
_ADDED_LINE = 12  # ``+    extra = 1`` of ``_GREET_PATCH``
_LLM = LlmSettings(primary=PRIMARY, fallback=FALLBACK)
_CONVENTIONS_ANSWER = {
    "files": [{"path": _PATH, "relevance": "changed file"}],
    "key_patterns": ["Pattern one", "Pattern two", "Pattern three"],
    "recommendations": [f"Check {index} (from: standard/correctness)" for index in range(5)],
}
_FINDING: dict[str, Any] = {
    "path": _PATH,
    "line": _ADDED_LINE,
    "start_line": None,
    "severity": "high",
    "category": "correctness",
    "title": "Added variable is unused",
    "body": "The added variable is never read.",
    "suggestion": None,
    "confidence": 0.9,
    "rule_name": None,
}
_SUMMARY = {"problem": "An added variable is unused.", "done_well": "Small.", "effort": "small"}
_EVIDENCE = [{"block_id": file_block_id(_PATH), "start_line": _ADDED_LINE, "end_line": _ADDED_LINE}]
_ROTATED = "---\nkey: review.conventions\nversion: 99\n---\nA rotated conventions prompt.\n"


@pytest.fixture
def env() -> Iterator[Env]:
    url = os.environ.get("TEST_DATABASE_URL")
    if url is None:
        pytest.skip("set TEST_DATABASE_URL to run the snapshot path end to end")
    with portal_schema(url, None) as schema:
        yield schema


@dataclass
class Queue:
    published: list[ReviewPublishPointer] = field(default_factory=list)
    retries: list[str] = field(default_factory=list)

    async def publish_confirmed(
        self, message: PendingRunMessage, *, kind: RunPublicationKind = RunPublicationKind.QUEUED
    ) -> None:
        raise AssertionError("the claimed run is already queued")

    async def publish_retry(self, message: PendingRunMessage, delay_key: str) -> None:
        self.retries.append(delay_key)

    async def publish_review(self, pointer: ReviewPublishPointer) -> None:
        self.published.append(pointer)


def _blob(sha: str, raw: bytes) -> httpx.Response:
    content = base64.b64encode(raw).decode()
    return httpx.Response(
        200, json={"sha": sha, "encoding": "base64", "content": content, "size": len(raw)}
    )


@dataclass
class GitHubHttp:
    """The GitHub REST API of PR 3 of octo/a: the policy at the base, the change at the head."""

    files_unavailable: int = 0
    calls: list[str] = field(default_factory=list)

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append(f"{request.method} {path}")
        if path.endswith("/access_tokens"):
            return httpx.Response(
                201, json={"token": "token", "expires_at": "2030-01-01T00:00:00Z"}
            )
        if "/git/trees/" in path:
            sha = path.rsplit("/", 1)[1]
            if sha == _REVIEW_TREE:
                tree = [{"path": "rules.md", "mode": "100644", "type": "blob", "sha": _RULES}]
            elif sha == _HEAD:
                tree = [
                    {"path": _PATH, "mode": "100644", "type": "blob", "sha": _SERVICE_SHA},
                    {"path": "pkg/models.py", "mode": "100644", "type": "blob", "sha": _MODELS_SHA},
                ]
            else:
                tree = [
                    {"path": ".review", "mode": "040000", "type": "tree", "sha": _REVIEW_TREE},
                    {"path": "AGENTS.md", "mode": "100644", "type": "blob", "sha": _AGENTS},
                ]
            return httpx.Response(200, json={"tree": tree, "truncated": False})
        if "/git/blobs/" in path:
            sha = path.rsplit("/", 1)[1]
            raw = {
                _RULES: b"Be careful.\n",
                _AGENTS: b"# AGENTS\n\nUse uv.\n",
                _SERVICE_SHA: _SERVICE.encode(),
                _MODELS_SHA: _MODELS.encode(),
            }[sha]
            return _blob(sha, raw)
        if path.endswith("/pulls/3"):
            payload = _pull_payload(changed_files=1, head_sha=_HEAD)
            payload["number"] = 3
            return httpx.Response(200, json=payload)
        if path.endswith("/pulls/3/commits"):
            return httpx.Response(200, json=[])
        if path.endswith("/pulls/3/files"):
            if self.files_unavailable:
                self.files_unavailable -= 1
                return httpx.Response(503, json={"message": "unavailable"})
            file = {
                "filename": _PATH,
                "status": "modified",
                "sha": _SERVICE_SHA,
                "additions": 1,
                "deletions": 0,
                "changes": 1,
                "patch": _GREET_PATCH,
            }
            return httpx.Response(200, json=[file])
        if "/compare/" in path:
            return httpx.Response(200, json={"merge_base_commit": {"sha": "c" * 40}})
        raise AssertionError(f"unexpected GitHub request {request.method} {request.url}")


@dataclass
class ModelHttp:
    """The model provider: each call answers the request it gets, in the order of the path."""

    kinds: list[str]
    bodies: list[dict[str, Any]] = field(default_factory=list)

    def handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.bodies.append(body)
        kind = self.kinds.pop(0)
        if kind == "conventions":
            answer: dict[str, Any] = _CONVENTIONS_ANSWER
        elif kind == "review-v2":
            answer = {"findings": [{**_FINDING, "evidence_refs": _EVIDENCE}], "summary": _SUMMARY}
        elif kind == "review-v1":
            answer = {"findings": [_FINDING], "summary": _SUMMARY}
        else:
            assert kind == "verifier"
            ids = re.findall(r'candidate_id="([^"]+)"', body["messages"][-1]["content"])
            answer = {
                "decisions": [
                    {
                        "candidate_id": candidate,
                        "status": "supported",
                        "reason": "The cited added line is never read.",
                        "evidence_refs": _EVIDENCE,
                        "requirement_refs": [],
                    }
                    for candidate in ids
                ]
            }
        return completion(json.dumps(answer))


@dataclass(frozen=True)
class Observed:
    run_id: UUID
    outcomes: list[str]
    state: tuple[Any, ...]
    tools: list[str]
    actions: dict[str, list[tuple[Any, Any]]]
    manifests: list[tuple[UUID, str, int]]
    snapshots: list[UUID]
    pins: tuple[Any, ...]
    conventions_prompt: str
    bodies: list[dict[str, Any]]
    queue: Queue


async def _rows(
    factory: async_sessionmaker[AsyncSession], sql: str, run_id: UUID
) -> list[tuple[Any, ...]]:
    async with factory() as session:
        return [tuple(row) for row in await session.execute(text(sql), {"id": run_id})]


async def _scenario(
    factory: async_sessionmaker[AsyncSession], *, v4: bool, retry: bool
) -> Observed:
    await _ready(factory)
    await _seed(factory, _live_assets())
    async with factory.begin() as session:
        run_id = await _insert(session)
        await session.execute(
            text("UPDATE repositories SET enabled=true,installation_available=true WHERE id=:id"),
            {"id": REPO_A},
        )
    if v4:
        await _bind_v4(factory, run_id)
    queue = Queue()
    github_http = GitHubHttp(files_unavailable=1 if retry else 0)
    review_kind = "review-v2" if v4 else "review-v1"
    model_http = ModelHttp(["conventions", review_kind, *(["verifier"] if v4 else [])])
    outcomes: list[str] = []
    async with (
        httpx.AsyncClient(
            base_url="https://api.github.test", transport=httpx.MockTransport(github_http.handle)
        ) as client,
        httpx.AsyncClient(transport=httpx.MockTransport(model_http.handle)) as llm_client,
    ):
        tokens = GitHubAppInstallationAccessTokenProvider(
            client=client,
            app_id="42",
            private_key="unused",
            jwt_encoder=lambda claims, key: "jwt",
            cache=DiscardTokenCache(),
            now=lambda: datetime(2026, 10, 10, tzinfo=UTC).timestamp(),
        )
        github_vcs = HttpGitHubVcsProvider(client=client, token_provider=tokens)
        vcs = ClassifiedVcsProvider(github_vcs)
        runs = SqlAlchemyRunRepository(factory, allow_unscoped=True)
        github = GitHub()
        # The adapters of ``worker.github_adapters`` with recording publication fakes.
        adapters = GitHubAdapters(
            context_stage=review_context_stage_factory(
                client=client,
                tokens=tokens,
                vcs=github_vcs,
                runs=runs,
                session_factory=factory,
                redis=None,
                execution_profile=execution_profile_of(_LLM),
            ),
            vcs=vcs,
            run_source=lambda run: GitHubRunSource(
                client=client,
                token_provider=tokens,
                vcs=vcs,
                runs=runs,
                run_id=run,
                conventions_from_base=True,
            ),
            check_runs=github,
            reviews=github,
            eligibility=github,
        )
        process = compose_worker_process(
            settings=SETTINGS,
            session_factory=factory,
            queue=queue,
            github=adapters,
            gateway=build_gateway(_LLM, llm_client, factory),
            attempt_deadline=timedelta(minutes=10),
        )
        outcomes.append(await process.handle_run.execute(run_id))
        if retry:
            async with factory.begin() as session:
                # A newer conventions row goes live between the attempts; the run keeps its pin.
                await session.execute(
                    text(
                        "UPDATE prompt_versions SET is_active=false WHERE key='review.conventions'"
                    )
                )
                await session.execute(
                    text(
                        "INSERT INTO prompt_versions (id,key,version,content,checksum,is_active) "
                        "VALUES (gen_random_uuid(),'review.conventions',99,:content,:sum,true)"
                    ),
                    {"content": _ROTATED, "sum": "9" * 64},
                )
                await session.execute(
                    text(
                        "UPDATE runs SET available_at=clock_timestamp()-interval '1 minute' "
                        "WHERE id=:id"
                    ),
                    {"id": run_id},
                )
            outcomes.append(await process.handle_run.execute(run_id))
    actions: dict[str, list[tuple[Any, Any]]] = {}
    tools: list[str] = []
    for tool, request, response in await _rows(
        factory,
        "SELECT tool, request, response FROM run_actions WHERE run_id=:id ORDER BY index",
        run_id,
    ):
        tools.append(tool)
        actions.setdefault(tool, []).append((request, response))
    [state] = await _rows(
        factory, "SELECT state::text, error_code, attempt FROM runs WHERE id=:id", run_id
    )
    [pins] = await _rows(
        factory,
        "SELECT prompt_version_id, conventions_prompt_version_id, verifier_prompt_version_id "
        "FROM runs WHERE id=:id",
        run_id,
    )
    [(conventions_prompt,)] = await _rows(
        factory,
        "SELECT p.content FROM prompt_versions p JOIN runs r "
        "ON p.id = r.conventions_prompt_version_id WHERE r.id=:id",
        run_id,
    )
    return Observed(
        run_id=run_id,
        outcomes=outcomes,
        state=state,
        tools=tools,
        actions=actions,
        manifests=[
            (row[0], row[1], row[2])
            for row in await _rows(
                factory,
                "SELECT policy_snapshot_id, stage, attempt FROM context_manifests "
                "WHERE run_id=:id ORDER BY created_at",
                run_id,
            )
        ],
        snapshots=[
            row[0]
            for row in await _rows(
                factory, "SELECT snapshot_id FROM run_policy_snapshots WHERE run_id=:id", run_id
            )
        ],
        pins=pins,
        conventions_prompt=conventions_prompt,
        bodies=model_http.bodies,
        queue=queue,
    )


def _observe(env: Env, *, v4: bool, retry: bool = False) -> Observed:
    async def scenario() -> Observed:
        engine = env.engine()
        try:
            return await _scenario(
                async_sessionmaker(engine, expire_on_commit=False), v4=v4, retry=retry
            )
        finally:
            await engine.dispose()

    return asyncio.run(scenario())


# The trace of a v4 attempt (PIPELINE_SPEC §7). The second ``context.build`` is the review
# prompt read's own step, which F3 removes; ``llm.verify`` sits in the publishing transaction
# right before ``review.postprocess``.
_SNAPSHOT_PATH_STEPS = [
    "policy.load",
    "vcs.fetch_diff",
    "context.build",
    "context.manifest",
    "llm.call",
    "llm.repo_conventions",
    "context.build",
    "context.manifest",
    "llm.call",
    "context.manifest",
    "llm.call",
    "llm.review_output",
    "llm.verify",
    "review.postprocess",
]
_LEGACY_STEPS = [
    "policy.load",
    "vcs.fetch_diff",
    "context.build",
    "llm.call",
    "llm.repo_conventions",
    "context.build",
    "llm.call",
    "llm.review_output",
    "review.postprocess",
]


def _assert_snapshot_path(
    observed: Observed, *, attempt: int, earlier: tuple[str, ...] = ()
) -> None:
    run_id, (review_pin, _, verifier_pin) = observed.run_id, observed.pins
    assert observed.state == ("publishing", None, attempt)
    assert observed.tools == [*earlier, *_SNAPSHOT_PATH_STEPS]
    # Selector and snapshot: the run's one stored snapshot carries every call of the run.
    [snapshot_id] = observed.snapshots
    assert [policy["policy_snapshot_id"] for _, policy in observed.actions["policy.load"]] == [
        str(snapshot_id)
    ] * (1 + earlier.count("policy.load"))
    assert observed.manifests == [
        (snapshot_id, stage, attempt) for stage in ("conventions", "review", "verification")
    ]
    calls = [request for request, _ in observed.actions["llm.call"]]
    manifests = observed.actions["context.manifest"]
    assert [call["stage"] for call in calls] == ["conventions", "review", "verification"]
    for call, (manifest_request, manifest) in zip(calls, manifests, strict=True):
        assert call["policy_snapshot_id"] == str(snapshot_id)
        assert (call["call_id"], call["manifest_id"]) == (
            manifest_request["call_id"],
            manifest["manifest_id"],
        )
        # A single fit: the estimate of the sent prompt is the manifest's own count.
        assert call["input_tokens_estimate"] == manifest["input_tokens"]
    assert calls[1]["prompt_version_id"] == str(review_pin)
    assert calls[2]["prompt_version_id"] == str(verifier_pin)
    conventions_body, review_body, verifier_body = observed.bodies
    assert review_body["messages"][0]["content"] == _V4_CONTENT
    # L2-L4: the review gets the context excerpts of the stage, counted in its manifest.
    review_prompt = review_body["messages"][-1]["content"]
    assert re.findall(
        r'<excerpt block_id="[^"]+" level="(L[234])" path="([^"]+)"', review_prompt
    ) == [
        ("L4", "pkg/models.py"),
        ("L3", _PATH),
    ]
    assert set(manifests[1][1]["tokens_by_role"]) == {
        "diff",
        "ast_excerpt",
        "source_excerpt",
        "policy_document",
    }
    # The verifier gets the candidate's cited excerpt: the reference and the shown line.
    verifier_prompt = verifier_body["messages"][-1]["content"]
    block = file_block_id(_PATH)
    assert f'<ref block_id="{block}" start_line="12" end_line="12" />' in verifier_prompt
    assert f'<file block_id="{block}" path="{_PATH}"' in verifier_prompt
    assert '<line n="12" type="added">    extra = 1</line>' in verifier_prompt
    [(verify, decisions)] = observed.actions["llm.verify"]
    assert verify["manifest_id"] == manifests[2][1]["manifest_id"]
    assert verify["accepted_call_id"] == calls[2]["call_id"]
    assert [decision["status"] for decision in decisions["decisions"]] == ["supported"]
    # The publication input: the verified finding, stored and handed to the publisher.
    [(_, output)] = observed.actions["llm.review_output"]
    assert [
        (finding["path"], finding["line"], finding["evidence_refs"])
        for finding in output["findings"]
    ] == [(_PATH, _ADDED_LINE, [{"block_id": block, "start_line": 12, "end_line": 12}])]
    [(_, postprocess)] = observed.actions["review.postprocess"]
    assert postprocess["inline"] == 1
    assert [(pointer.run_id, pointer.findings_hash) for pointer in observed.queue.published] == [
        (run_id, postprocess["findings_hash"])
    ]
    # The conventions call takes the run's pinned row, not the active one (R1).
    assert conventions_body["messages"][0]["content"] == observed.conventions_prompt


@pytest.mark.integration
def test_a_v4_run_takes_the_snapshot_path_through_the_worker_composition(env: Env) -> None:
    observed = _observe(env, v4=True)

    assert observed.outcomes == ["ack"]
    assert observed.queue.retries == []
    _assert_snapshot_path(observed, attempt=1)


@pytest.mark.integration
def test_a_retried_v4_attempt_keeps_the_snapshot_and_the_pins(env: Env) -> None:
    # The first attempt stores the snapshot, then the diff read fails and the run is retried.
    # A newer conventions row goes live in between: the second attempt keeps the stored
    # snapshot and the run's pinned prompts.
    observed = _observe(env, v4=True, retry=True)

    assert observed.outcomes == ["ack", "ack"]
    assert len(observed.queue.retries) == 1
    _assert_snapshot_path(observed, attempt=2, earlier=("policy.load", "vcs.fetch_diff"))
    assert all(_ROTATED not in body["messages"][0]["content"] for body in observed.bodies)


@pytest.mark.integration
def test_a_v3_run_stays_on_the_legacy_path_in_the_same_composition(env: Env) -> None:
    observed = _observe(env, v4=False)

    assert observed.outcomes == ["ack"]
    assert observed.state == ("publishing", None, 1)
    assert observed.tools == _LEGACY_STEPS
    assert observed.manifests == []
    calls = [request for request, _ in observed.actions["llm.call"]]
    assert [call["operation"] for call in calls] == ["conventions", "review"]
    # No snapshot-path keys on a legacy call (E-3) and no block ids in the legacy prompt.
    assert all("manifest_id" not in call and "policy_snapshot_id" not in call for call in calls)
    assert len(observed.bodies) == 2
    assert "block_id=" not in observed.bodies[1]["messages"][-1]["content"]
    [(_, output)] = observed.actions["llm.review_output"]
    assert [(finding["path"], finding["line"]) for finding in output["findings"]] == [
        (_PATH, _ADDED_LINE)
    ]
    assert all("evidence_refs" not in finding for finding in output["findings"])
    assert [pointer.run_id for pointer in observed.queue.published] == [observed.run_id]
