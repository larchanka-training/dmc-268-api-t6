"""The live review path, pinned before the policy-snapshot path is switched on (#130 F1-1).

A run bound to review prompt v3 with all three prompt pins and a ready policy snapshot goes
through ``process_review_run`` with the real context stage, the production gateway, the
worker's Reflection Pass and a recording HTTP transport. Every F1 task keeps this test green:
until F2 binds a run to a v2-contract prompt, the snapshot path changes nothing a live run
sends or records.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import partial
from hashlib import sha256
from typing import Any
from uuid import UUID

import httpx
import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bootstrap.llm_gateway import build_gateway
from app.bootstrap.seed_prompts import PromptAsset
from app.modules.reviews.application.ast_resolution import RevisionTree
from app.modules.reviews.application.check_runs import RunProviderCheckpoint
from app.modules.reviews.application.conventions import (
    ConventionsRequest,
    GenerateRepoConventions,
    PolicyFilteredConventionsSource,
    RepositoryFile,
    RepositorySnapshot,
    conventions_cache_key,
)
from app.modules.reviews.application.execute_review import ReviewReply
from app.modules.reviews.application.get_run_diff import DiffSnapshot
from app.modules.reviews.application.llm import RunCallContext
from app.modules.reviews.application.policy_snapshot import (
    PolicySnapshot,
    make_complete_execution_profile,
)
from app.modules.reviews.application.prepare_policy_snapshot import PreparePolicySnapshot
from app.modules.reviews.application.process_run import ReviewRunProcessor
from app.modules.reviews.application.prompt_builder import PullRequestMeta, ReviewContext
from app.modules.reviews.application.reflection_pass import ReflectionPass
from app.modules.reviews.application.review_context import BuildReviewContext
from app.modules.reviews.application.review_context_stage import (
    EstimatedTokenCounter,
    ReviewContextStage,
)
from app.modules.reviews.application.review_output import PublishedFinding
from app.modules.reviews.application.run_trace import TransactionalRunTrace
from app.modules.reviews.application.vcs_diff import PullRequestLocator, VcsFile, VcsPullRequest
from app.modules.reviews.infrastructure.blob_cache import SqlAlchemyBlobCache
from app.modules.reviews.infrastructure.conventions_unit_of_work import (
    SqlAlchemyRepositoryConventionsUnitOfWork,
)
from app.modules.reviews.infrastructure.llm.gateway import (
    GatewayResult,
    LlmGateway,
    StructuredTask,
)
from app.modules.reviews.infrastructure.llm.models import (
    GatewayConventionsModel,
    GatewayReviewModel,
    GatewayVerifierModel,
)
from app.modules.reviews.infrastructure.llm.settings import LlmSettings
from app.modules.reviews.infrastructure.models import PromptVersion, Run
from app.modules.reviews.infrastructure.policy_snapshot_unit_of_work import (
    SqlAlchemyPolicySnapshotUnitOfWork,
)
from app.modules.reviews.infrastructure.provider_conventions import (
    ProviderConventionsModel,
    ProviderRepositoryConventionsSource,
)
from app.modules.reviews.infrastructure.run_lifecycle_store import (
    SqlAlchemyRunLifecycleStore,
    SqlAlchemyRunTraceUnitOfWork,
)
from app.modules.reviews.infrastructure.run_processing_unit_of_work import (
    SqlAlchemyRunProcessingUnitOfWork,
)
from app.modules.reviews.infrastructure.run_repository import SqlAlchemyRunRepository
from app.worker import process_review_run
from tests.portal_postgres import NOW, REPO_A, RULE_A, WS_A, Env, portal_schema
from tests.test_llm_gateway import FALLBACK, PRIMARY, completion
from tests.test_prepare_policy_snapshot import Source
from tests.test_review_context_stage import Parser, Vcs, _checkpoint
from tests.test_review_prompt_pins_postgres import _bind_v4
from tests.test_run_prompt_pins_postgres import _packaged, _seed
from tests.test_run_publication_settings_postgres import _insert, _ready

_HEAD = "f" * 40
_PATH = "app/charge.py"
_BLOB = "d" * 40
_PATCH = "@@ -10,4 +10,5 @@\n context\n+added\n context\n context\n context"
# The file at the head, so the stage's context build has a real L1 neighbourhood.
_SOURCE = "".join(f"line_{number} = {number}\n" for number in range(1, 10)) + (
    "context\nadded\ncontext\ncontext\ncontext\n"
)
# The provider's AGENTS.md, which the legacy conventions read (R1 names it the unchecked read).
_PROVIDER_AGENTS = "# Provider AGENTS\n\nRead by fetch_agents_md.\n"
_PROVIDER_AGENTS_SHA = "a" * 40
# The snapshot's AGENTS.md: the legacy path never shows it to a model.
_SNAPSHOT_AGENTS = b"# Snapshot AGENTS\n\nOnly the snapshot path shows this.\n"
_CONVENTIONS_ANSWER = {
    "files": [{"path": _PATH, "relevance": "changed file"}],
    "key_patterns": ["Pattern one", "Pattern two", "Pattern three"],
    "recommendations": [f"Check {index} (from: standard/correctness)" for index in range(5)],
}
_REVIEW_ANSWER = {
    "findings": [
        {
            "path": _PATH,
            "line": 11,
            "start_line": None,
            "severity": "high",
            "category": "correctness",
            "title": "Added line is unused",
            "body": "The added line is never read.",
            "suggestion": None,
            "confidence": 0.9,
            "rule_name": None,
        }
    ],
    "summary": {
        "problem": "An added line is unused.",
        "done_well": "The change is small.",
        "effort": "small",
    },
}
_META = PullRequestMeta(
    title="Charge fix",
    description="Fixes the charge.",
    author="octocat",
    source_branch="feature",
    target_branch="main",
    labels=(),
    files_changed=1,
    lines_added=1,
    lines_removed=0,
    is_draft=False,
    is_fork=False,
)


@pytest.fixture
def env() -> Iterator[Env]:
    url = os.environ.get("TEST_DATABASE_URL")
    if url is None:
        pytest.skip("set TEST_DATABASE_URL to pin the live review path")
    with portal_schema(url, None) as schema:
        yield schema


@dataclass
class LiveVcs(Vcs):
    """The pull request of the run; the first attempt fetches and stores its diff."""

    async def get_diff(
        self, pull_request: VcsPullRequest, *, checkpoint: RunProviderCheckpoint
    ) -> tuple[VcsFile, ...]:
        await checkpoint()
        return (VcsFile(_PATH, "modified", _BLOB, None, 1, 0, 1, _PATCH),)

    async def get_blob(
        self, locator: PullRequestLocator, sha: str, *, checkpoint: RunProviderCheckpoint
    ) -> bytes:
        await checkpoint()
        return _SOURCE.encode()


class Tree:
    """The head revision of the run: the changed file only."""

    async def tree(self, revision: str) -> RevisionTree:
        return RevisionTree(revision, {_PATH: _BLOB}, complete=True)

    async def read_blob(self, blob_sha: str) -> bytes:
        return _SOURCE.encode()


class Provider:
    """The attempt's provider: fixed repository reads, the models over the real gateway."""

    def __init__(self, gateway: LlmGateway, run: RunCallContext) -> None:
        self._conventions = GatewayConventionsModel(gateway, run)
        self._review = GatewayReviewModel(gateway, run, self)

    async def fetch_diff(self, *, code_change_id: UUID, head_sha: str) -> list[DiffSnapshot]:
        raise AssertionError("the worker reads the diff through its VcsProvider")

    async def fetch_file_content(self, *, code_change_id: UUID, head_sha: str, path: str) -> str:
        raise AssertionError("the worker reads files through its VcsProvider")

    async def fetch_agents_md(self, repository_id: UUID) -> RepositorySnapshot:
        return RepositorySnapshot(_PROVIDER_AGENTS, _PROVIDER_AGENTS_SHA)

    async def fetch_tree(self, repository_id: UUID) -> tuple[RepositoryFile, ...]:
        return (RepositoryFile(_PATH, len(_SOURCE)),)

    async def fetch_files(
        self, repository_id: UUID, paths: tuple[str, ...]
    ) -> tuple[RepositoryFile, ...]:
        return tuple(RepositoryFile(path, len(_SOURCE), _SOURCE) for path in paths)

    async def draft_conventions(self, *, request: ConventionsRequest) -> Mapping[str, object]:
        return await self._conventions.draft_conventions(request=request)

    async def get_pull_request_meta(self, run_id: UUID) -> PullRequestMeta:
        return _META

    async def draft_review(self, *, context: ReviewContext) -> Mapping[str, object] | ReviewReply:
        return await self._review.draft_review(context=context)

    async def publish_review(
        self,
        *,
        commit_sha: str,
        body: str,
        findings: tuple[PublishedFinding, ...],
        idempotency_key: str,
    ) -> None:
        raise AssertionError("the output handler receives the answer")


@dataclass
class Output:
    answers: list[object]

    async def execute(self, run_id: UUID, model_output: object) -> bool:
        self.answers.append(model_output)
        return True


@dataclass(frozen=True)
class Observed:
    snapshot: PolicySnapshot
    review_system: tuple[int, str]
    conventions_prompt_id: UUID
    messages: list[list[dict[str, str]]]
    tasks: list[StructuredTask]
    tools: list[str]
    llm_call_requests: list[dict[str, Any]]
    # The responses of the steps before and between the model calls, by tool.
    responses: list[tuple[str, Any]]
    conventions_keys: list[tuple[UUID, str | None, UUID, str | None]]
    answers: list[object]
    # Rows in context_manifests: a legacy run stores none (F1-6).
    manifests: int


async def _claim(factory: async_sessionmaker[AsyncSession], run_id: UUID) -> None:
    async with factory.begin() as session:
        assert (
            await SqlAlchemyRunLifecycleStore(session).claim(
                run_id, worker_id="worker", now=NOW, lease_until=NOW + timedelta(minutes=10)
            )
            == 1
        )


def _live_assets() -> tuple[PromptAsset, ...]:
    # Bound to v3 explicitly: a newer packaged review.system (F2's v4) is not seeded here.
    return tuple(
        asset for asset in _packaged() if not (asset.key == "review.system" and asset.version > 3)
    )


async def _exercise(
    factory: async_sessionmaker[AsyncSession], *, pre_0039: bool = False
) -> Observed:
    await _ready(factory)
    await _seed(factory, _live_assets())
    async with factory.begin() as session:
        run_id = await _insert(session)
        if pre_0039:
            # A run created before 0039: only the review prompt is pinned.
            await session.execute(
                text(
                    "UPDATE runs SET conventions_prompt_version_id=NULL, "
                    "verifier_prompt_version_id=NULL WHERE id=:id"
                ),
                {"id": run_id},
            )
    await _claim(factory, run_id)
    async with factory() as session:
        run = await session.get(Run, run_id)
        assert run is not None
        review = await session.get(PromptVersion, run.prompt_version_id)
        assert review is not None
        conventions_id = (
            await session.scalars(
                select(PromptVersion.id).where(
                    PromptVersion.key == "review.conventions", PromptVersion.is_active.is_(True)
                )
            )
        ).one()
        prompt_version_id, rule_version_id = run.prompt_version_id, run.rule_version_id

    def uow() -> SqlAlchemyPolicySnapshotUnitOfWork:
        # The worker injects its complete profile the same way (worker.py:534).
        return SqlAlchemyPolicySnapshotUnitOfWork(
            factory, execution_profile=make_complete_execution_profile()
        )

    prepare = PreparePolicySnapshot(
        uow_factory=uow, source_factory=lambda pins: Source(agents=_SNAPSHOT_AGENTS)
    )
    trace = TransactionalRunTrace(partial(SqlAlchemyRunTraceUnitOfWork, factory))
    vcs = LiveVcs(head_sha=_HEAD, files_changed=1)
    stage = ReviewContextStage(
        runs=SqlAlchemyRunRepository(factory, allow_unscoped=True),
        checkpoint=_checkpoint,
        vcs=vcs,
        merge_base=vcs,
        prepare_policy=prepare,
        context_builder=lambda run: BuildReviewContext(
            source=Tree(), parser=Parser(), counter=EstimatedTokenCounter()
        ),
        trace=trace,
    )
    replies = [
        completion(json.dumps(_CONVENTIONS_ANSWER)),
        completion(json.dumps(_REVIEW_ANSWER)),
    ]
    requests: list[httpx.Request] = []

    def http(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return replies.pop(0)

    tasks: list[StructuredTask] = []
    output = Output([])
    async with httpx.AsyncClient(transport=httpx.MockTransport(http)) as client:
        gateway = build_gateway(LlmSettings(primary=PRIMARY, fallback=None), client, factory)
        generate = gateway.generate

        async def recording_generate(
            task: StructuredTask, context: RunCallContext
        ) -> GatewayResult:
            # reserved_calls and bounded_repair never reach the trace: read them off the task.
            tasks.append(task)
            return await generate(task, context)

        gateway.generate = recording_generate  # type: ignore[method-assign]
        call_context = RunCallContext(
            run_id=run_id,
            workspace_id=WS_A,
            attempt=1,
            engine="fast",
            deadline=datetime.now(UTC) + timedelta(minutes=10),
            prompt_version_id=prompt_version_id,
            rule_version_id=rule_version_id,
        )
        assert await process_review_run(
            run_id,
            Provider(gateway, call_context),
            factory,
            vcs,
            output=output,
            trace=trace,
            engine="fast",
            context_stage=stage,
            # The worker hands every attempt its verifier (R14): a legacy run never calls it.
            reflection_pass=ReflectionPass(GatewayVerifierModel(gateway, call_context)),
        )
    snapshot = await prepare.execute(run_id)
    async with factory() as session:
        actions = (
            await session.execute(
                text(
                    "SELECT tool, request, response FROM run_actions "
                    "WHERE run_id=:id ORDER BY index"
                ),
                {"id": run_id},
            )
        ).all()
        manifests = (
            await session.execute(
                text("SELECT count(*) FROM context_manifests WHERE run_id=:id"), {"id": run_id}
            )
        ).scalar_one()
        conventions_keys = (
            await session.execute(
                text(
                    "SELECT repository_id, agents_md_sha, prompt_version_id, policy_key "
                    "FROM repo_conventions ORDER BY created_at"
                )
            )
        ).all()
    return Observed(
        snapshot=snapshot,
        review_system=(review.version, review.content),
        conventions_prompt_id=conventions_id,
        messages=[json.loads(request.content)["messages"] for request in requests],
        tasks=tasks,
        tools=[tool for tool, _, _ in actions],
        llm_call_requests=[request for tool, request, _ in actions if tool == "llm.call"],
        responses=[(tool, response) for tool, _, response in actions if tool != "llm.call"],
        conventions_keys=[tuple(row) for row in conventions_keys],
        answers=output.answers,
        manifests=manifests,
    )


# The packaged prompt files, byte for byte: review.system v3 and review.conventions v2.
_REVIEW_SYSTEM_SHA = "eb851e7fc2329eb7bdff015ec0fda945c46111ec5c884934ed6f6d6803883452"
_CONVENTIONS_SYSTEM_SHA = "225a8f7d56835cf1307ddb25feac602c3d10e03c34ee51afc90a1f99af101454"
_CONVENTIONS_USER = (
    "<custom_instructions>\n\n</custom_instructions>\n"
    "<agents_md>\n# Provider AGENTS\n\nRead by fetch_agents_md.\n\n</agents_md>\n"
    "<repo_tree>\napp/charge.py\n</repo_tree>\n"
    "<repo_files>\n"
    '<file path="app/charge.py">\n'
    + "".join(f'<line n="{number}">line_{number} = {number}</line>\n' for number in range(1, 10))
    + '<line n="10">context</line>\n'
    '<line n="11">added</line>\n'
    '<line n="12">context</line>\n'
    '<line n="13">context</line>\n'
    '<line n="14">context</line>\n'
    "</file>\n"
    "</repo_files>\n"
    "<changed_files>\napp/charge.py\n</changed_files>"
)
_REVIEW_USER = (
    "<custom_instructions>\n\n</custom_instructions>\n"
    "<agents_md>\n# Provider AGENTS\n\nRead by fetch_agents_md.\n\n</agents_md>\n"
    "<repo_conventions>\n"
    "<key_patterns>\n"
    "<pattern>Pattern one</pattern>\n"
    "<pattern>Pattern two</pattern>\n"
    "<pattern>Pattern three</pattern>\n"
    "</key_patterns>\n"
    "<recommendations>\n"
    + "".join(
        f"<recommendation>Check {index} (from: standard/correctness)</recommendation>\n"
        for index in range(5)
    )
    + "</recommendations>\n"
    "</repo_conventions>\n"
    "<pr_meta>\n"
    "<title>Charge fix</title>\n"
    "<description>Fixes the charge.</description>\n"
    "<author>octocat</author>\n"
    "<source_branch>feature</source_branch>\n"
    "<target_branch>main</target_branch>\n"
    "<labels></labels>\n"
    "<files_changed>1</files_changed>\n"
    "<lines_added>1</lines_added>\n"
    "<lines_removed>0</lines_removed>\n"
    "<draft>false</draft>\n"
    "<fork>false</fork>\n"
    "</pr_meta>\n"
    "<changed_files>\n"
    '<file path="app/charge.py" status="modified" language="Python">\n'
    '<line n="10" type="context">context</line>\n'
    '<line n="11" type="added">added</line>\n'
    '<line n="12" type="context">context</line>\n'
    '<line n="13" type="context">context</line>\n'
    '<line n="14" type="context">context</line>\n'
    "</file>\n"
    "</changed_files>\n"
    "<omitted_files>\n\n</omitted_files>"
)


def _legacy_responses(snapshot: PolicySnapshot) -> list[tuple[str, Any]]:
    """What a legacy run records before and between its model calls; only ids vary."""
    return [
        (
            "policy.load",
            {
                "policy_snapshot_id": str(snapshot.snapshot_id),
                "policy_base_sha": "b" * 40,
                "policy_digest": snapshot.policy_digest,
                "documents": [
                    {"path": ".review/rules.md", "presence": "present", "bytes": 43, "lines": 5},
                    {"path": "AGENTS.md", "presence": "present", "bytes": 54, "lines": 3},
                ],
                "ignore_patterns": 1,
                "ignore_digest": _IGNORE_DIGEST,
            },
        ),
        (
            "vcs.fetch_diff",
            {
                "files": [
                    {
                        "filename": _PATH,
                        "status": "modified",
                        "additions": 1,
                        "deletions": 0,
                        "too_large": False,
                    }
                ],
                "total_lines": 1,
                "summary_only": False,
            },
        ),
        (
            "context.build",
            {
                "status": "review",
                "reason": None,
                "diff": {
                    "digest": _DIFF_DIGEST,
                    "files": 1,
                    "diff_old_sha": "c" * 40,
                    "file_list_complete": True,
                },
                "reviewable_lines": 1,
                "ignored_files": 0,
                "levels_present": ["L1", "L3"],
                "blocks_by_level": {"L1": 1, "L3": 1},
                "tokens_by_level": {"L1": 15, "L3": 34},
                "tokens_by_role": {"diff": 15, "source_excerpt": 34},
                "tokens_used": 49,
                "imports": {},
                "omissions": [],
                "blocks": [
                    {
                        "level": "L1",
                        "role": "diff",
                        "path": _PATH,
                        "side": "RIGHT",
                        "ranges": [[10, 14]],
                        "reason": "changed_hunk",
                        "tokens": 15,
                        "related": [],
                        "truncated": False,
                        "representation": "exact_lines",
                    },
                    {
                        "level": "L3",
                        "role": "source_excerpt",
                        "path": _PATH,
                        "side": "RIGHT",
                        "ranges": [[1, 14]],
                        "reason": "full_file",
                        "tokens": 34,
                        "related": [],
                        "truncated": False,
                        "representation": "exact_lines",
                    },
                ],
            },
        ),
        ("llm.repo_conventions", {"files": [{"path": _PATH, "relevance": "changed file"}]}),
        (
            "context.build",
            {
                "files": [{"path": _PATH, "level_used": "L1", "priority": 0}],
                "omitted_files": [],
                "budget": {"limit": 60000, "used": 8},
            },
        ),
    ]


_IGNORE_DIGEST = "24ab4f16f70b7124f9af6fd8e6000572622dc1f868f1980616b2519fd03c9821"
_DIFF_DIGEST = "7ce85b36525348b9c166a03b5d6ea674ca035b2c2ae694aab41d67e087641a42"


def _observe(env: Env, *, pre_0039: bool = False) -> Observed:
    async def scenario() -> Observed:
        engine = env.engine()
        try:
            return await _exercise(
                async_sessionmaker(engine, expire_on_commit=False), pre_0039=pre_0039
            )
        finally:
            await engine.dispose()

    return asyncio.run(scenario())


def _assert_legacy_run(observed: Observed) -> None:
    """Everything a live run sends and records, for a v3-bound run."""
    bindings = observed.snapshot.prompt_bindings
    assert observed.review_system[0] == 3
    assert bindings.review_system_version == "v3"
    # The exact provider messages: the legacy envelope, AGENTS.md from the provider read,
    # no snapshot document and no block id.
    conventions, review = observed.messages
    assert [message["role"] for message in conventions] == ["system", "user"]
    assert sha256(conventions[0]["content"].encode()).hexdigest() == _CONVENTIONS_SYSTEM_SHA
    assert conventions[1]["content"] == _CONVENTIONS_USER
    assert [message["role"] for message in review] == ["system", "user"]
    assert review[0]["content"] == observed.review_system[1]
    assert sha256(review[0]["content"].encode()).hexdigest() == _REVIEW_SYSTEM_SHA
    assert review[1]["content"] == _REVIEW_USER
    # The tasks the gateway ran: no reserved slot or time, no bounded repair, no snapshot.
    assert [
        (
            task.operation,
            task.route,
            task.reserved_calls,
            task.bounded_repair,
            task.reserved_seconds,
        )
        for task in observed.tasks
    ] == [("conventions", "review", 0, False, 0.0), ("review", "review", 0, False, 0.0)]
    assert [(task.policy_snapshot, task.counter) for task in observed.tasks] == [
        (None, None),
        (None, None),
    ]
    # The run trace: the step sequence, every llm.call request (keys and values) and the
    # responses of the other steps.
    assert observed.tools == [
        "policy.load",
        "vcs.fetch_diff",
        "context.build",
        "llm.call",
        "llm.repo_conventions",
        "context.build",
        "llm.call",
    ]
    review_prompt = str(bindings.review_system_id)
    assert observed.llm_call_requests == [
        {
            "kind": "primary",
            "model": "primary-model",
            "call_no": call_no,
            "attempt": 1,
            "timeout_s": 90.0,
            "prompt_version_id": review_prompt,
            "rule_version_id": str(RULE_A),
            "input_tokens_estimate": tokens,
            "operation": operation,
        }
        for call_no, operation, tokens in ((1, "conventions", 2162), (2, "review", 7445))
    ]
    assert observed.responses == _legacy_responses(observed.snapshot)
    # The conventions cache key: the provider's AGENTS.md sha, the active conventions row
    # and the ignore digest of the snapshot as the policy key.
    assert observed.conventions_keys == [
        (REPO_A, _PROVIDER_AGENTS_SHA, observed.conventions_prompt_id, _IGNORE_DIGEST)
    ]
    assert observed.answers == [_REVIEW_ANSWER]
    # The production gateway has a manifest store: a legacy run still stores no manifest.
    assert observed.manifests == 0


@pytest.mark.integration
def test_v3_run_with_a_ready_snapshot_stays_on_the_legacy_path(env: Env) -> None:
    observed = _observe(env)

    # The premise: v3 is bound, D pinned all three prompts and the snapshot is ready.
    bindings = observed.snapshot.prompt_bindings
    assert bindings.conventions_id == observed.conventions_prompt_id
    assert bindings.is_complete is True
    assert observed.snapshot.is_ready_for_llm is True
    _assert_legacy_run(observed)


@pytest.mark.integration
def test_v3_run_created_before_the_pins_stays_on_the_legacy_path(env: Env) -> None:
    observed = _observe(env, pre_0039=True)

    # The premise: a pre-0039 run pins only its review prompt, so its snapshot is not ready.
    bindings = observed.snapshot.prompt_bindings
    assert (bindings.conventions_id, bindings.verifier_id) == (None, None)
    assert bindings.is_complete is False
    assert observed.snapshot.is_ready_for_llm is False
    _assert_legacy_run(observed)


# ---------- #130 F1-10: conventions on the snapshot path, through the processor ----------

_ROTATED = "A conventions prompt activated after the run was created."


class CountingProvider(Provider):
    """The attempt's provider, counting the AGENTS.md reads the snapshot path must not make."""

    def __init__(self, gateway: LlmGateway, run: RunCallContext) -> None:
        super().__init__(gateway, run)
        self.agents_reads = 0

    async def fetch_agents_md(self, repository_id: UUID) -> RepositorySnapshot:
        self.agents_reads += 1
        return await super().fetch_agents_md(repository_id)


@dataclass(frozen=True)
class ConventionsObserved:
    snapshot: PolicySnapshot
    pinned: tuple[UUID, str]
    rotated: UUID
    messages: list[list[dict[str, str]]]
    models: list[str]
    tasks: list[StructuredTask]
    agents_reads: int
    manifest_rows: list[tuple[str, str]]
    manifest_responses: list[Any]
    conventions_keys: list[tuple[UUID, str | None, UUID, str | None]]


async def _prepare_conventions(
    factory: async_sessionmaker[AsyncSession], *, v4: bool
) -> ConventionsObserved:
    """``process_review_run``'s processor up to the conventions answer: C1 fails validation."""
    await _ready(factory)
    await _seed(factory, _live_assets())
    async with factory.begin() as session:
        run_id = await _insert(session)
    if v4:
        await _bind_v4(factory, run_id)
    async with factory.begin() as session:
        run = await session.get(Run, run_id)
        assert run is not None and run.conventions_prompt_version_id is not None
        pinned = await session.get(PromptVersion, run.conventions_prompt_version_id)
        assert pinned is not None
        pinned_row = (pinned.id, pinned.content)
        prompt_version_id, rule_version_id = run.prompt_version_id, run.rule_version_id
        # A newer conventions row goes live after the run pinned its prompt.
        await session.execute(
            text("UPDATE prompt_versions SET is_active=false WHERE key='review.conventions'")
        )
        rotated = PromptVersion(
            key="review.conventions",
            version=pinned.version + 1,
            content=_ROTATED,
            checksum=sha256(_ROTATED.encode()).hexdigest(),
            is_active=True,
        )
        session.add(rotated)
        await session.flush()
        rotated_id = rotated.id
    await _claim(factory, run_id)

    def uow() -> SqlAlchemyPolicySnapshotUnitOfWork:
        return SqlAlchemyPolicySnapshotUnitOfWork(
            factory, execution_profile=make_complete_execution_profile()
        )

    prepare = PreparePolicySnapshot(
        uow_factory=uow, source_factory=lambda pins: Source(agents=_SNAPSHOT_AGENTS)
    )
    trace = TransactionalRunTrace(partial(SqlAlchemyRunTraceUnitOfWork, factory))
    vcs = LiveVcs(head_sha=_HEAD, files_changed=1)
    stage = ReviewContextStage(
        runs=SqlAlchemyRunRepository(factory, allow_unscoped=True),
        checkpoint=_checkpoint,
        vcs=vcs,
        merge_base=vcs,
        prepare_policy=prepare,
        context_builder=lambda run: BuildReviewContext(
            source=Tree(), parser=Parser(), counter=EstimatedTokenCounter()
        ),
        trace=trace,
    )
    replies = [completion("{not json"), completion(json.dumps(_CONVENTIONS_ANSWER))]
    requests: list[httpx.Request] = []

    def http(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return replies.pop(0)

    tasks: list[StructuredTask] = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(http)) as client:
        gateway = build_gateway(LlmSettings(primary=PRIMARY, fallback=FALLBACK), client, factory)
        generate = gateway.generate

        async def recording_generate(
            task: StructuredTask, context: RunCallContext
        ) -> GatewayResult:
            tasks.append(task)
            return await generate(task, context)

        gateway.generate = recording_generate  # type: ignore[method-assign]
        provider = CountingProvider(
            gateway,
            RunCallContext(
                run_id=run_id,
                workspace_id=WS_A,
                attempt=1,
                engine="fast",
                deadline=datetime.now(UTC) + timedelta(minutes=10),
                prompt_version_id=prompt_version_id,
                rule_version_id=rule_version_id,
            ),
        )
        # The composition of process_review_run (worker.py), stopped after the conventions.
        await ReviewRunProcessor(
            SqlAlchemyRunRepository(factory, allow_unscoped=True),
            provider,
            SqlAlchemyBlobCache(factory),
            GenerateRepoConventions(
                PolicyFilteredConventionsSource(
                    ProviderRepositoryConventionsSource(provider), stage.excluded
                ),
                ProviderConventionsModel(provider),
                lambda: SqlAlchemyRepositoryConventionsUnitOfWork(factory),
            ),
            vcs_provider=vcs,
            trace=trace,
            uow_factory=lambda: SqlAlchemyRunProcessingUnitOfWork(factory),
            context_stage=stage,
        ).prepare(run_id)
    snapshot = await prepare.execute(run_id)
    async with factory() as session:
        manifest_rows = (
            await session.execute(
                text(
                    "SELECT stage, recovery_kind FROM context_manifests "
                    "WHERE run_id=:id ORDER BY created_at"
                ),
                {"id": run_id},
            )
        ).all()
        manifest_responses = (
            await session.execute(
                text(
                    "SELECT response FROM run_actions WHERE run_id=:id "
                    "AND tool='context.manifest' ORDER BY index"
                ),
                {"id": run_id},
            )
        ).scalars()
        conventions_keys = (
            await session.execute(
                text(
                    "SELECT repository_id, agents_md_sha, prompt_version_id, policy_key "
                    "FROM repo_conventions ORDER BY created_at"
                )
            )
        ).all()
    return ConventionsObserved(
        snapshot=snapshot,
        pinned=pinned_row,
        rotated=rotated_id,
        messages=[json.loads(request.content)["messages"] for request in requests],
        models=[json.loads(request.content)["model"] for request in requests],
        tasks=tasks,
        agents_reads=provider.agents_reads,
        manifest_rows=[(row[0], row[1]) for row in manifest_rows],
        manifest_responses=list(manifest_responses),
        conventions_keys=[tuple(row) for row in conventions_keys],
    )


def _observe_conventions(env: Env, *, v4: bool) -> ConventionsObserved:
    async def scenario() -> ConventionsObserved:
        engine = env.engine()
        try:
            return await _prepare_conventions(
                async_sessionmaker(engine, expire_on_commit=False), v4=v4
            )
        finally:
            await engine.dispose()

    return asyncio.run(scenario())


@pytest.mark.integration
def test_v4_conventions_use_the_pinned_prompt_the_snapshot_and_the_reserve(env: Env) -> None:
    observed = _observe_conventions(env, v4=True)

    assert observed.snapshot.prompt_bindings.review_system_version == "v4"
    assert observed.snapshot.prompt_bindings.conventions_id == observed.pinned[0]
    # R1/T-b: the pinned row, not the row activated after the run was created.
    assert [messages[0]["content"] for messages in observed.messages] == [observed.pinned[1]] * 2
    # R1: AGENTS.md comes from the snapshot; the provider is never asked for it.
    assert observed.agents_reads == 0
    user = observed.messages[0][1]["content"]
    assert "Only the snapshot path shows this." in user
    assert "Read by fetch_agents_md." not in user
    # E-1 (TL-2): two slots kept for the review and the verifier, so the failed C1 goes
    # straight to the fallback within the reserve; bounded repair is on.
    [task] = observed.tasks
    assert (task.operation, task.reserved_calls, task.bounded_repair) == ("conventions", 2, True)
    assert task.policy_snapshot == observed.snapshot
    assert observed.models == ["primary-model", "fallback-model"]
    assert [len(messages) for messages in observed.messages] == [2, 2]
    assert observed.manifest_rows == [("conventions", "none"), ("conventions", "fallback")]
    # The manifest blocks are counted with the pinned counter.
    assert all(response["tokens_by_role"] for response in observed.manifest_responses)
    # R5 (TL-7): the v2 digest of the pinned identities, so no legacy row is reused.
    assert observed.conventions_keys == [
        (
            REPO_A,
            observed.snapshot.agents.blob_oid,
            observed.pinned[0],
            conventions_cache_key(observed.snapshot, _IGNORE_DIGEST),
        )
    ]


@pytest.mark.integration
def test_v3_conventions_keep_the_active_prompt_the_provider_read_and_the_repair(
    env: Env,
) -> None:
    observed = _observe_conventions(env, v4=False)

    assert observed.snapshot.prompt_bindings.review_system_version == "v3"
    assert [messages[0]["content"] for messages in observed.messages] == [_ROTATED] * 2
    assert observed.agents_reads == 1
    assert "Read by fetch_agents_md." in observed.messages[0][1]["content"]
    [task] = observed.tasks
    assert (task.operation, task.reserved_calls, task.bounded_repair) == ("conventions", 0, False)
    assert task.policy_snapshot is None
    # Legacy repairs C1 on the primary model (its answer and the repair turn appended)
    # instead of going to the fallback.
    assert observed.models == ["primary-model", "primary-model"]
    assert [len(messages) for messages in observed.messages] == [2, 4]
    assert observed.manifest_rows == []
    assert [key[1:3] for key in observed.conventions_keys] == [
        (_PROVIDER_AGENTS_SHA, observed.rotated)
    ]
