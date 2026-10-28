"""The composed context stage reads graph candidates and records their use (#144).

`review_context_stage_factory` against PostgreSQL: the RAG gate, the repeatable-read graph
read and the blob cache of the run, with the run's pinned review prompt patched to declare a
retrieved section (in production only review.system v5 has one).
"""

from __future__ import annotations

import asyncio
import base64
import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from functools import partial

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.modules.code_index.application.index_store import ImportRecord, IndexedFile
from app.modules.integrations.webhooks.infrastructure.github_installation_tree_provider import (
    GitHubAppInstallationAccessTokenProvider,
)
from app.modules.reviews.application import rag_index
from app.modules.reviews.application.ast_resolution import ImportResolution
from app.modules.reviews.application.get_run_diff import DiffSnapshot, StoreDiffSnapshot
from app.modules.reviews.application.handle_review_run import AttemptCheckpoint
from app.modules.reviews.application.run_trace import TransactionalRunTrace
from app.modules.reviews.infrastructure.github_vcs import HttpGitHubVcsProvider
from app.modules.reviews.infrastructure.run_lifecycle_store import (
    SqlAlchemyRunLifecycleUnitOfWork,
    SqlAlchemyRunTraceUnitOfWork,
)
from app.modules.reviews.infrastructure.run_processing_unit_of_work import (
    SqlAlchemyRunProcessingUnitOfWork,
)
from app.modules.reviews.infrastructure.run_repository import SqlAlchemyRunRepository
from app.worker import review_context_stage_factory
from tests.portal_postgres import (
    HEAD,
    INSTALL_A,
    REPO_A,
    REPO_B,
    RUN_ATTEMPTED,
    Env,
    portal_schema,
)
from tests.test_code_index_store_postgres import C1, _build, _enable, _imports, _Index, _uses
from tests.test_github_vcs_provider import _pull_payload
from tests.test_retired_installation_postgres import DiscardTokenCache
from tests.test_review_context import _GREET_PATCH, _MODELS, _SERVICE
from tests.test_run_origin_guard_postgres import prepare

_BASE = "b" * 40
_REVIEW = "1" * 40
# The Git blob ids of b"rules" and b"agents": the raw policy read checks them (#168).
_RULES = "8b30841cdeae6458fe8bfca3e53f697bd46f64dd"
_AGENTS = "93aa575b2804e25ec8ff4398e131a66fea60eb53"
_APP = "4" * 40
_MODELS_SHA = "5" * 40
_USER_SHA = "6" * 40
# `pkg/app.py` calls the `greet` the PR changes; the default-branch graph knows it.
_USER = "from .service import greet\n\n\ndef welcome(user_id):\n    return greet(user_id)\n"
# The review prompt the portal seed pins: version 1.
_PINNED = ("review.system", "v1")


@pytest.fixture
def rag_env() -> Iterator[Env]:
    url = os.environ.get("TEST_DATABASE_URL")
    if url is None:
        pytest.skip("set TEST_DATABASE_URL to test the composed RAG context")
    with portal_schema(url, None) as env:
        yield env


def _user_file() -> IndexedFile:
    return IndexedFile(
        "pkg/app.py",
        "a1" * 20,
        edges=(
            _imports("pkg/app.py", "pkg/service.py"),
            _uses("pkg/app.py", "pkg/service.py", "greet"),
        ),
        imports=(
            ImportRecord("pkg/app.py", ".service", ImportResolution.RESOLVED, "pkg/service.py"),
        ),
    )


@pytest.mark.integration
def test_the_composed_stage_reads_graph_candidates_and_records_their_use(
    rag_env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(rag_index, "RETRIEVED_PROMPT_VERSIONS", frozenset({_PINNED}))

    async def exercise() -> None:
        engine = rag_env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        calls: list[str] = []

        async def http(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            calls.append(request.method + " " + path)
            # Independent locks prove no transaction of the run crosses a provider call.
            async with factory.begin() as session:
                for table, row_id in (
                    ("provider_installations", INSTALL_A),
                    ("repositories", REPO_A),
                    ("runs", RUN_ATTEMPTED),
                ):
                    await session.execute(
                        text(f"SELECT id FROM {table} WHERE id=:id FOR UPDATE NOWAIT"),
                        {"id": row_id},
                    )
            if path == "/app/installations/17/access_tokens":
                return httpx.Response(
                    201, json={"token": "token", "expires_at": "2030-01-01T00:00:00Z"}
                )
            if "/git/trees/" in path:
                sha = path.rsplit("/", 1)[1]
                if sha == _BASE:
                    tree = [
                        {"path": ".review", "mode": "040000", "type": "tree", "sha": _REVIEW},
                        {"path": "AGENTS.md", "mode": "100644", "type": "blob", "sha": _AGENTS},
                    ]
                elif sha == _REVIEW:
                    tree = [{"path": "rules.md", "mode": "100644", "type": "blob", "sha": _RULES}]
                else:
                    assert sha == HEAD
                    tree = [
                        {"path": "pkg/service.py", "type": "blob", "sha": _APP},
                        {"path": "pkg/models.py", "type": "blob", "sha": _MODELS_SHA},
                        {"path": "pkg/app.py", "type": "blob", "sha": _USER_SHA},
                    ]
                return httpx.Response(200, json={"tree": tree, "truncated": False})
            if "/git/blobs/" in path:
                sha = path.rsplit("/", 1)[1]
                raw = {
                    _RULES: b"rules",
                    _AGENTS: b"agents",
                    _APP: _SERVICE.encode(),
                    _MODELS_SHA: _MODELS.encode(),
                    _USER_SHA: _USER.encode(),
                }[sha]
                if request.headers["Accept"] == "application/vnd.github.raw+json":
                    return httpx.Response(200, content=raw)
                return httpx.Response(
                    200,
                    json={
                        "sha": sha,
                        "encoding": "base64",
                        "content": base64.b64encode(raw).decode(),
                        "size": len(raw),
                    },
                )
            if path.endswith("/pulls/3"):
                payload = _pull_payload(changed_files=1, head_sha=HEAD)
                payload["number"] = 3
                base = payload["base"]
                assert isinstance(base, dict)
                base["sha"] = _BASE
                return httpx.Response(200, json=payload)
            if "/compare/" in path:
                return httpx.Response(200, json={"merge_base_commit": {"sha": "c" * 40}})
            raise AssertionError(f"unexpected request {request.url}")

        try:
            await prepare(factory, "active", "running")
            async with factory.begin() as session:
                await session.execute(
                    text("UPDATE provider_installations SET account_external_id=700 WHERE id=:id"),
                    {"id": INSTALL_A},
                )
                await session.execute(
                    text("UPDATE runs SET base_sha=:base WHERE id=:id"),
                    {"base": _BASE, "id": RUN_ATTEMPTED},
                )
                await session.execute(
                    text("UPDATE repositories SET rag_enabled=true WHERE id=:id"), {"id": REPO_A}
                )
            index = _Index(factory, REPO_A, REPO_B)
            await _enable(index, REPO_A)
            await _build(index, REPO_A, C1, _user_file())
            runs = SqlAlchemyRunRepository(factory, allow_unscoped=True)
            run = await runs.get_run_vcs_input(RUN_ATTEMPTED)
            assert run is not None
            async with SqlAlchemyRunProcessingUnitOfWork(factory) as work:
                await StoreDiffSnapshot(work.repository).execute(
                    run_id=RUN_ATTEMPTED,
                    code_change_id=run.code_change_id,
                    head_sha=HEAD,
                    files=[
                        DiffSnapshot(
                            "pkg/service.py", _GREET_PATCH, _APP, "modified", None, 1, 0, 1
                        )
                    ],
                )
                await work.commit()
            async with httpx.AsyncClient(
                base_url="https://api.github.test", transport=httpx.MockTransport(http)
            ) as client:
                tokens = GitHubAppInstallationAccessTokenProvider(
                    client=client,
                    app_id="42",
                    private_key="unused",
                    jwt_encoder=lambda claims, key: "jwt",
                    cache=DiscardTokenCache(),
                    now=lambda: datetime(2026, 10, 10, tzinfo=UTC).timestamp(),
                )
                checkpoint = AttemptCheckpoint(
                    partial(SqlAlchemyRunLifecycleUnitOfWork, factory),
                    RUN_ATTEMPTED,
                    datetime.now(UTC) + timedelta(minutes=5),
                    lambda: datetime.now(UTC),
                )
                build_stage = review_context_stage_factory(
                    client=client,
                    tokens=tokens,
                    vcs=HttpGitHubVcsProvider(client=client, token_provider=tokens),
                    runs=runs,
                    session_factory=factory,
                    redis=None,
                )
                trace = TransactionalRunTrace(partial(SqlAlchemyRunTraceUnitOfWork, factory))
                stage = build_stage(trace, checkpoint)
                await stage.begin(RUN_ATTEMPTED)
                payload = await stage.run(RUN_ATTEMPTED)

            assert payload is not None
            (block,) = payload.retrieved
            # Read at the PR head, from the PR tree; the index commit is metadata only.
            assert (block.source.path, block.source.revision, block.source.blob_sha) == (
                "pkg/app.py",
                HEAD,
                _USER_SHA,
            )
            assert block.index_commit_sha == C1
            # One provider read of the candidate, kept in the shared blob cache.
            assert calls.count(f"GET /repos/octo/a/git/blobs/{_USER_SHA}") == 1
            async with factory() as session:
                steps = [
                    tuple(row)
                    for row in await session.execute(
                        text(
                            "SELECT tool,request,response FROM run_actions WHERE run_id=:id "
                            "AND tool IN ('context.build','context.rag') ORDER BY index"
                        ),
                        {"id": RUN_ATTEMPTED},
                    )
                ]
                target = await session.scalar(
                    text("SELECT target_commit_sha FROM code_index_states WHERE repository_id=:id"),
                    {"id": REPO_A},
                )
                cached = await session.scalar(
                    text(
                        "SELECT content FROM cached_file_blobs "
                        "WHERE repository_id=:repository AND blob_sha=:blob"
                    ),
                    {"repository": REPO_A, "blob": _USER_SHA},
                )
            assert cached == _USER
            assert [step[0] for step in steps] == ["context.build", "context.rag"]
            build, rag = steps
            assert rag[1:] == (
                {
                    "index_status": "ready",
                    "indexed_commit_sha": C1,
                    "default_head_sha": target,
                    "stale": False,
                },
                {
                    "outcome": "used",
                    "candidates": {"found": 1, "confirmed": 1, "dropped": {}},
                    "tokens": block.tokens,
                },
            )
            assert [item for item in build[2]["blocks"] if item["level"] == "RAG"] == [
                {
                    "level": "RAG",
                    "role": "retrieved_excerpt",
                    "path": "pkg/app.py",
                    "side": "RIGHT",
                    "revision": HEAD,
                    "ranges": [[1, 1], [4, 5]],
                    "representation": "exact_lines",
                    "tokens": block.tokens,
                    "reason": "rag_reverse_edge",
                }
            ]
        finally:
            await engine.dispose()

    asyncio.run(exercise())
