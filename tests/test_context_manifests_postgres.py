"""The manifest of every snapshot-path provider call is stored before the call (#130 F1-6).

The gateway writes one ``context_manifests`` row and one ``context.manifest`` run action per
base, repair or fallback call of a snapshot-path task, in one short transaction before the
provider is called (CVS §5; backend.md §3.3). A retry reuses the row. The ``llm.call`` request
names the call, its stage, the policy snapshot and the manifest (E-3). A failed write means no
provider call. A legacy task writes nothing and its ``llm.call`` request is unchanged.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from functools import partial
from hashlib import sha256
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.code_index.domain.enums import EdgeKind
from app.modules.reviews.application.context_provenance import LineRange, SourceRef, SourceRole
from app.modules.reviews.application.diff_map import DiffSide
from app.modules.reviews.application.llm import (
    LlmCallFailed,
    LlmCallKind,
    LlmCallRecord,
    ManifestEntry,
    RunCallContext,
    TransactionalManifestStore,
    stage_prompt_checksum,
)
from app.modules.reviews.application.policy_snapshot import CallManifest, ManifestBlock
from app.modules.reviews.application.prepare_policy_snapshot import pinned_to_json
from app.modules.reviews.application.prompt_builder import ChangedFile, DiffLine, ReviewContext
from app.modules.reviews.application.review_context import (
    ContextLevel,
    Representation,
    RetrievedBlock,
)
from app.modules.reviews.application.run_failures import RunFailure
from app.modules.reviews.application.run_trace import TransactionalRunTrace
from app.modules.reviews.infrastructure.context_manifest_store import (
    SqlAlchemyContextManifestUnitOfWork,
)
from app.modules.reviews.infrastructure.llm.gateway import LlmGateway, StructuredTask
from app.modules.reviews.infrastructure.llm.memory import InMemoryLlmCallTrace, InMemoryUsageLedger
from app.modules.reviews.infrastructure.llm.models import review_with_gateway
from app.modules.reviews.infrastructure.llm.settings import LlmSettings
from app.modules.reviews.infrastructure.llm.transport import (
    ChatMessage,
    ResponseSchema,
    TransportUnavailable,
)
from app.modules.reviews.infrastructure.llm_call_trace import RunTraceLlmCalls
from app.modules.reviews.infrastructure.run_action_records import insert_run_action
from app.modules.reviews.infrastructure.run_lifecycle_store import SqlAlchemyRunTraceUnitOfWork
from tests.llm_fakes import context_checkpoint
from tests.portal_postgres import REPO_A, RULE_A, RUN_ATTEMPTED, WS_A, Env, portal_schema
from tests.test_gateway_review_model import (
    EMPTY_ANSWER,
    MAIN,
    SUMMARY,
    Transport,
    _context,
    _finding,
    _meta,
    _profile,
    _snapshot,
)

SNAPSHOT = _snapshot()
FILES = (ChangedFile(MAIN, "modified", (DiffLine(1, "added", "x = 1"),)),)
VALID = json.dumps({"findings": [_finding(1)], "summary": SUMMARY})
# The rendered blocks of the base call, counted with counter/v2 (three characters per token).
RULES_BLOCK = (
    '<repository_rules block_id="policy-rules" path=".review/rules.md">\n'
    '<line n="1"># Rules</line>\n'
    '<line n="2">Always sanitize input.</line>\n'
    "</repository_rules>"
)
FILE_BLOCK = (
    '<file block_id="file-app-main-py-990f7e41" path="app/main.py" status="modified" '
    'language="Python">\n'
    '<line n="1" type="added">x = 1</line>\n'
    "</file>"
)


def _tokens(text: str) -> int:
    return (len(text) + 2) // 3


# A stored policy snapshot of the run: every manifest row names one (CVS §8.3).
INSERT_POLICY_SNAPSHOT = (
    "INSERT INTO run_policy_snapshots (run_id, snapshot_id, repository_id, schema_version, "
    "policy_base_sha, ignore_patterns, ignore_digest, parser_profile, parser_version, "
    "matcher_version, limits_version, builtin_exclusions_version, priority_matcher_version, "
    "service_defaults_rule_version_id, defaults_checksum, prompt_bindings, execution_profile, "
    "policy_digest) VALUES (:run, :snapshot, :repository, 1, :base, '[]'::jsonb, :ignore, "
    "'rules-markdown/v1', '4.0.0', 'review-ignore/v1', 'limits/v1', 'builtin-exclusions/v1', "
    "'priority/v1', :rule, :defaults, CAST(:bindings AS jsonb), CAST(:profile AS jsonb), "
    ":digest)"
)


def policy_snapshot_values(run_id: UUID, snapshot_id: UUID) -> dict[str, Any]:
    return {
        "run": run_id,
        "snapshot": snapshot_id,
        "repository": REPO_A,
        "base": "b" * 40,
        "ignore": "0" * 64,
        "rule": RULE_A,
        "defaults": "f" * 64,
        "bindings": json.dumps(pinned_to_json(SNAPSHOT.prompt_bindings)),
        "profile": json.dumps(pinned_to_json(SNAPSHOT.execution_profile)),
        "digest": "d" * 64,
    }


async def _store_snapshot(env: Env) -> None:
    engine = env.engine()
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(INSERT_POLICY_SNAPSHOT),
                policy_snapshot_values(RUN_ATTEMPTED, SNAPSHOT.snapshot_id),
            )
    finally:
        await engine.dispose()


@pytest.fixture
def env() -> Iterator[Env]:
    url = os.environ.get("TEST_DATABASE_URL")
    if url is None:
        pytest.skip("set TEST_DATABASE_URL to store context manifests")
    with portal_schema(url, None) as schema:
        asyncio.run(_store_snapshot(schema))
        yield schema


def _messages_sha256(messages: tuple[ChatMessage, ...]) -> str:
    # The canonical JSON of the exact messages the transport received, in order.
    canonical = json.dumps(
        [{"role": message.role, "content": message.content} for message in messages],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return sha256(canonical.encode("utf-8")).hexdigest()


# SHA-256 of the canonical JSON of the snapshot's execution profile, as stored above.
PROFILE_VERSION = sha256(
    json.dumps(
        pinned_to_json(SNAPSHOT.execution_profile), sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
).hexdigest()


def _run() -> RunCallContext:
    return RunCallContext(
        run_id=RUN_ATTEMPTED,
        workspace_id=WS_A,
        attempt=1,
        engine="fast",
        deadline=datetime.now(UTC) + timedelta(hours=1),
    )


async def _no_sleep(seconds: float) -> None:
    return None


def _stored_gateway(
    factory: async_sessionmaker[AsyncSession], transport: Transport, *, fallback: bool = False
) -> LlmGateway:
    return LlmGateway(
        LlmSettings(primary=_profile(), fallback=_profile("fallback-model") if fallback else None),
        transport,
        InMemoryUsageLedger(),
        RunTraceLlmCalls(TransactionalRunTrace(partial(SqlAlchemyRunTraceUnitOfWork, factory))),
        checkpoint=context_checkpoint,
        sleep=_no_sleep,
        manifest_store=TransactionalManifestStore(
            partial(SqlAlchemyContextManifestUnitOfWork, factory)
        ),
    )


async def _review(
    env: Env,
    transport: Transport,
    *,
    fallback: bool = False,
    context: ReviewContext | None = None,
) -> dict[str, Any]:
    engine = env.engine()
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        await review_with_gateway(
            _stored_gateway(factory, transport, fallback=fallback),
            _context(FILES, SNAPSHOT) if context is None else context,
            _run(),
        )
        async with factory() as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT id, run_id, attempt, call_id, stage, recovery_kind, "
                        "parent_call_id, policy_snapshot_id, digest, block_count, input_tokens, "
                        "blocks, omissions, schema_version, rendered_messages_sha256, "
                        "prompt_checksum, model_profile_version FROM context_manifests "
                        "ORDER BY created_at, recovery_kind"
                    )
                )
            ).mappings()
            manifests = [dict(row) for row in rows]
            actions = (
                await session.execute(
                    text(
                        "SELECT tool, request, response FROM run_actions "
                        "WHERE run_id=:run ORDER BY index"
                    ),
                    {"run": RUN_ATTEMPTED},
                )
            ).all()
    finally:
        await engine.dispose()
    return {"manifests": manifests, "actions": [tuple(action) for action in actions]}


def _ordered(manifests: list[dict[str, Any]]) -> list[dict[str, Any]]:
    order = {"none": 0, "repair": 1, "fallback": 2}
    return sorted(manifests, key=lambda row: order[row["recovery_kind"]])


@pytest.mark.integration
def test_base_and_repair_calls_each_store_a_manifest_before_the_call(env: Env) -> None:
    transport = Transport("{not json", VALID)
    observed = asyncio.run(_review(env, transport))

    base, repair = _ordered(observed["manifests"])
    assert [action[0] for action in observed["actions"]] == [
        "context.manifest",
        "llm.call",
        "context.manifest",
        "llm.call",
    ]
    for row in (base, repair):
        assert (row["run_id"], row["attempt"], row["stage"]) == (RUN_ATTEMPTED, 1, "review")
        assert row["policy_snapshot_id"] == SNAPSHOT.snapshot_id
        assert (row["omissions"], row["schema_version"]) == ([], 1)
    assert (base["recovery_kind"], base["parent_call_id"]) == ("none", None)
    assert (repair["recovery_kind"], repair["parent_call_id"]) == ("repair", base["call_id"])
    assert [block["block_id"] for block in base["blocks"]] == [
        "policy-rules",
        "file-app-main-py-990f7e41",
    ]
    assert [block["block_id"] for block in repair["blocks"]][-1] == "previous-output"
    assert (base["block_count"], repair["block_count"]) == (2, 3)
    (_, manifest_request, base_response), (_, base_call, _) = observed["actions"][:2]
    (_, _, repair_response), (_, repair_call, _) = observed["actions"][2:]
    assert manifest_request == {
        "call_id": str(base["call_id"]),
        "stage": "review",
        "policy_snapshot_id": str(SNAPSHOT.snapshot_id),
        "diff_snapshot_id": None,
    }
    assert base_response == {
        "manifest_id": str(base["id"]),
        "digest": base["digest"],
        "block_count": 2,
        "input_tokens": base["input_tokens"],
        "tokens_by_role": {"policy_document": _tokens(RULES_BLOCK), "diff": _tokens(FILE_BLOCK)},
    }
    # The repair repeats the base blocks and adds the shown previous answer as data.
    assert repair_response["tokens_by_role"] == {
        "policy_document": _tokens(RULES_BLOCK),
        "diff": _tokens(FILE_BLOCK),
        "previous_output": _tokens("{not json"),
    }
    assert repair_response["manifest_id"] == str(repair["id"])
    # E-3: each llm.call names its own call, stage, snapshot and manifest.
    for call, row in ((base_call, base), (repair_call, repair)):
        assert {key: call[key] for key in ("call_id", "stage", "policy_snapshot_id")} == {
            "call_id": str(row["call_id"]),
            "stage": "review",
            "policy_snapshot_id": str(SNAPSHOT.snapshot_id),
        }
        assert call["manifest_id"] == str(row["id"])
        assert call["input_tokens_estimate"] == row["input_tokens"]
        assert call["operation"] == "review"
    assert base["digest"] != repair["digest"] and len(base["digest"]) == 64
    # CVS §5.1: the exact messages of each call, the review prompt's pin, the pinned profile.
    for row, request in zip((base, repair), transport.requests, strict=True):
        assert row["rendered_messages_sha256"] == _messages_sha256(request.messages)
        assert row["prompt_checksum"] == SNAPSHOT.prompt_bindings.review_system_checksum
        assert row["model_profile_version"] == PROFILE_VERSION


@pytest.mark.integration
def test_a_retry_reuses_the_manifest_of_its_call(env: Env) -> None:
    class Flaky(Transport):
        async def complete(self, request: Any, *, checkpoint: Any) -> Any:
            if not self.requests:
                self.requests.append(request)
                raise TransportUnavailable("upstream 503", http_status=503)
            return await super().complete(request, checkpoint=checkpoint)

    observed = asyncio.run(_review(env, Flaky(VALID)))

    [row] = observed["manifests"]
    assert [action[0] for action in observed["actions"]] == [
        "context.manifest",
        "llm.call",
        "llm.call",
    ]
    calls = [request for tool, request, _ in observed["actions"] if tool == "llm.call"]
    assert [call["kind"] for call in calls] == ["primary", "retry"]
    assert {call["manifest_id"] for call in calls} == {str(row["id"])}
    assert {call["call_id"] for call in calls} == {str(row["call_id"])}


@pytest.mark.integration
def test_a_fallback_call_stores_its_own_manifest(env: Env) -> None:
    observed = asyncio.run(
        _review(
            env,
            Transport("{not json", "{still not json", json.dumps(EMPTY_ANSWER)),
            fallback=True,
        )
    )

    base, repair, fallback = _ordered(observed["manifests"])
    assert fallback["recovery_kind"] == "fallback"
    assert fallback["parent_call_id"] == base["call_id"]
    assert [block["block_id"] for block in fallback["blocks"]] == [
        block["block_id"] for block in base["blocks"]
    ]
    assert len({base["call_id"], repair["call_id"], fallback["call_id"]}) == 3


# The RAG slot of a snapshot-path prompt: a place at the head and the base definition it
# read (CVS §3.5), named as the snapshot path names them. ``tokens`` is the stage's count;
# the manifest counts the rendered excerpt with the pinned counter (D5).
BASE_SHA = "e" * 40
RAG_DEFINITION = RetrievedBlock(
    block_id="RAG:eeeeeeeeeeee:1b2c3d4e5f607182",
    level=ContextLevel.RAG,
    source=SourceRef(
        role=SourceRole.RETRIEVED_EXCERPT,
        repository_id=SNAPSHOT.repository_id,
        revision=BASE_SHA,
        path="app/defs.py",
        blob_sha=None,
        origin_paths=(),
        side=DiffSide.LEFT,
        ranges=(LineRange(1, 1),),
        reason="rag_broken_reference",
    ),
    representation=Representation.SIGNATURE,
    text="def helper(value):",
    tokens=1,
    edge_kind=EdgeKind.USES_IMPORTED,
    index_commit_sha="d" * 40,
)
RAG_PLACE = RetrievedBlock(
    block_id="RAG:head_sha_123:0a1b2c3d4e5f6071",
    level=ContextLevel.RAG,
    source=replace(
        RAG_DEFINITION.source,
        revision="head_sha_123",
        path="app/caller.py",
        side=DiffSide.RIGHT,
        ranges=(LineRange(3, 4), LineRange(7, 7)),
    ),
    representation=Representation.EXACT_LINES,
    text="def caller():\n    x = helper(1)\n    return x",
    tokens=1,
    edge_kind=EdgeKind.IMPORTS,
    index_commit_sha="d" * 40,
    definitions=(RAG_DEFINITION.source,),
)
# The two excerpts as the retrieved section renders them, counted with counter/v2.
RAG_PLACE_BLOCK = (
    '<excerpt block_id="RAG:head_sha_123:0a1b2c3d4e5f6071" level="RAG" path="app/caller.py" '
    'revision="head_sha_123" side="RIGHT" representation="exact_lines">\n'
    '<line n="3">def caller():</line>\n'
    '<line n="4">    x = helper(1)</line>\n'
    '<line n="7">    return x</line>\n'
    "</excerpt>"
)
RAG_DEFINITION_BLOCK = (
    '<excerpt block_id="RAG:eeeeeeeeeeee:1b2c3d4e5f607182" level="RAG" path="app/defs.py" '
    f'revision="{BASE_SHA}" side="LEFT" representation="signature">\n'
    '<line n="1">def helper(value):</line>\n'
    "</excerpt>"
)


@pytest.mark.integration
def test_retrieved_blocks_are_recorded_with_their_selection_and_rendered_tokens(
    env: Env,
) -> None:
    transport = Transport(VALID)
    context = replace(
        _context(FILES, SNAPSHOT),
        pr_meta=replace(_meta(), diff_old_sha=BASE_SHA),
        retrieved=(RAG_PLACE, RAG_DEFINITION),
        retrieved_section=True,
    )

    observed = asyncio.run(_review(env, transport, context=context))

    [row] = observed["manifests"]
    # The counted text is the shown text: both excerpts are in the call's user message.
    [request] = transport.requests
    assert RAG_PLACE_BLOCK + "\n" + RAG_DEFINITION_BLOCK in request.messages[1].content
    assert [block["block_id"] for block in row["blocks"]] == [
        "policy-rules",
        "file-app-main-py-990f7e41",
        RAG_PLACE.block_id,
        RAG_DEFINITION.block_id,
    ]
    place, definition = row["blocks"][2:]
    assert place == {
        "block_id": RAG_PLACE.block_id,
        "source_role": "retrieved_excerpt",
        "source_kind": "source_code",
        "path": "app/caller.py",
        "revision": "head_sha_123",
        "side": "RIGHT",
        "content_sha256": None,
        "start_line": 3,
        "end_line": 7,
        "shown_line_count": 3,
        "shown_ranges": [[3, 4], [7, 7]],
        "tokens": _tokens(RAG_PLACE_BLOCK),
        "repository_id": str(SNAPSHOT.repository_id),
        # A RAG block is only ever dropped whole (D5).
        "truncated": False,
        "selection_reason": "rag_broken_reference",
        "index_commit_sha": "d" * 40,
        "edge_kind": "imports",
    }
    assert definition == {
        **place,
        "block_id": RAG_DEFINITION.block_id,
        "path": "app/defs.py",
        "revision": BASE_SHA,
        "side": "LEFT",
        "start_line": 1,
        "end_line": 1,
        "shown_line_count": 1,
        "shown_ranges": [[1, 1]],
        "tokens": _tokens(RAG_DEFINITION_BLOCK),
        "edge_kind": "uses_imported",
    }
    [(_, _, response)] = [
        action for action in observed["actions"] if action[0] == "context.manifest"
    ]
    retrieved_tokens = _tokens(RAG_PLACE_BLOCK) + _tokens(RAG_DEFINITION_BLOCK)
    assert response["tokens_by_role"] == {
        "policy_document": _tokens(RULES_BLOCK),
        "diff": _tokens(FILE_BLOCK),
        "retrieved_excerpt": retrieved_tokens,
    }
    # The retrieved share is part of the call's input tokens, as every counted role is.
    assert response["input_tokens"] == row["input_tokens"]
    assert sum(response["tokens_by_role"].values()) <= row["input_tokens"]


RETRIEVED = ManifestBlock(
    block_id="RAG:app/caller.py:3-9",
    source_role=SourceRole.RETRIEVED_EXCERPT,
    source_kind="source_code",
    path="app/caller.py",
    revision="c" * 40,
    side="LEFT",
    start_line=3,
    end_line=9,
    shown_line_count=5,
    shown_ranges=((3, 4), (7, 9)),
    tokens=12,
    repository_id=REPO_A,
    truncated=True,
    # ADR 0001 §9: the selection metadata of a retrieved block, next to its source ref.
    selection_reason="rag_reverse_edge",
    index_commit_sha="d" * 40,
    edge_kind="uses_imported",
)


def _entry(manifest: CallManifest, manifest_id: UUID) -> ManifestEntry:
    return ManifestEntry(
        manifest_id=manifest_id,
        attempt=1,
        policy_snapshot_id=SNAPSHOT.snapshot_id,
        manifest=manifest,
        parent_call_id=None,
        input_tokens=40,
        started_at=datetime.now(UTC),
        rendered_messages_sha256="1" * 64,
        prompt_checksum="2" * 64,
        model_profile_version="3" * 64,
    )


async def _record(env: Env, *entries: ManifestEntry) -> dict[str, Any]:
    """Store the entries through the transactional store; return the rows and the steps."""
    engine = env.engine()
    factory = async_sessionmaker(engine, expire_on_commit=False)
    store = TransactionalManifestStore(partial(SqlAlchemyContextManifestUnitOfWork, factory))
    try:
        for entry in entries:
            await store.record_manifest(RUN_ATTEMPTED, entry)
        async with factory() as session:
            rows = await session.execute(
                text(
                    "SELECT id, stage, recovery_kind, blocks, digest, input_tokens, "
                    "rendered_messages_sha256, prompt_checksum, model_profile_version "
                    "FROM context_manifests ORDER BY id"
                )
            )
            manifests = [dict(row) for row in rows.mappings()]
            steps = await session.execute(
                text(
                    "SELECT response FROM run_actions WHERE run_id=:run AND "
                    "tool='context.manifest' ORDER BY index"
                ),
                {"run": RUN_ATTEMPTED},
            )
            responses = [row[0] for row in steps]
    finally:
        await engine.dispose()
    return {"manifests": manifests, "responses": responses}


@pytest.mark.integration
def test_a_retrieved_excerpt_block_round_trips_with_its_side_ranges_and_repository(
    env: Env,
) -> None:
    manifest = CallManifest(
        call_id=UUID(int=901), stage="verification", blocks=(RETRIEVED,), run_id=RUN_ATTEMPTED
    )
    changed = replace(manifest, call_id=UUID(int=902), blocks=(replace(RETRIEVED, tokens=13),))

    first, second = asyncio.run(
        _record(env, _entry(manifest, UUID(int=911)), _entry(changed, UUID(int=912)))
    )["manifests"]

    assert (first["id"], first["stage"], first["recovery_kind"]) == (
        UUID(int=911),
        "verification",
        "none",
    )
    assert first["input_tokens"] == 40
    [stored] = first["blocks"]
    assert stored == {
        "block_id": "RAG:app/caller.py:3-9",
        "source_role": "retrieved_excerpt",
        "source_kind": "source_code",
        "path": "app/caller.py",
        "revision": "c" * 40,
        "side": "LEFT",
        "content_sha256": None,
        "start_line": 3,
        "end_line": 9,
        "shown_line_count": 5,
        "shown_ranges": [[3, 4], [7, 9]],
        "tokens": 12,
        "repository_id": str(REPO_A),
        "truncated": True,
        "selection_reason": "rag_reverse_edge",
        "index_commit_sha": "d" * 40,
        "edge_kind": "uses_imported",
    }
    rebuilt = ManifestBlock(
        **{
            **stored,
            "source_role": SourceRole(stored["source_role"]),
            "shown_ranges": tuple(tuple(item) for item in stored["shown_ranges"]),
            "repository_id": UUID(stored["repository_id"]),
        }
    )
    assert rebuilt == RETRIEVED
    # SHA-256 of the canonical JSON of the blocks (sorted keys, no spaces; PIPELINE_SPEC §2),
    # computed apart from the store from the literal row above.
    assert first["digest"] == "8b07db8bbf86d9bbf012aae76d9232cd0188a0c1252ccae26af633976683b0c2"
    # The digest follows the blocks: one changed token count gives another digest.
    assert first["digest"] != second["digest"]
    assert (
        first["rendered_messages_sha256"],
        first["prompt_checksum"],
        first["model_profile_version"],
    ) == ("1" * 64, "2" * 64, "3" * 64)


@pytest.mark.integration
def test_blocks_without_a_token_count_stay_out_of_tokens_by_role(env: Env) -> None:
    rules = ManifestBlock(
        block_id="policy-rules",
        source_role=SourceRole.POLICY_DOCUMENT,
        source_kind="repository_rules",
        path=".review/rules.md",
    )
    manifest = CallManifest(
        call_id=UUID(int=903),
        stage="verification",
        blocks=(RETRIEVED, replace(RETRIEVED, block_id="RAG:app/other.py:1-2", tokens=None), rules),
        run_id=RUN_ATTEMPTED,
    )

    [response] = asyncio.run(_record(env, _entry(manifest, UUID(int=913))))["responses"]

    # The uncounted retrieved block adds nothing; a role with no counted block is absent.
    assert response["tokens_by_role"] == {"retrieved_excerpt": 12}
    assert response["block_count"] == 3


@pytest.mark.integration
def test_the_step_locks_the_run_before_the_row_takes_its_foreign_key(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    # insert_run_action takes SELECT ... FOR UPDATE on the run. A manifest row flushed before it
    # already holds KEY SHARE on the run, and two writers of one run then deadlock.
    rows_before_the_step: list[int] = []

    async def observed(session: AsyncSession, **kwargs: Any) -> None:
        count = await session.scalar(text("SELECT count(*) FROM context_manifests"))
        rows_before_the_step.append(int(count or 0))
        await insert_run_action(session, **kwargs)

    monkeypatch.setattr(
        "app.modules.reviews.infrastructure.context_manifest_store.insert_run_action", observed
    )
    manifest = CallManifest(
        call_id=UUID(int=904), stage="review", blocks=(RETRIEVED,), run_id=RUN_ATTEMPTED
    )

    stored = asyncio.run(_record(env, _entry(manifest, UUID(int=914))))

    assert rows_before_the_step == [0]
    assert [row["id"] for row in stored["manifests"]] == [UUID(int=914)]
    assert len(stored["responses"]) == 1


@pytest.mark.integration
def test_a_manifest_names_a_stored_snapshot_of_its_run(env: Env) -> None:
    manifest = CallManifest(
        call_id=UUID(int=905), stage="review", blocks=(RETRIEVED,), run_id=RUN_ATTEMPTED
    )
    unknown = replace(_entry(manifest, UUID(int=915)), policy_snapshot_id=UUID(int=999))

    with pytest.raises(IntegrityError):
        asyncio.run(_record(env, unknown))


def test_the_prompt_checksum_is_the_pin_of_the_calls_stage() -> None:
    bindings = replace(
        SNAPSHOT.prompt_bindings,
        review_system_checksum="r" * 64,
        conventions_checksum="c" * 64,
        verifier_checksum="v" * 64,
    )

    assert stage_prompt_checksum(bindings, "review") == "r" * 64
    assert stage_prompt_checksum(bindings, "conventions") == "c" * 64
    assert stage_prompt_checksum(bindings, "verification") == "v" * 64


class _BrokenStore:
    def __init__(self) -> None:
        self.entries: list[ManifestEntry] = []

    async def record_manifest(self, run_id: UUID, entry: ManifestEntry) -> None:
        self.entries.append(entry)
        raise ConnectionError("the database is gone")


def _gateway(transport: Transport, store: Any = None) -> tuple[LlmGateway, InMemoryLlmCallTrace]:
    trace = InMemoryLlmCallTrace()
    gateway = LlmGateway(
        LlmSettings(primary=_profile(), fallback=None),
        transport,
        InMemoryUsageLedger(),
        trace,
        checkpoint=context_checkpoint,
        manifest_store=store,
    )
    return gateway, trace


def test_a_failed_manifest_write_means_no_provider_call() -> None:
    transport = Transport(VALID)
    store = _BrokenStore()
    gateway, trace = _gateway(transport, store)

    with pytest.raises(RunFailure) as raised:
        asyncio.run(review_with_gateway(gateway, _context(FILES, SNAPSHOT), _run()))

    assert raised.value.error_code == "internal_error"
    assert len(store.entries) == 1
    assert transport.requests == []
    assert trace.records == []


def test_concurrent_snapshot_generations_of_one_attempt_stay_within_four_calls() -> None:
    # The twin of test_llm_gateway's case with a manifest store: the call's slot is claimed
    # before the store write, so a generation held in the write keeps its slot. The snapshot
    # review keeps one of the attempt's four slots for the verifier (reserved_calls=1), so
    # both generations together get at most three, and the fourth is left for the verifier.
    class PausingStore:
        """Hold the first manifest write until the other generation has spent the cap."""

        def __init__(self) -> None:
            self.entries: list[ManifestEntry] = []
            self.entered = asyncio.Event()
            self.resume = asyncio.Event()

        async def record_manifest(self, run_id: UUID, entry: ManifestEntry) -> None:
            self.entries.append(entry)
            if not self.entered.is_set():
                self.entered.set()
                await self.resume.wait()

    class Unavailable(Transport):
        async def complete(self, request: Any, *, checkpoint: Any) -> Any:
            await checkpoint()
            self.requests.append(request)
            raise TransportUnavailable("upstream 503", http_status=503)

    store = PausingStore()
    transport = Unavailable()
    trace = InMemoryLlmCallTrace()
    gateway = LlmGateway(
        LlmSettings(primary=_profile(), fallback=_profile("fallback-model")),
        transport,
        InMemoryUsageLedger(),
        trace,
        checkpoint=context_checkpoint,
        sleep=_no_sleep,
        manifest_store=store,
    )
    run = _run()
    context = _context(FILES, SNAPSHOT)

    verifier = StructuredTask(
        operation="verification",
        messages=(ChatMessage("system", "SYS"), ChatMessage("user", "USER")),
        schema=ResponseSchema("Answer", {"type": "object"}),
        validate=lambda answer: {},
        route="verifier",
    )

    async def scenario() -> tuple[LlmCallFailed, LlmCallFailed, LlmCallFailed]:
        blocked = asyncio.create_task(review_with_gateway(gateway, context, run))
        await store.entered.wait()
        try:
            with pytest.raises(LlmCallFailed) as active:
                await review_with_gateway(gateway, context, run)
        finally:
            store.resume.set()
        with pytest.raises(LlmCallFailed) as held:
            await blocked
        with pytest.raises(LlmCallFailed) as verified:
            await gateway.generate(verifier, run)
        return active.value, held.value, verified.value

    active, held, verified = asyncio.run(scenario())

    # The held generation kept the slot it claimed: the other one got two (its primary call
    # and its fallback), never the verifier's slot.
    assert (active.calls, held.calls) == (2, 1)
    # Each call keeps the number of the slot it claimed, also across the held write; the
    # verifier gets the fourth and last.
    assert verified.calls == 1
    assert sorted(record.call_no for _, record in trace.records) == [1, 2, 3, 4]
    assert [record.operation for _, record in trace.records].count("verification") == 1
    assert len(transport.requests) == 4
    assert [entry.manifest.recovery_kind for entry in store.entries] == ["none", "none", "fallback"]


def test_a_legacy_task_writes_no_manifest_and_names_none_in_llm_call() -> None:
    class Recording:
        def __init__(self) -> None:
            self.entries: list[ManifestEntry] = []

        async def record_manifest(self, run_id: UUID, entry: ManifestEntry) -> None:
            self.entries.append(entry)

    store = Recording()
    legacy = json.dumps(
        {
            "findings": [],
            "summary": {"problem": "Nothing found.", "done_well": "Small.", "effort": "none"},
        }
    )
    gateway, trace = _gateway(Transport(legacy), store)

    asyncio.run(review_with_gateway(gateway, _context(FILES, None), _run()))

    assert store.entries == []
    [(_, record)] = trace.records
    request = record.request_json()
    assert not {"call_id", "stage", "policy_snapshot_id", "manifest_id"} & set(request)


def test_a_snapshot_task_without_a_store_names_its_call_but_no_manifest() -> None:
    gateway, trace = _gateway(Transport(VALID))

    asyncio.run(review_with_gateway(gateway, _context(FILES, SNAPSHOT), _run()))

    [(_, record)] = trace.records
    request = record.request_json()
    assert request["stage"] == "review"
    assert request["policy_snapshot_id"] == str(SNAPSHOT.snapshot_id)
    assert request["manifest_id"] is None
    assert isinstance(request["call_id"], str)


def test_llm_call_record_adds_the_e3_keys_only_for_a_named_call() -> None:
    base = LlmCallRecord(
        kind=LlmCallKind.PRIMARY,
        model="m",
        call_no=1,
        attempt=1,
        timeout_s=90.0,
        prompt_version_id=None,
        rule_version_id=None,
        input_tokens_estimate=10,
        started_at=datetime.now(UTC),
        duration_ms=1,
        operation="verification",
    )
    named = replace(
        base,
        call_id=UUID(int=1),
        stage="verification",
        policy_snapshot_id=UUID(int=2),
        manifest_id=UUID(int=3),
    )

    assert "call_id" not in base.request_json()
    assert {
        key: named.request_json()[key]
        for key in ("call_id", "stage", "policy_snapshot_id", "manifest_id")
    } == {
        "call_id": str(UUID(int=1)),
        "stage": "verification",
        "policy_snapshot_id": str(UUID(int=2)),
        "manifest_id": str(UUID(int=3)),
    }
