"""Bitbucket index eligibility is portal scoped and revoked per binding (#333)."""

from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import Connection, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from alembic import command
from app.bootstrap.bitbucket_reviews import BitbucketReviewWorkerResources
from app.bootstrap.code_index_worker import CodeIndexSettings, index_versions
from app.bootstrap.llm_gateway import build_gateway
from app.bootstrap.seed_prompts import load_prompt_assets, seed_prompt_versions
from app.modules.code_index.application.build_index import BuildRepositoryIndex, BuildStatus
from app.modules.code_index.application.request_bitbucket_index_refresh import (
    RequestBitbucketIndexRefresh,
)
from app.modules.code_index.application.vcs import VcsAccessDenied
from app.modules.code_index.infrastructure.bitbucket_index_refresh import (
    SqlAlchemyBitbucketIndexRefreshUnitOfWork,
)
from app.modules.code_index.infrastructure.bitbucket_snapshot_source import (
    BitbucketRepositorySnapshotSource,
)
from app.modules.code_index.infrastructure.graph_retriever import SqlAlchemyGraphRetriever
from app.modules.code_index.infrastructure.index_store import (
    SqlAlchemyCodeIndexStore,
    SqlAlchemyCodeIndexUnitOfWork,
)
from app.modules.code_index.infrastructure.maintenance import SqlAlchemyCodeIndexMaintenance
from app.modules.code_index.infrastructure.models import CodeIndexState
from app.modules.code_index.infrastructure.repository_reader import SqlAlchemyIndexRepositoryReader
from app.modules.code_index.infrastructure.review_index import SqlAlchemyReviewIndexAccess
from app.modules.code_index.infrastructure.tar_archive import TarArchiveReader
from app.modules.integrations.bitbucket.infrastructure.connection_store import (
    SqlAlchemyBitbucketConnectionStore,
)
from app.modules.integrations.bitbucket.infrastructure.credential_cipher import (
    BitbucketCredentialCipher,
    CredentialContext,
    CredentialPurpose,
)
from app.modules.integrations.bitbucket.infrastructure.models import (
    BitbucketConnection,
    BitbucketRepositoryWebhookSecret,
)
from app.modules.integrations.bitbucket.infrastructure.request_credentials import (
    BitbucketRequestCredentials,
    SqlAlchemyBitbucketTargetReader,
)
from app.modules.repositories.infrastructure.models import Repository
from app.modules.repositories.infrastructure.repository_settings import (
    SqlAlchemyRepositorySettingsStore,
)
from app.modules.reviews.application.context_provenance import SourceRole
from app.modules.reviews.application.get_run_diff import DiffSnapshot, StoreDiffSnapshot
from app.modules.reviews.application.graph_retriever import GraphTarget
from app.modules.reviews.application.handle_review_run import ClaimedAttempt
from app.modules.reviews.application.policy_errors import PolicyError, PolicyErrorCode
from app.modules.reviews.application.prompt_budget import verify_provenance_pre_send_guard
from app.modules.reviews.application.prompt_builder import PromptBuilder, file_block_id
from app.modules.reviews.application.review_context import snapshot_retrieved
from app.modules.reviews.application.run_trace import TransactionalRunTrace
from app.modules.reviews.application.try_enqueue_webhook_run import (
    PendingRunMessage,
    RunInsertCandidate,
)
from app.modules.reviews.infrastructure.llm.settings import LlmSettings
from app.modules.reviews.infrastructure.run_lifecycle_store import SqlAlchemyRunTraceUnitOfWork
from app.modules.reviews.infrastructure.run_processing_unit_of_work import (
    SqlAlchemyRunProcessingUnitOfWork,
)
from app.modules.reviews.infrastructure.run_repository import SqlAlchemyRunRepository
from app.modules.reviews.infrastructure.subprocess_source_parser import SubprocessSourceParser
from app.modules.reviews.infrastructure.webhook_runs import SqlAlchemyWebhookRunStore
from app.worker import (
    BoundVcsProviderSelector,
    attempt_model_provider,
    execution_profile_of,
    process_review_run,
)
from tests.test_bitbucket_connections_postgres import _config
from tests.test_llm_gateway import PRIMARY, completion
from tests.test_prompt_builder import _metadata_context

pytest_plugins = ("tests.test_bitbucket_connections_postgres",)

_HEAD = "a" * 40
_LIB = b"def target():\n    return 1\n"
_USER = b"from lib import target\n\n\ndef use():\n    return target()\n"
_OTHER_LIB = b"def other():\n    return 2\n"
_OTHER_USER = b"from lib import other\n\n\ndef use():\n    return other()\n"


class _Wakeups:
    async def wake(self, repository_id: UUID, generation: int) -> None:
        return None


class _ModelHttp:
    def __init__(self) -> None:
        self.bodies: list[dict[str, Any]] = []

    def respond(self, request: httpx.Request) -> httpx.Response:
        body: dict[str, Any] = json.loads(request.content)
        self.bodies.append(body)
        if len(self.bodies) == 1:
            answer: dict[str, Any] = {
                "files": [{"path": "lib.py", "relevance": "changed function"}],
                "key_patterns": [
                    "Callers depend on target",
                    "Return values matter",
                    "Keep imports",
                ],
                "recommendations": [
                    f"Check {index} (from: standard/correctness)" for index in range(5)
                ],
            }
        elif len(self.bodies) == 2:
            prompt = body["messages"][-1]["content"]
            [rag] = re.findall(r'<excerpt block_id="([^"]+)" level="RAG"', prompt)
            answer = {
                "findings": [
                    {
                        "path": "lib.py",
                        "line": 2,
                        "start_line": None,
                        "severity": "high",
                        "category": "correctness",
                        "title": "Changed return affects callers",
                        "body": "The caller relies on target's return value.",
                        "suggestion": None,
                        "confidence": 0.9,
                        "rule_name": None,
                        "evidence_refs": [
                            {"block_id": file_block_id("lib.py"), "start_line": 2, "end_line": 2},
                            {"block_id": rag, "start_line": 5, "end_line": 5},
                        ],
                    }
                ],
                "summary": {
                    "problem": "A caller may be affected.",
                    "done_well": "Small change.",
                    "effort": "small",
                },
            }
        else:
            prompt = body["messages"][-1]["content"]
            [candidate] = re.findall(r'candidate_id="([^"]+)"', prompt)
            answer = {
                "decisions": [
                    {
                        "candidate_id": candidate,
                        "status": "supported",
                        "reason": "The changed return is used by the caller.",
                        "evidence_refs": [
                            {"block_id": file_block_id("lib.py"), "start_line": 2, "end_line": 2}
                        ],
                        "requirement_refs": [],
                    }
                ]
            }
        return completion(json.dumps(answer))


class _AcceptedOutput:
    async def execute(self, run_id: UUID, raw_output: object) -> bool:
        assert raw_output is not None
        return True


def _assert_verifier_manifest_shown(prompt: str, blocks: list[dict[str, Any]]) -> None:
    """Every persisted verifier block must have its own rendered envelope in the sent prompt."""
    tags = {
        "service_defaults": "service_defaults",
        "diff": "file",
        "source_excerpt": "excerpt",
        "ast_excerpt": "excerpt",
        "retrieved_excerpt": "excerpt",
    }
    for block in blocks:
        role = str(block["source_role"])
        block_id = str(block["block_id"])
        if role == "policy_document":
            tag = str(block["source_kind"])
            assert tag in {"repository_rules", "project_conventions_document"}
        elif role == "conventions":
            assert block_id == "repo-conventions"
            assert "<repo_conventions>" in prompt and "</repo_conventions>" in prompt
            continue
        elif role == "candidate":
            assert re.search(rf'<candidate candidate_id="{re.escape(block_id)}"(?:\s|>)', prompt), (
                f"candidate {block_id} not shown"
            )
            continue
        else:
            assert role in tags, f"unknown manifest role {role}"
            tag = tags[role]
        assert re.search(rf'<{tag}\b[^>]*\bblock_id="{re.escape(block_id)}"', prompt), (
            f"{role} {block_id} not shown"
        )


@pytest.mark.integration
def test_two_portal_workspaces_index_same_remote_uuid_without_cross_tenant_reads(
    bitbucket_connection_schema: Connection,
) -> None:
    """A revoked tenant stops indexing while its sibling keeps its own credential."""
    db = bitbucket_connection_schema
    command.upgrade(_config(db), "head")
    workspaces = (uuid4(), uuid4())
    installations = (uuid4(), uuid4())
    remote_uuid = uuid4()
    for workspace in workspaces:
        db.execute(
            text("INSERT INTO workspaces (id, name, daily_budget_usd) VALUES (:id, :name, 7)"),
            {"id": workspace, "name": str(workspace)},
        )
    schema = str(db.scalar(text("SELECT current_schema()")))
    db.commit()
    database_url = make_url(os.environ["TEST_DATABASE_URL"]).set(
        drivername="postgresql+psycopg_async"
    )
    engine = create_async_engine(database_url, connect_args={"options": f"-csearch_path={schema}"})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    cipher = BitbucketCredentialCipher(active_key_id="test", keys={"test": b"a" * 32})

    async def scenario() -> None:
        try:
            ids: list[UUID] = []
            async with factory.begin() as session:
                store = SqlAlchemyBitbucketConnectionStore(session)
                for index, (workspace, installation) in enumerate(
                    zip(workspaces, installations, strict=True)
                ):
                    await store.create_connection(
                        workspace_id=workspace,
                        installation_id=installation,
                        bitbucket_workspace_uuid=uuid4(),
                        token=cipher.encrypt(
                            f"tenant-{index}",
                            context=CredentialContext(installation, workspace),
                            purpose=CredentialPurpose.TOKEN,
                        ),
                    )
                    repository_id, _ = await store.bind_repository(
                        workspace_id=workspace,
                        installation_id=installation,
                        repository_uuid=remote_uuid,
                        full_name="nvdovnar/same-repo",
                        default_branch="main",
                        web_url="https://bitbucket.org/nvdovnar/same-repo",
                    )
                    ids.append(repository_id)
                    await session.execute(
                        update(Repository)
                        .where(Repository.id == repository_id)
                        .values(rag_enabled=True)
                    )
                    if index == 1:
                        generation = await store.set_repository_webhook_secret(
                            workspace_id=workspace,
                            installation_id=installation,
                            repository_id=repository_id,
                            secret=cipher.encrypt(
                                "hook-secret-012345678901234567890123",
                                context=CredentialContext(installation, workspace, repository_id),
                                purpose=CredentialPurpose.WEBHOOK_SECRET,
                            ),
                        )
                        assert generation == 2
                        await session.execute(
                            update(BitbucketRepositoryWebhookSecret)
                            .where(BitbucketRepositoryWebhookSecret.repository_id == repository_id)
                            .values(confirmed_at=text("clock_timestamp()"))
                        )
            reader = SqlAlchemyBitbucketTargetReader(factory)
            credentials = BitbucketRequestCredentials(reader, cipher)
            targets = [
                await reader.resolve_repository_target(repository_id) for repository_id in ids
            ]
            assert targets[0].repository_uuid == targets[1].repository_uuid == remote_uuid
            assert targets[0].workspace_id != targets[1].workspace_id
            assert [(await credentials.credential_for(target)).token for target in targets] == [
                "tenant-0",
                "tenant-1",
            ]
            async with factory.begin() as session:
                maintenance = SqlAlchemyCodeIndexMaintenance(session)
                assert set(await maintenance.to_rearm(10)) == set(ids)
                review = SqlAlchemyReviewIndexAccess(session)
                assert (await review.open_for_review(ids[0])).enabled
                assert (await review.open_for_review(ids[1])).enabled
                await SqlAlchemyCodeIndexStore(session).enable(ids[0])
                await SqlAlchemyCodeIndexStore(session).enable(ids[1])
            rule_id, change_id = uuid4(), uuid4()
            async with factory.begin() as session:
                await seed_prompt_versions(
                    session,
                    load_prompt_assets(Path(__file__).resolve().parents[1] / "review/prompts"),
                )
                await session.execute(
                    text(
                        "INSERT INTO rule_versions "
                        "(id,repository_id,version,rules,checksum,is_active) "
                        "VALUES (:id,:repository,1,'[]'::jsonb,:checksum,true)"
                    ),
                    {"id": rule_id, "repository": ids[1], "checksum": "d" * 64},
                )
                await session.execute(
                    text(
                        "INSERT INTO code_changes "
                        "(id,repository_id,external_id,external_number,title,source_branch,"
                        "target_branch,base_sha,head_sha,state,web_url) VALUES "
                        "(:id,:repository,7,7,'Review','feature','main',:base,:head,'open',:url)"
                    ),
                    {
                        "id": change_id,
                        "repository": ids[1],
                        "base": "b" * 40,
                        "head": _HEAD,
                        "url": "https://bitbucket.org/nvdovnar/same-repo/pull-requests/7",
                    },
                )
            async with factory.begin() as session:
                active_version = await session.scalar(
                    text(
                        "SELECT version FROM prompt_versions "
                        "WHERE key='review.system' AND is_active=true"
                    )
                )
                assert active_version == 5
                v5_id = await session.scalar(
                    text("SELECT id FROM prompt_versions WHERE key='review.system' AND version=5")
                )
                assert v5_id is not None
                candidate = await SqlAlchemyWebhookRunStore(session).lock_candidate(change_id)
                assert isinstance(candidate, RunInsertCandidate)
                assert candidate.prompt_version_id == v5_id
                pending = await SqlAlchemyWebhookRunStore(session).insert_webhook_run(
                    candidate, datetime.now(UTC)
                )
                assert isinstance(pending, PendingRunMessage)
                await session.execute(
                    text(
                        "UPDATE runs SET state='running', attempt=1, "
                        "lease_until=clock_timestamp()+interval '5 minutes' WHERE id=:id"
                    ),
                    {"id": pending.run_id},
                )
            run_id = pending.run_id
            async with SqlAlchemyRunProcessingUnitOfWork(factory) as work:
                await StoreDiffSnapshot(work.repository).execute(
                    run_id=run_id,
                    code_change_id=change_id,
                    head_sha=_HEAD,
                    files=[
                        DiffSnapshot(
                            "lib.py",
                            "@@ -1,2 +1,2 @@\n def target():\n-    return 0\n+    return 1",
                            None,
                            "modified",
                            None,
                            1,
                            1,
                            2,
                        )
                    ],
                )
                await work.commit()

            raw_user_reads: list[str] = []

            def respond(request: httpx.Request) -> httpx.Response:
                assert request.url.host == "api.bitbucket.org"
                token = request.headers["Authorization"]
                assert token in {"Bearer tenant-0", "Bearer tenant-1"}
                lib, user = (
                    (_OTHER_LIB, _OTHER_USER) if token == "Bearer tenant-0" else (_LIB, _USER)
                )
                path = request.url.path
                if path.endswith(str(remote_uuid) + "}/"):
                    return httpx.Response(
                        200,
                        json={"uuid": "{" + str(remote_uuid) + "}", "mainbranch": {"name": "main"}},
                    )
                if path.endswith("/refs/branches/main"):
                    return httpx.Response(200, json={"target": {"hash": _HEAD}})
                if path.endswith("/pullrequests/7"):
                    return httpx.Response(
                        200,
                        json={
                            "id": 7,
                            "state": "OPEN",
                            "title": "Review target",
                            "description": "Change target",
                            "author": {"nickname": "developer"},
                            "source": {
                                "branch": {"name": "feature"},
                                "commit": {"hash": _HEAD},
                                "repository": {"uuid": "{" + str(remote_uuid) + "}"},
                            },
                            "destination": {
                                "branch": {"name": "main"},
                                "commit": {"hash": "b" * 40},
                                "repository": {"uuid": "{" + str(remote_uuid) + "}"},
                            },
                        },
                    )
                if path.endswith(f"/diffstat/{_HEAD}..{'b' * 40}"):
                    return httpx.Response(
                        200,
                        json={
                            "size": 1,
                            "page": 1,
                            "pagelen": 100,
                            "values": [
                                {
                                    "status": "modified",
                                    "lines_added": 1,
                                    "lines_removed": 1,
                                    "old": {"path": "lib.py"},
                                    "new": {"path": "lib.py", "size": len(lib)},
                                }
                            ],
                        },
                    )
                if path.endswith(f"/merge-base/{_HEAD}..{'b' * 40}"):
                    return httpx.Response(200, json={"hash": "b" * 40})
                if path.endswith("/pullrequests/7/commits"):
                    return httpx.Response(
                        200, json={"values": [{"hash": _HEAD, "message": "feat: update target"}]}
                    )
                revision = (
                    _HEAD
                    if f"/src/{_HEAD}/" in path
                    else "b" * 40
                    if f"/src/{'b' * 40}/" in path
                    else None
                )
                if revision is not None and path.endswith(f"/src/{revision}/"):
                    return httpx.Response(
                        200,
                        json={
                            "values": [
                                {
                                    "path": path,
                                    "type": "commit_file",
                                    "size": len(content),
                                    "commit": {"hash": revision},
                                }
                                for path, content in (("lib.py", lib), ("user.py", user))
                            ]
                        },
                    )
                if path.endswith("/lib.py"):
                    return httpx.Response(
                        200,
                        content=b"def target():\n    return 0\n" if revision != _HEAD else lib,
                    )
                if path.endswith("/user.py"):
                    raw_user_reads.append(path)
                    return httpx.Response(200, content=user)
                raise AssertionError(path)

            settings = CodeIndexSettings()
            parser = SubprocessSourceParser(attempt_cpu_seconds=None)
            try:
                async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
                    build = BuildRepositoryIndex(
                        uow=partial(SqlAlchemyCodeIndexUnitOfWork, factory),
                        repositories=SqlAlchemyIndexRepositoryReader(factory),
                        sources=lambda repository: BitbucketRepositorySnapshotSource(
                            client=client,
                            target_for_repository=partial(
                                reader.resolve_repository_target, repository.repository_id
                            ),
                            credentials=credentials,
                        ),
                        archives=TarArchiveReader(settings.limits),
                        parser=parser,
                        wakeups=_Wakeups(),
                        versions=index_versions(settings.limits),
                        limits=settings.limits,
                        settings=settings.build,
                    )
                    first_outcome = await build.execute(ids[0], 1)
                    assert first_outcome.status is BuildStatus.PUBLISHED, first_outcome
                    outcome = await build.execute(ids[1], 1)
                    assert outcome.status is BuildStatus.PUBLISHED, outcome
                    hints = await SqlAlchemyGraphRetriever(factory).read_hints(
                        ids[1], (GraphTarget("lib.py", "target"),)
                    )
                    assert any(hint.path == "user.py" for hint in hints.hints)
                    other = await SqlAlchemyGraphRetriever(factory).read_hints(
                        ids[0], (GraphTarget("lib.py", "target"),)
                    )
                    assert other.hints == ()
                    own = await SqlAlchemyGraphRetriever(factory).read_hints(
                        ids[0], (GraphTarget("lib.py", "other"),)
                    )
                    assert any(hint.path == "user.py" for hint in own.hints)
                    runs = SqlAlchemyRunRepository(factory, allow_unscoped=True)
                    shown_run = await runs.get_run(run_id)
                    assert shown_run is not None and shown_run.provider == "bitbucket"
                    async with factory() as settings_session:
                        shown_repository = await SqlAlchemyRepositorySettingsStore(
                            settings_session, None, allow_unscoped=True
                        ).get_repository(ids[1])
                    assert shown_repository is not None
                    assert shown_repository.provider == "bitbucket"
                    expected_input = await runs.get_run_vcs_input(run_id)
                    assert expected_input is not None and expected_input.provider == "bitbucket"

                    async def checkpoint() -> None:
                        return None

                    llm_settings = LlmSettings(primary=PRIMARY, fallback=None)
                    resources = BitbucketReviewWorkerResources(
                        session_factory=factory,
                        cipher=cipher,
                        execution_profile=execution_profile_of(llm_settings),
                        transport_factory=lambda: httpx.MockTransport(respond),
                    )
                    claimed = ClaimedAttempt(
                        run_id,
                        workspaces[1],
                        1,
                        "fast",
                        datetime.now(UTC) + timedelta(minutes=5),
                        v5_id,
                        rule_id,
                        "bitbucket-rag-worker",
                    )
                    trace = TransactionalRunTrace(partial(SqlAlchemyRunTraceUnitOfWork, factory))
                    raw_user_reads.clear()
                    async with resources.attempt(
                        claimed=claimed,
                        expected_input=expected_input,
                        checkpoint=checkpoint,
                        trace=trace,
                    ) as bundle:
                        stage = bundle.context_stage
                        await stage.begin(run_id)
                        payload = await stage.run(run_id)
                    assert payload is not None
                    # The complete Bitbucket diff proves user.py unchanged. Its
                    # revision-bound pseudo blob IDs differ, but base reads must
                    # reuse the head file instead of fetching the old raw path.
                    assert any(f"/src/{_HEAD}/user.py" in path for path in raw_user_reads)
                    assert not any(f"/src/{'b' * 40}/user.py" in path for path in raw_user_reads)
                    async with factory() as session:
                        first_payload = (
                            await session.execute(
                                text(
                                    "SELECT schema_version, summary FROM context_payloads "
                                    "WHERE run_id=:id"
                                ),
                                {"id": run_id},
                            )
                        ).all()
                        build = await session.scalar(
                            text(
                                "SELECT response FROM run_actions "
                                "WHERE run_id=:id AND tool='context.build' "
                                "ORDER BY started_at DESC LIMIT 1"
                            ),
                            {"id": run_id},
                        )
                    assert len(first_payload) == 1
                    assert first_payload[0][0] == 2
                    assert first_payload[0][1] == build
                    async with factory.begin() as session:
                        await session.execute(
                            text("UPDATE runs SET attempt=2 WHERE id=:id"), {"id": run_id}
                        )
                        stale = await session.scalar(
                            text(
                                "UPDATE context_payloads SET summary='{\"stale\": true}'::jsonb "
                                "WHERE run_id=:id RETURNING summary"
                            ),
                            {"id": run_id},
                        )
                        assert stale == {"stale": True}
                    async with resources.attempt(
                        claimed=replace(claimed, attempt=2),
                        expected_input=expected_input,
                        checkpoint=checkpoint,
                        trace=trace,
                    ) as retry_bundle:
                        await retry_bundle.context_stage.begin(run_id)
                        assert await retry_bundle.context_stage.run(run_id) is not None
                    async with factory() as session:
                        retried_rows = (
                            await session.execute(
                                text(
                                    "SELECT schema_version, summary FROM context_payloads "
                                    "WHERE run_id=:id"
                                ),
                                {"id": run_id},
                            )
                        ).all()
                        builds = (
                            (
                                await session.execute(
                                    text(
                                        "SELECT response FROM run_actions "
                                        "WHERE run_id=:id AND tool='context.build' "
                                        "ORDER BY started_at"
                                    ),
                                    {"id": run_id},
                                )
                            )
                            .scalars()
                            .all()
                        )
                    assert len(builds) == 2
                    assert len(retried_rows) == 1
                    assert retried_rows[0][0] == 2
                    assert retried_rows[0][1] != {"stale": True}
                    assert retried_rows[0][1] == builds[-1]
                    assert [block.source.path for block in payload.retrieved] == ["user.py"]
                    snapshot = stage.snapshot_for_llm
                    assert snapshot is not None
                    retrieved = tuple(snapshot_retrieved(block) for block in payload.retrieved)
                    presented = replace(
                        _metadata_context(head_sha=_HEAD),
                        policy_snapshot=snapshot,
                        retrieved=retrieved,
                        retrieved_section=True,
                    )
                    prompt = PromptBuilder().render_input(presented)
                    assert "<retrieved_excerpts>" in prompt
                    assert 'path="user.py"' in prompt
                    assert f'block_id="{retrieved[0].block_id}"' in prompt
                    assert re.fullmatch(r"RAG:a{12}:[0-9a-f]{16}", retrieved[0].block_id)
                    assert len(retrieved[0].block_id) <= 128
                    manifest = PromptBuilder().build_manifest(
                        presented, call_id=uuid4(), stage="review"
                    )
                    shown = manifest.get_block(retrieved[0].block_id)
                    assert shown is not None and shown.repository_id == ids[1]
                    assert shown.source_role is SourceRole.RETRIEVED_EXCERPT
                    assert shown.path == "user.py"
                    assert shown.shown_ranges == ((1, 1), (4, 5))
                    assert shown.index_commit_sha == _HEAD
                    assert {
                        block.block_id
                        for block in manifest.blocks
                        if block.source_role is SourceRole.RETRIEVED_EXCERPT
                    } == {retrieved[0].block_id}
                    verify_provenance_pre_send_guard(manifest, context=presented, snapshot=snapshot)
                    for bad_source, error_detail in (
                        (
                            replace(retrieved[0].source, revision="f" * 40),
                            "revision does not match",
                        ),
                        (
                            replace(retrieved[0].source, repository_id=ids[0]),
                            "repository does not match",
                        ),
                    ):
                        bad_context = replace(
                            presented,
                            retrieved=(replace(retrieved[0], source=bad_source),),
                        )
                        bad_manifest = PromptBuilder().build_manifest(
                            bad_context, call_id=uuid4(), stage="review"
                        )
                        with pytest.raises(PolicyError, match=error_detail) as error:
                            verify_provenance_pre_send_guard(
                                bad_manifest, context=bad_context, snapshot=snapshot
                            )
                        assert error.value.code is PolicyErrorCode.CONTEXT_VIOLATION
                    model_http = _ModelHttp()
                    async with httpx.AsyncClient(
                        transport=httpx.MockTransport(model_http.respond)
                    ) as llm_client:
                        gateway = build_gateway(llm_settings, llm_client, factory)
                        production_claim = replace(claimed, attempt=2)
                        async with resources.attempt(
                            claimed=production_claim,
                            expected_input=expected_input,
                            checkpoint=checkpoint,
                            trace=trace,
                        ) as production_bundle:
                            provider = attempt_model_provider(
                                gateway, production_bundle.source, production_claim
                            )
                            assert await process_review_run(
                                run_id,
                                provider,
                                factory,
                                production_bundle.vcs,
                                output=_AcceptedOutput(),
                                trace=trace,
                                context_stage=production_bundle.context_stage,
                                reflection_pass=provider.reflection_pass,
                                vcs_provider_selector=BoundVcsProviderSelector(
                                    expected_input.locator, production_bundle.vcs
                                ),
                            )
                    assert len(model_http.bodies) == 3
                    review_prompt = model_http.bodies[1]["messages"][-1]["content"]
                    verifier_prompt = model_http.bodies[2]["messages"][-1]["content"]
                    [sent_rag_id] = re.findall(
                        rf'<excerpt block_id="([^"]+)" level="RAG" path="user.py" '
                        rf'revision="{_HEAD}" side="RIGHT" representation="exact_lines">',
                        review_prompt,
                    )
                    assert re.fullmatch(r"RAG:a{12}:[0-9a-f]{16}", sent_rag_id)
                    assert len(sent_rag_id) <= 128
                    assert '<line n="5">    return target()</line>' in review_prompt
                    async with factory() as session:
                        manifest_rows = (
                            await session.execute(
                                text(
                                    "SELECT stage, blocks FROM context_manifests "
                                    "WHERE run_id=:id ORDER BY created_at"
                                ),
                                {"id": run_id},
                            )
                        ).all()
                    assert [row[0] for row in manifest_rows] == [
                        "conventions",
                        "review",
                        "verification",
                    ]
                    review_rag = [
                        block
                        for block in manifest_rows[1][1]
                        if block["source_role"] == "retrieved_excerpt"
                    ]
                    assert len(review_rag) == 1
                    assert review_rag[0]["block_id"] == sent_rag_id
                    assert review_rag[0]["path"] == "user.py"
                    assert review_rag[0]["repository_id"] == str(ids[1])
                    assert review_rag[0]["revision"] == _HEAD
                    assert review_rag[0]["side"] == "RIGHT"
                    assert review_rag[0]["shown_ranges"] == [[1, 1], [4, 5]]
                    assert review_rag[0]["index_commit_sha"] == _HEAD
                    assert review_rag[0]["selection_reason"] == "rag_reverse_edge"
                    assert review_rag[0]["edge_kind"] == "uses_imported"
                    verifier_rag = [
                        block
                        for block in manifest_rows[2][1]
                        if block["source_role"] == "retrieved_excerpt"
                    ]
                    assert verifier_rag == review_rag
                    assert re.findall(
                        r'<excerpt block_id="([^"]+)" level="RAG"', verifier_prompt
                    ) == [sent_rag_id]
                    shown_excerpt_ids = set(
                        re.findall(r'<excerpt block_id="([^"]+)"', verifier_prompt)
                    )
                    manifest_excerpt_ids = {
                        block["block_id"]
                        for block in manifest_rows[2][1]
                        if block["source_role"]
                        in {"source_excerpt", "ast_excerpt", "retrieved_excerpt"}
                    }
                    assert manifest_excerpt_ids == shown_excerpt_ids
                    _assert_verifier_manifest_shown(verifier_prompt, manifest_rows[2][1])
                    diff_block = next(
                        block for block in manifest_rows[2][1] if block["source_role"] == "diff"
                    )
                    with pytest.raises(AssertionError, match="unshown"):
                        _assert_verifier_manifest_shown(
                            verifier_prompt,
                            [*manifest_rows[2][1], {**diff_block, "block_id": "unshown"}],
                        )
            finally:
                await parser.aclose()
            refresh = RequestBitbucketIndexRefresh(
                partial(SqlAlchemyBitbucketIndexRefreshUnitOfWork, factory)
            )
            assert not await refresh.execute(
                installations[1], ids[1], remote_uuid, frozenset({"feature"}), 1, 2
            )
            assert await refresh.execute(
                installations[1], ids[1], remote_uuid, frozenset({"main"}), 1, 2
            )
            async with factory() as session:
                state = await session.get(CodeIndexState, ids[1])
                assert state is not None and state.wake_requested_at is not None
            for binding_generation, webhook_generation in ((0, 2), (1, 1)):
                async with factory.begin() as session:
                    await session.execute(
                        text(
                            "UPDATE code_index_states SET wake_requested_at=NULL "
                            "WHERE repository_id=:repository"
                        ),
                        {"repository": ids[1]},
                    )
                assert not await refresh.execute(
                    installations[1],
                    ids[1],
                    remote_uuid,
                    frozenset({"main"}),
                    binding_generation,
                    webhook_generation,
                )
                async with factory() as session:
                    assert (
                        await session.scalar(
                            text(
                                "SELECT wake_requested_at FROM code_index_states "
                                "WHERE repository_id=:repository"
                            ),
                            {"repository": ids[1]},
                        )
                        is None
                    )
                assert await refresh.execute(
                    installations[1], ids[1], remote_uuid, frozenset({"main"}), 1, 2
                )
            for table, id_column, disabled, restored, expected in (
                ("repositories", "id", "enabled=false", "enabled=true", True),
                (
                    "repositories",
                    "id",
                    "installation_available=false",
                    "installation_available=true",
                    False,
                ),
                (
                    "bitbucket_repository_webhook_secrets",
                    "repository_id",
                    "confirmed_at=NULL",
                    "confirmed_at=clock_timestamp()",
                    False,
                ),
            ):
                async with factory.begin() as session:
                    await session.execute(
                        text(
                            "UPDATE code_index_states SET wake_requested_at=NULL "
                            "WHERE repository_id=:repository"
                        ),
                        {"repository": ids[1]},
                    )
                    await session.execute(
                        text(f"UPDATE {table} SET {disabled} WHERE {id_column}=:repository"),
                        {"repository": ids[1]},
                    )
                assert (
                    await refresh.execute(
                        installations[1], ids[1], remote_uuid, frozenset({"main"}), 1, 2
                    )
                    is expected
                )
                async with factory() as session:
                    wake = await session.scalar(
                        text(
                            "SELECT wake_requested_at FROM code_index_states "
                            "WHERE repository_id=:repository"
                        ),
                        {"repository": ids[1]},
                    )
                    assert (wake is not None) is expected
                async with factory.begin() as session:
                    await session.execute(
                        text(f"UPDATE {table} SET {restored} WHERE {id_column}=:repository"),
                        {"repository": ids[1]},
                    )
                assert await refresh.execute(
                    installations[1], ids[1], remote_uuid, frozenset({"main"}), 1, 2
                )
            requests_after_revoke: list[str] = []

            async def revoke_during_head(request: httpx.Request) -> httpx.Response:
                requests_after_revoke.append(str(request.url))
                assert request.headers["Authorization"] == "Bearer tenant-0"
                async with factory.begin() as session:
                    await session.execute(
                        update(BitbucketConnection)
                        .where(BitbucketConnection.provider_installation_id == installations[0])
                        .values(
                            revoked_at=text("clock_timestamp()"),
                            token_ciphertext=None,
                            token_key_id=None,
                            intake_enabled=False,
                        )
                    )
                return httpx.Response(
                    200,
                    json={"uuid": "{" + str(remote_uuid) + "}", "mainbranch": {"name": "main"}},
                )

            async with httpx.AsyncClient(transport=httpx.MockTransport(revoke_during_head)) as http:
                interrupted = BitbucketRepositorySnapshotSource(
                    client=http,
                    target_for_repository=partial(reader.resolve_repository_target, ids[0]),
                    credentials=credentials,
                )
                with pytest.raises(VcsAccessDenied):
                    await interrupted.head()
                with pytest.raises(VcsAccessDenied):
                    await interrupted.head()
            assert len(requests_after_revoke) == 1
            async with factory.begin() as session:
                maintenance = SqlAlchemyCodeIndexMaintenance(session)
                assert await maintenance.to_rearm(10) == []
                assert [item.repository_id for item in await maintenance.to_disarm(10)] == [ids[0]]
                review = SqlAlchemyReviewIndexAccess(session)
                assert not (await review.open_for_review(ids[0])).enabled
                assert (await review.open_for_review(ids[1])).enabled
            with pytest.raises(ValueError):
                await reader.resolve_repository_target(ids[0])
            assert (await credentials.credential_for(targets[1])).token == "tenant-1"
        finally:
            await engine.dispose()

    asyncio.run(scenario())
