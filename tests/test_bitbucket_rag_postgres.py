"""Bitbucket index eligibility is portal scoped and revoked per binding (#333)."""

from __future__ import annotations

import asyncio
import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import Connection, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from alembic import command
from app.bootstrap.bitbucket_reviews import BitbucketReviewWorkerResources
from app.bootstrap.code_index_worker import CodeIndexSettings, index_versions
from app.bootstrap.seed_prompts import load_prompt_assets, seed_prompt_versions
from app.modules.code_index.application.build_index import BuildRepositoryIndex, BuildStatus
from app.modules.code_index.infrastructure.bitbucket_index_refresh import (
    SqlAlchemyBitbucketIndexRefresh,
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
from app.modules.reviews.application.get_run_diff import DiffSnapshot, StoreDiffSnapshot
from app.modules.reviews.application.graph_retriever import GraphTarget
from app.modules.reviews.application.handle_review_run import ClaimedAttempt
from app.modules.reviews.application.prompt_builder import PromptBuilder
from app.modules.reviews.application.try_enqueue_webhook_run import (
    PendingRunMessage,
    RunInsertCandidate,
)
from app.modules.reviews.infrastructure.run_processing_unit_of_work import (
    SqlAlchemyRunProcessingUnitOfWork,
)
from app.modules.reviews.infrastructure.run_repository import SqlAlchemyRunRepository
from app.modules.reviews.infrastructure.subprocess_source_parser import SubprocessSourceParser
from app.modules.reviews.infrastructure.webhook_runs import SqlAlchemyWebhookRunStore
from tests.policy_snapshot_fakes import make_complete_execution_profile
from tests.test_bitbucket_connections_postgres import _config
from tests.test_prompt_builder import _metadata_context

pytest_plugins = ("tests.test_bitbucket_connections_postgres",)

_HEAD = "a" * 40
_LIB = b"def target():\n    return 1\n"
_USER = b"from lib import target\n\n\ndef use():\n    return target()\n"


class _Wakeups:
    async def wake(self, repository_id: UUID, generation: int) -> None:
        return None


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
                v4_path = Path(__file__).resolve().parents[1] / "review/prompts/review.system.v4.md"
                assert active_version == (4 if v4_path.exists() else 3)
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

            def respond(request: httpx.Request) -> httpx.Response:
                assert request.url.host == "api.bitbucket.org"
                assert request.headers["Authorization"] == "Bearer tenant-1"
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
                                    "new": {"path": "lib.py", "size": len(_LIB)},
                                }
                            ],
                        },
                    )
                if path.endswith(f"/merge-base/{_HEAD}..{'b' * 40}"):
                    return httpx.Response(200, json={"hash": "b" * 40})
                if path.endswith("/pullrequests/7/commits"):
                    return httpx.Response(200, json={"values": []})
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
                                for path, content in (("lib.py", _LIB), ("user.py", _USER))
                            ]
                        },
                    )
                if path.endswith("/lib.py"):
                    return httpx.Response(
                        200,
                        content=b"def target():\n    return 0\n" if revision != _HEAD else _LIB,
                    )
                if path.endswith("/user.py"):
                    return httpx.Response(200, content=_USER)
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

                    resources = BitbucketReviewWorkerResources(
                        session_factory=factory,
                        cipher=cipher,
                        execution_profile=make_complete_execution_profile(),
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
                    async with resources.attempt(
                        claimed=claimed,
                        expected_input=expected_input,
                        checkpoint=checkpoint,
                    ) as bundle:
                        stage = bundle.context_stage
                        await stage.begin(run_id)
                        payload = await stage.run(run_id)
                    assert payload is not None
                    assert [block.source.path for block in payload.retrieved] == ["user.py"]
                    snapshot = stage.snapshot_for_llm
                    assert snapshot is not None
                    presented = replace(
                        _metadata_context(),
                        policy_snapshot=snapshot,
                        retrieved=payload.retrieved,
                    )
                    prompt = PromptBuilder().render_input(presented)
                    assert "<retrieved_excerpts>" in prompt
                    assert 'path="user.py"' in prompt
                    manifest = PromptBuilder().build_manifest(
                        presented, call_id=uuid4(), stage="review"
                    )
                    shown = manifest.get_block(payload.retrieved[0].block_id)
                    assert shown is not None and shown.repository_id == ids[1]
            finally:
                await parser.aclose()
            refresh = SqlAlchemyBitbucketIndexRefresh(factory)
            assert not await refresh.execute(
                installations[1], ids[1], remote_uuid, frozenset({"feature"}), 1, 2
            )
            assert await refresh.execute(
                installations[1], ids[1], remote_uuid, frozenset({"main"}), 1, 2
            )
            async with factory() as session:
                state = await session.get(CodeIndexState, ids[1])
                assert state is not None and state.wake_requested_at is not None
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
