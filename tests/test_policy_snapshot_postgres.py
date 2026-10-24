"""Migration 0029: staged policy documents and the snapshot of a run are write-once."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from dataclasses import replace
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.modules.reviews.application.policy_documents import (
    LoadedPolicyDocument,
    missing_document,
    normalize_document,
)
from app.modules.reviews.application.policy_snapshot import (
    AGENTS_DOCUMENT_PATH,
    RULES_DOCUMENT_PATH,
    DocumentPresence,
    ExecutionProfile,
)
from app.modules.reviews.application.prepare_policy_snapshot import build_policy_snapshot
from app.modules.reviews.application.review_ignore import parse_review_ignore
from app.modules.reviews.infrastructure.policy_snapshot_unit_of_work import (
    SqlAlchemyPolicySnapshotUnitOfWork,
)
from tests.portal_postgres import (
    PROMPT,
    REPO_A,
    RULE_A,
    RUN_ATTEMPTED,
    RUN_DONE,
    Env,
    portal_schema,
)

_RAW = "# Правила\r\n\r\n```review-ignore\r\ngenerated/**\r\n```\r\n".encode()


@pytest.fixture
def env() -> Iterator[Env]:
    url = os.environ.get("TEST_DATABASE_URL")
    if url is None:
        pytest.skip("set TEST_DATABASE_URL to test policy snapshot persistence")
    with portal_schema(url, None) as schema:
        yield schema


def _documents() -> tuple[LoadedPolicyDocument, LoadedPolicyDocument]:
    rules = normalize_document(RULES_DOCUMENT_PATH, _RAW, blob_oid="3" * 40)
    return (
        LoadedPolicyDocument(rules, _RAW),
        LoadedPolicyDocument(missing_document(AGENTS_DOCUMENT_PATH), None),
    )


@pytest.mark.integration
def test_pins_documents_and_snapshot_round_trip_and_stay_write_once(env: Env) -> None:
    async def scenario() -> None:
        engine = env.engine()
        factory = async_sessionmaker(engine)

        def uow() -> SqlAlchemyPolicySnapshotUnitOfWork:
            return SqlAlchemyPolicySnapshotUnitOfWork(
                factory,
                execution_profile=ExecutionProfile.fast("model-a", "", "model-c"),
            )

        try:
            # Policy inputs are written during the running attempt.
            async with factory.begin() as session:
                await session.execute(
                    text("UPDATE runs SET state='running' WHERE id=:id"), {"id": RUN_DONE}
                )
            async with uow() as work:
                pins = await work.policy.get_pins(RUN_DONE)
                assert await work.policy.get_snapshot(RUN_DONE) is None
                assert await work.policy.get_staged_documents(RUN_DONE) == ()
                assert await work.policy.get_pins(UUID(int=7)) is None
            assert pins is not None
            assert (pins.repository_id, pins.service_defaults_rule_version_id) == (REPO_A, RULE_A)
            assert pins.prompt_bindings.pins_review_prompt(PROMPT)
            assert pins.prompt_bindings.review_system_checksum == "c" * 64
            assert pins.prompt_bindings.review_system_version == "v1"
            # This fixture run predates the conventions and verifier pins.
            assert pins.prompt_bindings.is_complete is False
            assert pins.execution_profile.review_model == "model-a"
            assert len(pins.defaults_checksum) == 64

            rules, agents = _documents()
            async with uow() as work:
                staged = await work.policy.stage_documents(RUN_DONE, (rules, agents))
                await work.commit()
            assert {item.document.path: item.raw for item in staged} == {
                AGENTS_DOCUMENT_PATH: None,
                RULES_DOCUMENT_PATH: _RAW,
            }

            # A later load of other content never replaces the staged document.
            other = normalize_document(RULES_DOCUMENT_PATH, b"other", blob_oid="4" * 40)
            async with uow() as work:
                staged = await work.policy.stage_documents(
                    RUN_DONE, (LoadedPolicyDocument(other, b"other"),)
                )
                await work.commit()
            by_path = {item.document.path: item.document for item in staged}
            assert by_path[RULES_DOCUMENT_PATH] == rules.document
            assert by_path[AGENTS_DOCUMENT_PATH].presence is DocumentPresence.MISSING

            snapshot = build_policy_snapshot(
                pins,
                rules=rules.document,
                agents=agents.document,
                ignore=parse_review_ignore(rules.document.text),
                snapshot_id=UUID(int=11),
            )
            async with uow() as work:
                stored = await work.policy.create_snapshot(snapshot)
                await work.commit()
            # The stored snapshot is the built one plus the time the database fixed it.
            assert stored.created_at is not None
            assert replace(stored, created_at=None) == snapshot
            assert stored.execution_profile == ExecutionProfile.fast("model-a", "", "model-c")
            assert stored.prompt_bindings.review_system_id == PROMPT
            snapshot = stored

            rival = build_policy_snapshot(
                pins,
                rules=other,
                agents=agents.document,
                ignore=parse_review_ignore(None),
                snapshot_id=UUID(int=12),
            )
            async with uow() as work:
                assert await work.policy.create_snapshot(rival) == snapshot
                await work.commit()
            async with uow() as work:
                assert await work.policy.get_snapshot(RUN_DONE) == snapshot
                assert await work.policy.get_snapshot(RUN_ATTEMPTED) is None
        finally:
            await engine.dispose()

    asyncio.run(scenario())


@pytest.mark.integration
def test_document_content_must_match_its_presence_in_the_database(env: Env) -> None:
    async def scenario() -> None:
        engine = env.engine()
        try:
            with pytest.raises(IntegrityError, match="content_matches_presence"):
                async with async_sessionmaker(engine).begin() as session:
                    await session.execute(
                        text(
                            "INSERT INTO run_policy_documents (id, run_id, path, presence, text) "
                            "VALUES (gen_random_uuid(), :run, 'AGENTS.md', 'missing', 'x')"
                        ),
                        {"run": RUN_DONE},
                    )
        finally:
            await engine.dispose()

    asyncio.run(scenario())
