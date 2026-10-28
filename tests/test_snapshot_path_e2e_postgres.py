"""A v4-bound review through the worker composition, end to end (#130 F1-14).

``compose_worker_process`` runs one queued run with the production attempt provider (the
gateway's conventions, review and verifier models), the factory's context stage and the real
GitHub run source over in-memory HTTP transports. The run is bound to a review.system v4 row
with the bytes of the packaged ``review.system.v4.md`` (F2), inactive here. The v4 run takes the
snapshot path: selector, snapshot, L2-L4 context counted with the pinned counter, a single
fit, a manifest per call, the review citing the diff and the L3 excerpt, the verifier shown
that excerpt, ``llm.verify`` and the publication input. A v3 run in the same composition
stays on the legacy path. A retried v4 attempt keeps the snapshot and every pinned prompt
while newer rows go live, and a provider failure after a model call sends the review to the
fallback on a manifest row of its own. The v4 prompt shows no retrieved section. A run bound
to the packaged ``review.system.v5.md`` (#144) always shows one: empty with RAG off; with a
ready graph index the review shows and records the caller of the changed function, the
verifier shows it again with the same index commit, and a retry before the accepted answer
retrieves from a newer index publication (D8). A v4 run in flight when the v5 seed makes v5
live keeps its v4 prompt: no retrieved section, and a ready index is recorded as not used.
A verifier answer citing a range the diff block does not show, or a rule finding without a
requirement of its rule, earns the attempt's one free slot: the fallback, or a bounded repair
without a fallback model; the recovered answer publishes.
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
from hashlib import sha256
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
from tests.portal_postgres import REPO_A, REPO_B, Env, portal_schema
from tests.test_code_index_store_postgres import C1, C2, _build, _enable, _Index
from tests.test_github_vcs_provider import _pull_payload
from tests.test_llm_gateway import FALLBACK, PRIMARY, completion
from tests.test_prompt_seeding import _up_to
from tests.test_rag_review_context_postgres import _USER, _user_file
from tests.test_retired_installation_postgres import DiscardTokenCache
from tests.test_review_context import _GREET_PATCH, _MODELS, _SERVICE
from tests.test_review_prompt_pins_postgres import _V4_CONTENT, _V5_CONTENT, _bind_v4, _bind_v5
from tests.test_run_prompt_pins_postgres import _packaged, _seed
from tests.test_run_publication_settings_postgres import _insert, _ready
from tests.test_snapshot_path_live_pin import _live_assets
from tests.test_worker_queue_postgres import SETTINGS, GitHub

_HEAD = "f" * 40  # the head of PR_NEW after ``_ready``
_REVIEW_TREE = "1" * 40
# The Git blob ids of the two policy documents below: the raw policy read checks them (#168).
_RULES = "ce0b715d4d8a24915dca62ff7df9fd0255e44312"
_AGENTS = "b45208a635f8eff05a44ab890c4650a41a108041"
_SERVICE_SHA = "4" * 40
_MODELS_SHA = "5" * 40
_TEST_SHA = "6" * 40
_PATH = "pkg/service.py"
# A changed test file: weighted below a source file, so it gets an L2 window, never L3.
_TEST_PATH = "tests/test_service.py"
_TEST_FILE = "def test_greet():\n    value = 1\n    assert value\n"
_TEST_PATCH = "@@ -1,2 +1,3 @@\n def test_greet():\n+    value = 1\n     assert value"
_ADDED_LINE = 12  # ``+    extra = 1`` of ``_GREET_PATCH``
# ``pkg/app.py`` calls the ``greet`` the PR changes: the graph index reaches it (#144).
_APP_PATH = "pkg/app.py"
_APP_SHA = "7" * 40
_CALL_LINE = 5  # ``    return greet(user_id)`` of ``_USER``
_LLM = LlmSettings(primary=PRIMARY, fallback=FALLBACK)
_CONVENTIONS_ANSWER = {
    "files": [{"path": path, "relevance": "changed file"} for path in (_PATH, _TEST_PATH)],
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
_ROTATED_REVIEW = "---\nkey: review.system\nversion: 98\n---\nA rotated review prompt.\n"
_ROTATED_VERIFIER = "---\nkey: review.verifier\nversion: 97\n---\nA rotated verifier prompt.\n"


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
    # The head tree also holds ``pkg/app.py``, the caller the graph index retrieves.
    rag: bool = False
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
                    {"path": _TEST_PATH, "mode": "100644", "type": "blob", "sha": _TEST_SHA},
                ]
                if self.rag:
                    tree.append(
                        {"path": _APP_PATH, "mode": "100644", "type": "blob", "sha": _APP_SHA}
                    )
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
                _TEST_SHA: _TEST_FILE.encode(),
                _APP_SHA: _USER.encode(),
            }[sha]
            if request.headers["Accept"] == "application/vnd.github.raw+json":
                return httpx.Response(200, content=raw)
            return _blob(sha, raw)
        if path.endswith("/pulls/3"):
            payload = _pull_payload(changed_files=2, head_sha=_HEAD)
            payload["number"] = 3
            return httpx.Response(200, json=payload)
        if path.endswith("/pulls/3/commits"):
            return httpx.Response(200, json=[])
        if path.endswith("/pulls/3/files"):
            if self.files_unavailable:
                self.files_unavailable -= 1
                return httpx.Response(503, json={"message": "unavailable"})
            files = [
                {
                    "filename": path,
                    "status": "modified",
                    "sha": sha,
                    "additions": 1,
                    "deletions": 0,
                    "changes": 1,
                    "patch": patch,
                }
                for path, sha, patch in (
                    (_PATH, _SERVICE_SHA, _GREET_PATCH),
                    (_TEST_PATH, _TEST_SHA, _TEST_PATCH),
                )
            ]
            return httpx.Response(200, json=files)
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
        if kind == "unavailable":
            # One provider outage of the call. Within the slots E-1 keeps for the verifier the
            # primary gets no second try: the gateway goes to the fallback.
            return httpx.Response(503, json={"error": {"message": "unavailable"}})
        if kind == "conventions":
            answer: dict[str, Any] = _CONVENTIONS_ANSWER
        elif kind in ("review-v2", "review-v2-rule"):
            # The review cites the added line in the diff and in the L3 excerpt of the file,
            # and the call of ``greet`` in a retrieved block when the prompt shows one.
            prompt = body["messages"][-1]["content"]
            [l3] = re.findall(r'<excerpt block_id="([^"]+)" level="L3"', prompt)
            cited = [
                *_EVIDENCE,
                {"block_id": l3, "start_line": _ADDED_LINE, "end_line": _ADDED_LINE},
                *(
                    {"block_id": rag, "start_line": _CALL_LINE, "end_line": _CALL_LINE}
                    for rag in re.findall(r'<excerpt block_id="([^"]+)" level="RAG"', prompt)
                ),
            ]
            # ``review-v2-rule``: the finding names the repository rules it breaks.
            rule = ".review/rules.md" if kind == "review-v2-rule" else None
            finding = {**_FINDING, "rule_name": rule, "evidence_refs": cited}
            answer = {"findings": [finding], "summary": _SUMMARY}
        elif kind == "review-v1":
            answer = {"findings": [_FINDING], "summary": _SUMMARY}
        else:
            assert kind in ("verifier", "verifier-unshown", "verifier-rule")
            # The verifier prompt, also under the repair turn of a repair call.
            prompt = body["messages"][1]["content"]
            ids = re.findall(r'candidate_id="([^"]+)"', prompt)
            evidence = _EVIDENCE
            requirements: list[dict[str, Any]] = []
            if kind == "verifier-unshown":
                # Lines past the end of the shown hunk (RIGHT 9-13): never in the prompt.
                evidence = [{"block_id": file_block_id(_PATH), "start_line": 22, "end_line": 24}]
            elif kind == "verifier-rule":
                # The rule finding cites the shown lines of the repository rules.
                [rules] = re.findall(
                    r'<repository_rules block_id="policy-rules"[^>]*>\n(.*?)\n</repository_rules>',
                    prompt,
                    flags=re.DOTALL,
                )
                shown = [int(n) for n in re.findall(r'<line n="(\d+)">', rules)]
                requirements = [
                    {
                        "block_id": "policy-rules",
                        "start_line": min(shown),
                        "end_line": max(shown),
                        "check_id": None,
                    }
                ]
            answer = {
                "decisions": [
                    {
                        "candidate_id": candidate,
                        "status": "supported",
                        "reason": "The cited added line is never read.",
                        "evidence_refs": evidence,
                        "requirement_refs": requirements,
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
    manifest_blocks: list[list[dict[str, Any]]]
    manifest_ids: list[UUID]
    snapshots: list[UUID]
    pins: tuple[Any, ...]
    conventions_prompt: str
    verifier_prompt: str
    bodies: list[dict[str, Any]]
    queue: Queue
    payloads: list[tuple[Any, ...]]


async def _rows(
    factory: async_sessionmaker[AsyncSession], sql: str, run_id: UUID
) -> list[tuple[Any, ...]]:
    async with factory() as session:
        return [tuple(row) for row in await session.execute(text(sql), {"id": run_id})]


async def _scenario(
    factory: async_sessionmaker[AsyncSession],
    *,
    version: int,
    retry: bool,
    rag: bool,
    v5_seeded: bool = False,
    llm: LlmSettings = _LLM,
    review_kind: str | None = None,
    verifier_kinds: tuple[str, ...] = ("verifier",),
) -> Observed:
    """``version``: the review.system version the run is bound to; v3 is the seeded live row.
    ``v5_seeded``: the run is created under the seeded live v4 row, then the v5 seed makes v5
    live before the worker takes the run. ``llm``: the models of the composition;
    ``review_kind`` and ``verifier_kinds``: how the review and the verifier calls answer."""
    await _ready(factory)
    await _seed(factory, _up_to(_packaged(), 4) if v5_seeded else _live_assets())
    async with factory.begin() as session:
        run_id = await _insert(session)
        await session.execute(
            text(
                "UPDATE repositories SET enabled=true,installation_available=true,"
                "rag_enabled=:rag WHERE id=:id"
            ),
            {"id": REPO_A, "rag": rag},
        )
    if v5_seeded:
        await _seed(factory, _packaged())
        [pinned_and_live] = await _rows(
            factory,
            "SELECT p.version, (SELECT version FROM prompt_versions "
            "WHERE key='review.system' AND is_active) "
            "FROM runs r JOIN prompt_versions p ON p.id = r.prompt_version_id WHERE r.id=:id",
            run_id,
        )
        assert pinned_and_live == (4, 5)
    index = _Index(factory, REPO_A, REPO_B)
    if rag:
        # A ready graph index of the default branch at C1 knows the caller of ``greet``.
        await _enable(index, REPO_A)
        await _build(index, REPO_A, C1, _user_file())
    if version == 4 and not v5_seeded:
        await _bind_v4(factory, run_id)
    elif version == 5:
        await _bind_v5(factory, run_id)
    snapshot_path = version >= 4
    queue = Queue()
    github_http = GitHubHttp(files_unavailable=1 if retry and not rag else 0, rag=rag)
    if review_kind is None:
        review_kind = "review-v2" if snapshot_path else "review-v1"
    if retry and rag:
        # The first attempt fails before any answer is accepted: the provider refuses its
        # review call on the primary and on the fallback model. The second attempt keeps the
        # conventions of the first.
        kinds = ["conventions", "unavailable", "unavailable", review_kind]
    else:
        # On the retried attempt the provider refuses the review call once.
        kinds = ["conventions", *(["unavailable"] if retry else []), review_kind]
    model_http = ModelHttp([*kinds, *(verifier_kinds if snapshot_path else [])])
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
                execution_profile=execution_profile_of(llm),
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
            gateway=build_gateway(llm, llm_client, factory),
            attempt_deadline=timedelta(minutes=10),
        )
        outcomes.append(await process.handle_run.execute(run_id))
        if retry:
            if rag:
                # A newer publication of the index, at C2, lands between the attempts.
                await _build(index, REPO_A, C2, _user_file(), base=C1)
            async with factory.begin() as session:
                if not rag:
                    # Newer conventions, review and verifier rows go live between the
                    # attempts; the run keeps its pins.
                    await session.execute(
                        text(
                            "UPDATE prompt_versions SET is_active=false WHERE key IN "
                            "('review.conventions','review.system','review.verifier')"
                        )
                    )
                    for key, version, content in (
                        ("review.conventions", 99, _ROTATED),
                        ("review.system", 98, _ROTATED_REVIEW),
                        ("review.verifier", 97, _ROTATED_VERIFIER),
                    ):
                        await session.execute(
                            text(
                                "INSERT INTO prompt_versions "
                                "(id,key,version,content,checksum,is_active) "
                                "VALUES (gen_random_uuid(),:key,:version,:content,:sum,true)"
                            ),
                            {
                                "key": key,
                                "version": version,
                                "content": content,
                                "sum": sha256(content.encode()).hexdigest(),
                            },
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
    [(verifier_prompt,)] = await _rows(
        factory,
        "SELECT p.content FROM prompt_versions p JOIN runs r "
        "ON p.id = r.verifier_prompt_version_id WHERE r.id=:id",
        run_id,
    )
    payloads = await _rows(
        factory, "SELECT schema_version, summary FROM context_payloads WHERE run_id=:id", run_id
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
        manifest_blocks=[
            row[0]
            for row in await _rows(
                factory,
                "SELECT blocks FROM context_manifests WHERE run_id=:id ORDER BY created_at",
                run_id,
            )
        ],
        manifest_ids=[
            row[0]
            for row in await _rows(
                factory,
                "SELECT id FROM context_manifests WHERE run_id=:id ORDER BY created_at",
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
        verifier_prompt=verifier_prompt,
        bodies=model_http.bodies,
        queue=queue,
        payloads=payloads,
    )


def _observe(
    env: Env,
    *,
    version: int,
    retry: bool = False,
    rag: bool = False,
    v5_seeded: bool = False,
    llm: LlmSettings = _LLM,
    review_kind: str | None = None,
    verifier_kinds: tuple[str, ...] = ("verifier",),
) -> Observed:
    async def scenario() -> Observed:
        engine = env.engine()
        try:
            return await _scenario(
                async_sessionmaker(engine, expire_on_commit=False),
                version=version,
                retry=retry,
                rag=rag,
                v5_seeded=v5_seeded,
                llm=llm,
                review_kind=review_kind,
                verifier_kinds=verifier_kinds,
            )
        finally:
            await engine.dispose()

    return asyncio.run(scenario())


# The trace of a v4 attempt, as built (G aligns the canon, PIPELINE_SPEC §7, with it). The
# stage's ``context.build`` is the attempt's only one (#168 part 6); ``llm.verify`` sits in
# the publishing transaction right before ``review.postprocess``.
_SNAPSHOT_PATH_STEPS = [
    "policy.load",
    "vcs.fetch_diff",
    "context.build",
    "context.manifest",
    "llm.call",
    "llm.repo_conventions",
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
    "llm.call",
    "llm.review_output",
    "review.postprocess",
]


def _assert_snapshot_path(
    observed: Observed,
    *,
    attempt: int,
    earlier: tuple[str, ...] = (),
    review_fallback: bool = False,
    version: int = 4,
    rag: bool = False,
) -> None:
    run_id, (review_pin, _, verifier_pin) = observed.run_id, observed.pins
    assert observed.state == ("publishing", None, attempt)
    steps = list(_SNAPSHOT_PATH_STEPS)
    if rag:
        # RAG is on: the stage records the index after the context build.
        steps.insert(steps.index("context.build") + 1, "context.rag")
    stages = ["conventions", "review", "verification"]
    if review_fallback:
        # The fallback is one more review call with a manifest of its own.
        at = steps.index("llm.call", steps.index("llm.repo_conventions")) + 1
        steps[at:at] = ["context.manifest", "llm.call"]
        stages.insert(1, "review")
    assert observed.tools == [*earlier, *steps]
    # Selector and snapshot: the run's one stored snapshot carries every call of the run.
    [snapshot_id] = observed.snapshots
    assert [policy["policy_snapshot_id"] for _, policy in observed.actions["policy.load"]] == [
        str(snapshot_id)
    ] * (1 + earlier.count("policy.load"))
    assert observed.manifests == [(snapshot_id, stage, attempt) for stage in stages]
    # No block of this run is retrieved (no code index is seeded, or the pinned prompt shows
    # none): every stored block keeps the selection metadata of a retrieved one (ADR 0001 §9)
    # as None.
    assert len(observed.manifest_blocks) == len(stages)
    assert all(observed.manifest_blocks)
    assert {
        (block["selection_reason"], block["index_commit_sha"], block["edge_kind"])
        for blocks in observed.manifest_blocks
        for block in blocks
    } == {(None, None, None)}
    # R19: the stage counts the blocks of a v4 run with the counter the profile pins
    # (counter/v2, three characters per token): L1 the two hunks, L4 the ``User`` signature
    # and docstring, L3 the whole service file, L2 the test file's window.
    [(_, stage_build)] = observed.actions["context.build"]
    assert stage_build["tokens_by_level"] == {"L1": 67, "L4": 15, "L3": 66, "L2": 16}
    # The run's one context_payloads row is that build's response (schema 2).
    assert observed.payloads == [(2, stage_build)]
    calls = [request for request, _ in observed.actions["llm.call"]]
    manifests = observed.actions["context.manifest"]
    assert [call["stage"] for call in calls] == stages
    for call, (manifest_request, manifest) in zip(calls, manifests, strict=True):
        assert call["policy_snapshot_id"] == str(snapshot_id)
        assert (call["call_id"], call["manifest_id"]) == (
            manifest_request["call_id"],
            manifest["manifest_id"],
        )
        # A single fit: the estimate of the sent prompt is the manifest's own count.
        assert call["input_tokens_estimate"] == manifest["input_tokens"]
    bodies = observed.bodies
    if review_fallback:
        # The failed primary call and the fallback: one prompt, two calls, two rows.
        assert [call["kind"] for call in calls[1:3]] == ["primary", "fallback"]
        assert calls[1]["manifest_id"] != calls[2]["manifest_id"]
        assert bodies[1]["messages"] == bodies[2]["messages"]
        calls, manifests, bodies = ([items[0], *items[2:]] for items in (calls, manifests, bodies))
    assert calls[1]["prompt_version_id"] == str(review_pin)
    assert calls[2]["prompt_version_id"] == str(verifier_pin)
    conventions_body, review_body, verifier_body = bodies
    assert review_body["messages"][0]["content"] == (_V5_CONTENT if version == 5 else _V4_CONTENT)
    # L2-L4: the review gets the context excerpts of the stage, counted in its manifest.
    review_prompt = review_body["messages"][-1]["content"]
    # v4 has no retrieved section; v5 always shows one, here empty.
    if version == 5:
        assert "</context_excerpts>\n<retrieved_excerpts>\n\n</retrieved_excerpts>\n" in (
            review_prompt
        )
    else:
        assert "<retrieved_excerpts>" not in review_prompt
    excerpts = re.findall(
        r'<excerpt block_id="([^"]+)" level="(L[234])" path="([^"]+)"', review_prompt
    )
    assert [(level, path) for _, level, path in excerpts] == [
        ("L4", "pkg/models.py"),
        ("L3", _PATH),
        ("L2", _TEST_PATH),
    ]
    (l4, _, _), (l3, _, _), _ = excerpts
    # The snapshot-path L4 id: the head and a digest, never the path.
    assert re.fullmatch(r"L4:f{12}:[0-9a-f]{16}", l4)
    assert set(manifests[1][1]["tokens_by_role"]) == {
        "diff",
        "ast_excerpt",
        "source_excerpt",
        "policy_document",
    }
    # The verifier gets the candidate's citations: the diff line and the cited L3 excerpt.
    verifier_prompt = verifier_body["messages"][-1]["content"]
    block = file_block_id(_PATH)
    assert f'<ref block_id="{block}" start_line="12" end_line="12" />' in verifier_prompt
    assert f'<ref block_id="{l3}" start_line="12" end_line="12" />' in verifier_prompt
    assert f'<file block_id="{block}" path="{_PATH}"' in verifier_prompt
    assert '<line n="12" type="added">    extra = 1</line>' in verifier_prompt
    assert f'<excerpt block_id="{l3}" level="L3" path="{_PATH}"' in verifier_prompt
    assert '<line n="12">    extra = 1</line>' in verifier_prompt
    # Only the cited excerpt: neither the L4 signature nor the L2 window.
    assert verifier_prompt.count("<excerpt ") == 1
    [(verify, decisions)] = observed.actions["llm.verify"]
    assert verify["manifest_id"] == manifests[2][1]["manifest_id"]
    assert verify["accepted_call_id"] == calls[2]["call_id"]
    assert [decision["status"] for decision in decisions["decisions"]] == ["supported"]
    # The publication input: the verified finding, stored and handed to the publisher.
    [(_, output)] = observed.actions["llm.review_output"]
    assert [
        (finding["path"], finding["line"], finding["evidence_refs"])
        for finding in output["findings"]
    ] == [
        (
            _PATH,
            _ADDED_LINE,
            [
                {"block_id": block, "start_line": 12, "end_line": 12},
                {"block_id": l3, "start_line": 12, "end_line": 12},
            ],
        )
    ]
    [(_, postprocess)] = observed.actions["review.postprocess"]
    assert postprocess["inline"] == 1
    assert [(pointer.run_id, pointer.findings_hash) for pointer in observed.queue.published] == [
        (run_id, postprocess["findings_hash"])
    ]
    # Every call takes the run's pinned row, not the active one (R1).
    assert conventions_body["messages"][0]["content"] == observed.conventions_prompt
    # The verifier is sent the body of its pinned row, without the front matter.
    assert verifier_body["messages"][0]["content"] == (
        observed.verifier_prompt.split("\n---\n", 1)[1].strip()
    )


@pytest.mark.integration
def test_a_v4_run_takes_the_snapshot_path_through_the_worker_composition(env: Env) -> None:
    observed = _observe(env, version=4)

    assert observed.outcomes == ["ack"]
    assert observed.queue.retries == []
    _assert_snapshot_path(observed, attempt=1)


@pytest.mark.integration
def test_a_retried_v4_attempt_keeps_the_snapshot_and_the_pins(env: Env) -> None:
    # The first attempt stores the snapshot, then the diff read fails and the run is retried.
    # Newer conventions, review and verifier rows go live in between: the second attempt keeps
    # the stored snapshot and the run's pinned prompts. Its review call fails once at the
    # provider, after the conventions call, and the fallback answers.
    observed = _observe(env, version=4, retry=True)

    assert observed.outcomes == ["ack", "ack"]
    assert len(observed.queue.retries) == 1
    _assert_snapshot_path(
        observed, attempt=2, earlier=("policy.load", "vcs.fetch_diff"), review_fallback=True
    )
    rotated = (_ROTATED, _ROTATED_REVIEW, _ROTATED_VERIFIER)
    assert all(
        prompt not in body["messages"][0]["content"]
        for body in observed.bodies
        for prompt in rotated
    )


@pytest.mark.integration
def test_a_v3_run_stays_on_the_legacy_path_in_the_same_composition(env: Env) -> None:
    observed = _observe(env, version=3)

    assert observed.outcomes == ["ack"]
    assert observed.state == ("publishing", None, 1)
    assert observed.tools == _LEGACY_STEPS
    assert observed.manifests == []
    # A legacy run keeps the composition's counter (four characters per token) for the
    # same blocks.
    [(_, stage_build)] = observed.actions["context.build"]
    assert stage_build["tokens_by_level"] == {"L1": 50, "L4": 11, "L3": 49, "L2": 12}
    assert observed.payloads == [(2, stage_build)]
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


@pytest.mark.integration
def test_a_v5_run_without_rag_shows_an_empty_retrieved_section(env: Env) -> None:
    # RAG is off for the repository: no ``context.rag`` step, and the bound v5 prompt still
    # shows its retrieved section, empty.
    observed = _observe(env, version=5)

    assert observed.outcomes == ["ack"]
    assert observed.queue.retries == []
    _assert_snapshot_path(observed, attempt=1, version=5)


@pytest.mark.integration
def test_a_v4_run_in_flight_when_v5_is_seeded_keeps_the_v4_prompt(env: Env) -> None:
    # The run is created under the live v4 row; the v5 seed makes v5 live before the worker
    # takes it. RAG is on with a ready index: the run keeps its v4 pin and body, shows no
    # retrieved section, and ``context.rag`` records the usable index as not used.
    observed = _observe(env, version=4, rag=True, v5_seeded=True)

    assert observed.outcomes == ["ack"]
    assert observed.queue.retries == []
    [(rag_request, rag_response)] = observed.actions["context.rag"]
    assert (rag_request["index_status"], rag_request["indexed_commit_sha"]) == ("ready", C1)
    assert rag_response == {"outcome": "not_ready", "reason": "prompt_version"}
    _assert_snapshot_path(observed, attempt=1, rag=True)


@dataclass(frozen=True)
class RetrievedRow:
    """A stored call manifest and the retrieved blocks it records."""

    manifest_id: str
    stage: str
    attempt: int
    blocks: list[dict[str, Any]]


def _retrieved_rows(observed: Observed) -> list[RetrievedRow]:
    return [
        RetrievedRow(
            str(manifest_id),
            stage,
            attempt,
            [block for block in blocks if block["source_role"] == "retrieved_excerpt"],
        )
        for manifest_id, (_, stage, attempt), blocks in zip(
            observed.manifest_ids, observed.manifests, observed.manifest_blocks, strict=True
        )
    ]


def _retrieved_section(prompt: str) -> str:
    [section] = re.findall(r"<retrieved_excerpts>\n(.*?)\n</retrieved_excerpts>", prompt, re.S)
    return str(section)


def _assert_retrieved_and_shown_again(
    observed: Observed,
    *,
    index_commit_sha: str,
    review: RetrievedRow,
    verification: RetrievedRow,
    review_body: dict[str, Any],
    verifier_body: dict[str, Any],
) -> None:
    """The accepted attempt: the review shows and records the caller of ``greet`` retrieved
    from the index at ``index_commit_sha``; the verifier shows that block again, recorded with
    the same metadata (ADR 0001 §9, CVS §5.2)."""
    assert review_body["messages"][0]["content"] == _V5_CONTENT
    section = _retrieved_section(review_body["messages"][-1]["content"])
    [rag] = re.findall(
        rf'<excerpt block_id="([^"]+)" level="RAG" path="{_APP_PATH}" revision="{_HEAD}" '
        r'side="RIGHT" representation="exact_lines">',
        section,
    )
    # The snapshot-path RAG id: the head and a digest, never the path (D3).
    assert re.fullmatch(r"RAG:f{12}:[0-9a-f]{16}", rag)
    assert f'<line n="{_CALL_LINE}">    return greet(user_id)</line>' in section
    [block] = review.blocks
    assert block == {
        "block_id": rag,
        "source_role": "retrieved_excerpt",
        "source_kind": "source_code",
        "path": _APP_PATH,
        "revision": _HEAD,
        "side": "RIGHT",
        "content_sha256": None,
        "start_line": 1,
        "end_line": _CALL_LINE,
        "shown_line_count": 3,
        "shown_ranges": [[1, 1], [4, _CALL_LINE]],
        "tokens": block["tokens"],
        "repository_id": str(REPO_A),
        "truncated": False,
        "selection_reason": "rag_reverse_edge",
        "index_commit_sha": index_commit_sha,
        "edge_kind": "uses_imported",
    }
    tokens_by_role = {
        response["manifest_id"]: response["tokens_by_role"]
        for _, response in observed.actions["context.manifest"]
    }
    # AC11: the manifest counts the retrieved block under its own role, no contract change.
    assert set(tokens_by_role[review.manifest_id]) == {
        "diff",
        "ast_excerpt",
        "source_excerpt",
        "policy_document",
        "retrieved_excerpt",
    }
    assert tokens_by_role[review.manifest_id]["retrieved_excerpt"] == block["tokens"]
    # The candidate cites the block, so the verifier is shown it again and its manifest
    # records the same block: same id, ranges, tokens and index commit.
    verifier_prompt = verifier_body["messages"][-1]["content"]
    assert _retrieved_section(verifier_prompt) == section
    assert f'<ref block_id="{rag}" start_line="{_CALL_LINE}" end_line="{_CALL_LINE}" />' in (
        verifier_prompt
    )
    assert verification.blocks == review.blocks
    assert tokens_by_role[verification.manifest_id]["retrieved_excerpt"] == block["tokens"]
    [(verify, _)] = observed.actions["llm.verify"]
    assert verify["manifest_id"] == verification.manifest_id
    # The stored finding keeps its citation of the retrieved block.
    [(_, output)] = observed.actions["llm.review_output"]
    [finding] = output["findings"]
    assert {"block_id": rag, "start_line": _CALL_LINE, "end_line": _CALL_LINE} in (
        finding["evidence_refs"]
    )


@pytest.mark.integration
def test_a_v5_run_shows_the_retrieved_section_through_the_worker_composition(env: Env) -> None:
    observed = _observe(env, version=5, rag=True)

    assert observed.outcomes == ["ack"]
    assert observed.queue.retries == []
    assert observed.state == ("publishing", None, 1)
    steps = list(_SNAPSHOT_PATH_STEPS)
    steps.insert(steps.index("context.build") + 1, "context.rag")
    assert observed.tools == steps
    # The stage reads the ready index at C1 and uses its one candidate.
    [(rag_request, rag_response)] = observed.actions["context.rag"]
    assert (rag_request["index_status"], rag_request["indexed_commit_sha"]) == ("ready", C1)
    assert rag_response["outcome"] == "used"
    rows = _retrieved_rows(observed)
    assert [(row.stage, row.attempt, len(row.blocks)) for row in rows] == [
        ("conventions", 1, 0),
        ("review", 1, 1),
        ("verification", 1, 1),
    ]
    _, review, verification = rows
    _, review_body, verifier_body = observed.bodies
    _assert_retrieved_and_shown_again(
        observed,
        index_commit_sha=C1,
        review=review,
        verification=verification,
        review_body=review_body,
        verifier_body=verifier_body,
    )


@pytest.mark.integration
def test_a_retry_before_the_accepted_answer_retrieves_from_the_newer_index(env: Env) -> None:
    # D8: the candidate checkpoint is the stored manifest. The first attempt retrieves at C1,
    # then its review call fails on both models before any answer is accepted. A newer
    # publication of the index (C2) lands, and the retry retrieves again: its review and
    # its verifier show the same block, recorded with C2.
    observed = _observe(env, version=5, retry=True, rag=True)

    assert observed.outcomes == ["ack", "ack"]
    assert len(observed.queue.retries) == 1
    assert observed.state == ("publishing", None, 2)
    assert [
        (request["indexed_commit_sha"], response["outcome"])
        for request, response in observed.actions["context.rag"]
    ] == [(C1, "used"), (C2, "used")]
    rows = _retrieved_rows(observed)
    assert [
        (row.stage, row.attempt, [block["index_commit_sha"] for block in row.blocks])
        for row in rows
    ] == [
        ("conventions", 1, []),
        ("review", 1, [C1]),
        ("review", 1, [C1]),
        ("review", 2, [C2]),
        ("verification", 2, [C2]),
    ]
    # The failed attempt and the retry show one block under one id; only the index differs.
    assert {row.blocks[0]["block_id"] for row in rows[1:]} == {rows[1].blocks[0]["block_id"]}
    # Bodies: conventions, the refused review on both models, then the retry's review and
    # verifier (the retry keeps the first attempt's conventions).
    _, refused, _, review_body, verifier_body = observed.bodies
    assert _retrieved_section(refused["messages"][-1]["content"]) == _retrieved_section(
        review_body["messages"][-1]["content"]
    )
    _assert_retrieved_and_shown_again(
        observed,
        index_commit_sha=C2,
        review=rows[3],
        verification=rows[4],
        review_body=review_body,
        verifier_body=verifier_body,
    )


# The review models without a fallback: the one slot a cache-miss attempt leaves after the
# conventions, the review and the verifier goes to a repair of the same model.
_NO_FALLBACK = LlmSettings(primary=PRIMARY, fallback=None)


@pytest.mark.integration
@pytest.mark.parametrize(
    ("llm", "recovery", "recovery_model"),
    [(_LLM, "fallback", FALLBACK.model), (_NO_FALLBACK, "repair", PRIMARY.model)],
    ids=["fallback", "repair"],
)
@pytest.mark.parametrize(
    ("review_kind", "invalid", "valid", "error"),
    [
        # A range past the shown hunk (RIGHT 9-13) of the diff block.
        ("review-v2", "verifier-unshown", "verifier", ":22-24 is not shown in verifier manifest"),
        # RULES_FORMAT_SPEC §8.2: a supported rule finding with no requirement of its rule.
        (
            "review-v2-rule",
            "verifier",
            "verifier-rule",
            "for candidate with rule_name '.review/rules.md' requires at least one matching "
            "requirement_ref for this rule",
        ),
    ],
    ids=["unshown-range", "rule-without-requirement"],
)
def test_an_invalid_verifier_reference_earns_the_bounded_recovery(
    env: Env,
    llm: LlmSettings,
    recovery: str,
    recovery_model: str,
    review_kind: str,
    invalid: str,
    valid: str,
    error: str,
) -> None:
    # PIPELINE_SPEC :632, TEST_PLAN :139 (#130 hotfix, tech lead 28.10): a verifier answer
    # citing what its manifest does not show earns the bounded repair/fallback of §4.5 within
    # the attempt's slots before verification_invalid_output; the accepted answer publishes.
    observed = _observe(
        env, version=4, llm=llm, review_kind=review_kind, verifier_kinds=(invalid, valid)
    )

    assert observed.state == ("publishing", None, 1)
    calls = [request for request, _ in observed.actions["llm.call"]]
    verification = [call for call in calls if call["stage"] == "verification"]
    assert [(call["kind"], call["model"]) for call in verification] == [
        ("primary", PRIMARY.model),
        (recovery, recovery_model),
    ]
    assert [call["operation"] for call in verification] == ["verification", "verification"]
    # CVS §5.2: the recovery call has a manifest row of its own; llm.verify names the
    # accepted call and that row.
    first, accepted = verification
    assert first["manifest_id"] != accepted["manifest_id"]
    [(verify, decisions)] = observed.actions["llm.verify"]
    assert (verify["accepted_call_id"], verify["manifest_id"]) == (
        accepted["call_id"],
        accepted["manifest_id"],
    )
    assert [decision["status"] for decision in decisions["decisions"]] == ["supported"]
    *_, first_body, recovery_body = observed.bodies
    if recovery == "repair":
        # The error goes back in the escaped validation errors of the bounded repair turn.
        assert [message["role"] for message in recovery_body["messages"]] == [
            "system",
            "user",
            "user",
        ]
        [errors] = re.findall(
            r"<validation_errors>\n(.*?)</validation_errors>",
            recovery_body["messages"][-1]["content"],
            flags=re.DOTALL,
        )
        assert error in errors
    else:
        # The fallback gets the original verifier prompt.
        assert recovery_body["messages"] == first_body["messages"]
    [(_, postprocess)] = observed.actions["review.postprocess"]
    assert postprocess["inline"] == 1
    assert [pointer.run_id for pointer in observed.queue.published] == [observed.run_id]
