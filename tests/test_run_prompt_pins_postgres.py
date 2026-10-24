"""Runs pin their conventions and verifier prompts at creation (#130)."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from hashlib import sha256
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bootstrap.seed_prompts import PromptAsset, load_prompt_assets, seed_prompt_versions
from app.modules.reviews.application.rerun_run import RerunOutcome
from app.modules.reviews.application.try_enqueue_webhook_run import (
    PendingRunMessage,
    RunInsertCandidate,
)
from app.modules.reviews.infrastructure.models import PromptVersion, Run
from app.modules.reviews.infrastructure.rerun_store import SqlAlchemyRerunUnitOfWork
from app.modules.reviews.infrastructure.run_lifecycle_store import SqlAlchemyRunLifecycleStore
from app.modules.reviews.infrastructure.webhook_runs import SqlAlchemyWebhookRunUnitOfWork
from tests.portal_postgres import NOW, PR_NEW, REPO_A, Env, portal_schema
from tests.test_run_publication_settings_postgres import _claim, _insert, _ready

REPO_ROOT = Path(__file__).resolve().parents[1]
CONVENTIONS_V2 = (
    "review.conventions",
    2,
    "225a8f7d56835cf1307ddb25feac602c3d10e03c34ee51afc90a1f99af101454",
)
VERIFIER_V2 = (
    "review.verifier",
    2,
    "1f676b4fea8477d77b83a106a6c62e78f67d19f71937d4a02093d11a9d276088",
)
VERIFIER_V3 = (
    "review.verifier",
    3,
    "cc60d26e35e7561dc225e6fa6675afc69c46b04210992b177476a46b745300a2",
)

type PromptRow = tuple[str, int, str]


@pytest.fixture
def env() -> Iterator[Env]:
    url = os.environ.get("TEST_DATABASE_URL")
    if url is None:
        pytest.skip("set TEST_DATABASE_URL to test run prompt pins")
    with portal_schema(url, None) as schema:
        yield schema


def _packaged(*keys: str) -> tuple[PromptAsset, ...]:
    # portal_schema seeds a fake review.system v1 whose checksum is not the packaged file's;
    # seeding that version again would be refused as an immutable prompt change.
    return tuple(
        asset
        for asset in load_prompt_assets(REPO_ROOT / "review" / "prompts")
        if (asset.key, asset.version) != ("review.system", 1) and (not keys or asset.key in keys)
    )


def _newer(key: str) -> PromptAsset:
    content = f"---\nkey: {key}\nversion: 3\n---\nPinned by a #130 test.\n"
    return PromptAsset(key, 3, content, sha256(content.encode("utf-8")).hexdigest())


async def _seed(factory: async_sessionmaker[AsyncSession], assets: tuple[PromptAsset, ...]) -> None:
    async with factory.begin() as session:
        await seed_prompt_versions(session, assets)


async def _prompt_id(factory: async_sessionmaker[AsyncSession], key: str, version: int) -> UUID:
    async with factory() as session:
        return (
            await session.scalars(
                select(PromptVersion.id).where(
                    PromptVersion.key == key, PromptVersion.version == version
                )
            )
        ).one()


async def _describe(session: AsyncSession, prompt_id: UUID | None) -> PromptRow | None:
    if prompt_id is None:
        return None
    row = await session.get(PromptVersion, prompt_id)
    assert row is not None
    return (row.key, row.version, row.checksum)


async def _row_pins(session: AsyncSession, run: Run) -> tuple[PromptRow | None, PromptRow | None]:
    return (
        await _describe(session, run.conventions_prompt_version_id),
        await _describe(session, run.verifier_prompt_version_id),
    )


async def _pins(
    factory: async_sessionmaker[AsyncSession], run_id: UUID
) -> tuple[PromptRow | None, PromptRow | None]:
    async with factory() as session:
        run = await session.get(Run, run_id)
        assert run is not None
        return await _row_pins(session, run)


@pytest.mark.integration
def test_webhook_creation_pins_active_conventions_and_verifier_and_rolls_back(
    env: Env,
) -> None:
    async def scenario() -> None:
        engine = env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            await _ready(factory)
            await _seed(factory, _packaged())
            async with SqlAlchemyWebhookRunUnitOfWork(factory) as work:
                candidate = await work.runs.lock_candidate(PR_NEW)
                assert isinstance(candidate, RunInsertCandidate)
                message = await work.runs.insert_webhook_run(candidate, NOW)
                assert isinstance(message, PendingRunMessage)
                run = await work.session.get(Run, message.run_id)
                assert run is not None
                assert await _row_pins(work.session, run) == (CONVENTIONS_V2, VERIFIER_V2)
                # No commit: leaving the unit of work rolls the run and its pins back.
            async with factory() as session:
                assert await session.get(Run, message.run_id) is None
        finally:
            await engine.dispose()

    asyncio.run(scenario())


@pytest.mark.integration
def test_rerun_pins_current_prompt_rows_while_original_keeps_its_own(env: Env) -> None:
    async def scenario() -> None:
        engine = env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            await _ready(factory)
            await _seed(factory, _packaged())
            async with factory.begin() as session:
                original = await _insert(session)
            async with factory.begin() as session:
                await session.execute(
                    text("UPDATE runs SET state='succeeded' WHERE id=:run"), {"run": original}
                )
            await _seed(factory, (_newer("review.verifier"),))
            async with SqlAlchemyRerunUnitOfWork(factory, allow_unscoped=True) as work:
                result = await work.runs.create_rerun(original, NOW)
                assert result.outcome is RerunOutcome.CREATED and result.message is not None
                await work.commit()
            assert await _pins(factory, result.message.run_id) == (CONVENTIONS_V2, VERIFIER_V3)
            assert await _pins(factory, original) == (CONVENTIONS_V2, VERIFIER_V2)
        finally:
            await engine.dispose()

    asyncio.run(scenario())


@pytest.mark.integration
def test_retry_keeps_prompt_pins_after_a_newer_verifier_is_activated(env: Env) -> None:
    async def scenario() -> None:
        engine = env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            await _ready(factory)
            await _seed(factory, _packaged())
            async with factory.begin() as session:
                run_id = await _insert(session)
            await _claim(factory, run_id, 1)
            await _seed(factory, (_newer("review.verifier"),))
            async with factory.begin() as session:
                assert await SqlAlchemyRunLifecycleStore(session).requeue(
                    run_id, worker_id="worker", available_at=NOW
                )
            await _claim(factory, run_id, 2)
            assert await _pins(factory, run_id) == (CONVENTIONS_V2, VERIFIER_V2)
        finally:
            await engine.dispose()

    asyncio.run(scenario())


@pytest.mark.integration
def test_missing_active_conventions_or_verifier_still_creates_the_run(env: Env) -> None:
    async def scenario() -> None:
        engine = env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            await _ready(factory)
            # portal_schema seeds review.system only: the run is created without either pin.
            async with factory() as session:
                run_id = await _insert(session)
                run = await session.get(Run, run_id)
                assert run is not None
                assert await _row_pins(session, run) == (None, None)
                await session.rollback()
            await _seed(factory, _packaged("review.conventions"))
            async with factory() as session:
                run_id = await _insert(session)
                run = await session.get(Run, run_id)
                assert run is not None
                assert await _row_pins(session, run) == (CONVENTIONS_V2, None)
                await session.rollback()
        finally:
            await engine.dispose()

    asyncio.run(scenario())


@pytest.mark.integration
def test_repository_prompt_override_applies_to_review_system_only(env: Env) -> None:
    async def scenario() -> None:
        engine = env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            await _ready(factory)
            await _seed(factory, _packaged())
            system_v2 = await _prompt_id(factory, "review.system", 2)
            async with factory.begin() as session:
                await session.execute(
                    text("UPDATE repositories SET prompt_version_id=:prompt WHERE id=:repo"),
                    {"prompt": system_v2, "repo": REPO_A},
                )
            async with factory() as session:
                run_id = await _insert(session)
                run = await session.get(Run, run_id)
                assert run is not None
                assert run.prompt_version_id == system_v2
                assert await _row_pins(session, run) == (CONVENTIONS_V2, VERIFIER_V2)
                await session.rollback()
        finally:
            await engine.dispose()

    asyncio.run(scenario())
