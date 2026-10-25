"""Activating v3 never repoints an existing v2 run or its stored policy snapshot."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.bootstrap.seed_prompts import load_prompt_assets, seed_prompt_versions
from app.modules.reviews.application.conventions import CachedConventions, GeneratedConventions
from app.modules.reviews.application.policy_documents import LoadedPolicyDocument, missing_document
from app.modules.reviews.application.policy_snapshot import (
    AGENTS_DOCUMENT_PATH,
    PINNED_PROMPT_CHECKSUMS,
    RULES_DOCUMENT_PATH,
    ExecutionProfile,
)
from app.modules.reviews.application.prepare_policy_snapshot import build_policy_snapshot
from app.modules.reviews.application.review_ignore import parse_review_ignore
from app.modules.reviews.infrastructure.models import PromptVersion
from app.modules.reviews.infrastructure.policy_snapshot_unit_of_work import (
    SqlAlchemyPolicySnapshotUnitOfWork,
)
from app.modules.reviews.infrastructure.review_prompt_repository import (
    SqlAlchemyReviewPromptRepository,
)
from app.modules.reviews.infrastructure.run_repository import (
    SqlAlchemyReviewOutputRepository,
)
from tests.portal_postgres import PROMPT, REPO_A, RUN_ATTEMPTED, Env, portal_schema

REPO_ROOT = Path(__file__).resolve().parents[1]
V2_SHA = "9177e358c23d1a7cd8987cbf667cf8b705c2afb27eaf8b2237480e4531431d75"


@pytest.fixture
def prompt_env() -> Iterator[Env]:
    url = os.environ.get("TEST_DATABASE_URL")
    if url is None:
        pytest.skip("set TEST_DATABASE_URL to test retained prompt pins")
    with portal_schema(url, None) as env:
        yield env


@pytest.mark.integration
def test_v2_run_and_persisted_snapshot_retain_their_prompt_after_v3_activation(
    prompt_env: Env,
) -> None:
    assets = load_prompt_assets(REPO_ROOT / "review/prompts")
    legacy_assets = tuple(
        asset for asset in assets if (asset.key, asset.version) != ("review.system", 3)
    )
    v2_content = (REPO_ROOT / "tests/fixtures/prompt_builder/sample.system.txt").read_text()

    async def exercise() -> None:
        engine = prompt_env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        repository = SqlAlchemyReviewPromptRepository(factory)

        def uow() -> SqlAlchemyPolicySnapshotUnitOfWork:
            return SqlAlchemyPolicySnapshotUnitOfWork(
                factory, execution_profile=ExecutionProfile.fast("legacy-review", "")
            )

        try:
            async with factory.begin() as session:
                # portal_schema starts with a fake v1 prompt. Establish an actual-v2
                # fixture before seeding real assets; never alter the shared helper.
                await session.execute(
                    text(
                        "UPDATE prompt_versions SET version=2, content=:content, "
                        "checksum=:checksum WHERE id=:id"
                    ),
                    {"id": PROMPT, "content": v2_content, "checksum": V2_SHA},
                )
                await session.execute(
                    text("UPDATE runs SET state='running' WHERE id=:id"), {"id": RUN_ATTEMPTED}
                )
                await session.execute(
                    text("UPDATE repositories SET severity_threshold='critical' WHERE id=:repo_id"),
                    {"repo_id": REPO_A},
                )
                assert await seed_prompt_versions(session, legacy_assets) == 5
            async with factory() as session:
                conventions_id = (
                    await session.scalars(
                        select(PromptVersion.id).where(
                            PromptVersion.key == "review.conventions", PromptVersion.version == 2
                        )
                    )
                ).one()
            conventions = GeneratedConventions(
                CachedConventions(REPO_A, None, conventions_id, (), (), {}), None, True
            )
            before = await repository.get_review_prompt_input(RUN_ATTEMPTED, conventions)
            assert before is not None and before.system == v2_content
            assert before.severity_threshold == "critical"
            assert before.policy_snapshot is None
            rules = missing_document(RULES_DOCUMENT_PATH)
            agents = missing_document(AGENTS_DOCUMENT_PATH)
            async with uow() as work:
                pins = await work.policy.get_pins(RUN_ATTEMPTED)
                assert pins is not None
                assert pins.prompt_bindings.review_system_id == PROMPT
                assert pins.prompt_bindings.review_system_version == "v2"
                assert pins.prompt_bindings.review_system_checksum == V2_SHA
                await work.policy.stage_documents(
                    RUN_ATTEMPTED,
                    (LoadedPolicyDocument(rules, None), LoadedPolicyDocument(agents, None)),
                )
                snapshot = await work.policy.create_snapshot(
                    build_policy_snapshot(
                        pins,
                        rules=rules,
                        agents=agents,
                        ignore=parse_review_ignore(None),
                        snapshot_id=uuid4(),
                    )
                )
                await work.commit()
            async with factory.begin() as session:
                assert await seed_prompt_versions(session, assets) == 1
            async with factory.begin() as session:
                assert await seed_prompt_versions(session, assets) == 0
            async with factory() as session:
                active = (
                    await session.scalars(
                        select(PromptVersion).where(
                            PromptVersion.key == "review.system", PromptVersion.is_active.is_(True)
                        )
                    )
                ).one()
                assert active.version == 3
                assert active.id != PROMPT
                assert (
                    active.checksum
                    == "eb851e7fc2329eb7bdff015ec0fda945c46111ec5c884934ed6f6d6803883452"
                )
            after = await repository.get_review_prompt_input(RUN_ATTEMPTED, conventions)
            assert after == before
            assert after is not None and after.system == v2_content
            assert after.severity_threshold == "critical"
            async with factory() as session:
                reviews_repo = SqlAlchemyReviewOutputRepository(session=session)
                post_input = await reviews_repo.get_post_processing_input(RUN_ATTEMPTED)
                assert post_input is not None
                assert post_input.severity_threshold == "critical"
            async with uow() as work:
                after_pins = await work.policy.get_pins(RUN_ATTEMPTED)
                assert after_pins is not None
                assert after_pins.prompt_bindings == pins.prompt_bindings
                assert await work.policy.get_snapshot(RUN_ATTEMPTED) == snapshot
            assert PINNED_PROMPT_CHECKSUMS["review.system:v2"] == V2_SHA
            assert "review.system:v3" not in PINNED_PROMPT_CHECKSUMS
        finally:
            await engine.dispose()

    asyncio.run(exercise())


@pytest.mark.integration
@pytest.mark.parametrize(
    ("pinned", "live", "expected"),
    [("warning", "critical", "warning"), (None, "critical", "critical"), (None, "all", "all")],
)
def test_prompt_threshold_uses_run_pin_or_historical_live_fallback(
    prompt_env: Env, pinned: str | None, live: str, expected: str
) -> None:
    async def scenario() -> None:
        engine = prompt_env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        repository = SqlAlchemyReviewPromptRepository(factory)
        conventions = GeneratedConventions(
            CachedConventions(REPO_A, None, PROMPT, (), (), {}), None, True
        )
        try:
            async with factory.begin() as session:
                await session.execute(
                    text(
                        "UPDATE runs SET severity_threshold=:threshold, "
                        "max_comments=:cap WHERE id=:id"
                    ),
                    {
                        "id": RUN_ATTEMPTED,
                        "threshold": pinned,
                        "cap": None if pinned is None else 3,
                    },
                )
                await session.execute(
                    text("UPDATE repositories SET severity_threshold=:threshold WHERE id=:id"),
                    {"id": REPO_A, "threshold": live},
                )
            result = await repository.get_review_prompt_input(RUN_ATTEMPTED, conventions)
            assert result is not None
            assert result.severity_threshold == expected
            assert await repository.get_review_prompt_input(uuid4(), conventions) is None
        finally:
            await engine.dispose()

    asyncio.run(scenario())
