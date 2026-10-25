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
from typing import Any
from uuid import UUID

import pytest
from app.modules.reviews.infrastructure.context_manifest_store import (
    SqlAlchemyContextManifestUnitOfWork,
)
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.reviews.application.context_provenance import SourceRole
from app.modules.reviews.application.llm import (
    LlmCallKind,
    LlmCallRecord,
    ManifestEntry,
    RunCallContext,
    TransactionalManifestStore,
)
from app.modules.reviews.application.policy_snapshot import CallManifest, ManifestBlock
from app.modules.reviews.application.prompt_builder import ChangedFile, DiffLine
from app.modules.reviews.application.run_failures import RunFailure
from app.modules.reviews.application.run_trace import TransactionalRunTrace
from app.modules.reviews.infrastructure.llm.gateway import LlmGateway
from app.modules.reviews.infrastructure.llm.memory import InMemoryLlmCallTrace, InMemoryUsageLedger
from app.modules.reviews.infrastructure.llm.models import review_with_gateway
from app.modules.reviews.infrastructure.llm.settings import LlmSettings
from app.modules.reviews.infrastructure.llm.transport import TransportUnavailable
from app.modules.reviews.infrastructure.llm_call_trace import RunTraceLlmCalls
from app.modules.reviews.infrastructure.run_lifecycle_store import SqlAlchemyRunTraceUnitOfWork
from tests.llm_fakes import context_checkpoint
from tests.portal_postgres import REPO_A, RUN_ATTEMPTED, WS_A, Env, portal_schema
from tests.test_gateway_review_model import (
    EMPTY_ANSWER,
    MAIN,
    SUMMARY,
    Transport,
    _context,
    _finding,
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


@pytest.fixture
def env() -> Iterator[Env]:
    url = os.environ.get("TEST_DATABASE_URL")
    if url is None:
        pytest.skip("set TEST_DATABASE_URL to store context manifests")
    with portal_schema(url, None) as schema:
        yield schema


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


async def _review(env: Env, transport: Transport, *, fallback: bool = False) -> dict[str, Any]:
    engine = env.engine()
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        await review_with_gateway(
            _stored_gateway(factory, transport, fallback=fallback),
            _context(FILES, SNAPSHOT),
            _run(),
        )
        async with factory() as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT id, run_id, attempt, call_id, stage, recovery_kind, "
                        "parent_call_id, policy_snapshot_id, digest, block_count, input_tokens, "
                        "blocks, omissions, schema_version FROM context_manifests "
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
    observed = asyncio.run(_review(env, Transport("{not json", VALID)))

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
    )


@pytest.mark.integration
def test_a_retrieved_excerpt_block_round_trips_with_its_side_ranges_and_repository(
    env: Env,
) -> None:
    manifest = CallManifest(
        call_id=UUID(int=901), stage="verification", blocks=(RETRIEVED,), run_id=RUN_ATTEMPTED
    )
    changed = replace(manifest, call_id=UUID(int=902), blocks=(replace(RETRIEVED, tokens=13),))

    async def scenario() -> list[dict[str, Any]]:
        engine = env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        store = TransactionalManifestStore(partial(SqlAlchemyContextManifestUnitOfWork, factory))
        try:
            await store.record_manifest(RUN_ATTEMPTED, _entry(manifest, UUID(int=911)))
            await store.record_manifest(RUN_ATTEMPTED, _entry(changed, UUID(int=912)))
            async with factory() as session:
                rows = await session.execute(
                    text(
                        "SELECT id, stage, recovery_kind, blocks, digest, input_tokens "
                        "FROM context_manifests ORDER BY id"
                    )
                )
                return [dict(row) for row in rows.mappings()]
        finally:
            await engine.dispose()

    first, second = asyncio.run(scenario())

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
    # The digest follows the blocks: one changed token count gives another digest.
    assert first["digest"] != second["digest"]


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
