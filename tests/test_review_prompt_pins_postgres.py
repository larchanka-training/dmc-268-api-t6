"""Activating v3 never repoints an existing v2 run or its stored policy snapshot."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from hashlib import sha256
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bootstrap.seed_prompts import PromptAsset, load_prompt_assets, seed_prompt_versions
from app.modules.reviews.application.conventions import (
    CachedConventions,
    GeneratedConventions,
    GenerateRepoConventions,
)
from app.modules.reviews.application.policy_documents import LoadedPolicyDocument, missing_document
from app.modules.reviews.application.policy_errors import PolicyError, PolicyErrorCode
from app.modules.reviews.application.policy_snapshot import (
    AGENTS_DOCUMENT_PATH,
    PINNED_PROMPT_CHECKSUMS,
    RULES_DOCUMENT_PATH,
    ExecutionProfile,
    make_complete_execution_profile,
)
from app.modules.reviews.application.prepare_policy_snapshot import (
    PreparePolicySnapshot,
    build_policy_snapshot,
)
from app.modules.reviews.application.process_run import ReviewRunProcessor
from app.modules.reviews.application.review_context import BuildReviewContext
from app.modules.reviews.application.review_context_stage import (
    EstimatedTokenCounter,
    ReviewContextStage,
)
from app.modules.reviews.application.review_ignore import parse_review_ignore
from app.modules.reviews.application.review_path import ReviewBinding
from app.modules.reviews.infrastructure.models import PromptVersion, Run
from app.modules.reviews.infrastructure.policy_snapshot_unit_of_work import (
    SqlAlchemyPolicySnapshotUnitOfWork,
)
from app.modules.reviews.infrastructure.review_prompt_repository import (
    SqlAlchemyReviewPromptRepository,
)
from app.modules.reviews.infrastructure.run_processing_unit_of_work import (
    SqlAlchemyRunProcessingUnitOfWork,
)
from app.modules.reviews.infrastructure.run_repository import (
    SqlAlchemyReviewOutputRepository,
    SqlAlchemyRunRepository,
)
from tests.portal_postgres import INSTALL_A, PROMPT, REPO_A, RUN_ATTEMPTED, Env, portal_schema
from tests.test_prepare_policy_snapshot import Source
from tests.test_review_context_stage import Parser, Vcs, _checkpoint
from tests.test_review_context_stage import Repository as SourceTree
from tests.test_run_processor_context_stage import Conventions, GitHub
from tests.test_run_prompt_pins_postgres import _packaged
from tests.test_run_publication_settings_postgres import _claim, _insert, _ready

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
            # The bound version comes from the pinned row: a v2 run stays legacy (F1-2).
            assert before.review_binding == ReviewBinding(PROMPT, "review.system", "v2", "github")
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


def _assets(*keys: str) -> tuple[PromptAsset, ...]:
    # review.system up to v3 only: F2's packaged v4 must not change what these runs bind.
    return tuple(
        asset
        for asset in _packaged(*keys)
        if not (asset.key == "review.system" and asset.version > 3)
    )


_V4_CONTENT = (
    "---\nkey: review.system\nversion: 4\n---\nA #130 test fixture, not the packaged v4.\n"
)


async def _bind_v4(factory: async_sessionmaker[AsyncSession], run_id: UUID) -> UUID:
    """Bind the run to a review.system v4 fixture row; nothing in F1 seeds or activates v4."""
    v4 = uuid4()
    async with factory.begin() as session:
        await session.execute(
            text(
                "INSERT INTO prompt_versions (id, key, version, content, checksum, is_active) "
                "VALUES (:id, 'review.system', 4, :content, :checksum, false)"
            ),
            {
                "id": v4,
                "content": _V4_CONTENT,
                "checksum": sha256(_V4_CONTENT.encode()).hexdigest(),
            },
        )
        await session.execute(
            text("UPDATE runs SET prompt_version_id=:prompt WHERE id=:id"),
            {"prompt": v4, "id": run_id},
        )
    return v4


def _stage(factory: async_sessionmaker[AsyncSession]) -> ReviewContextStage:
    vcs = Vcs(head_sha="f" * 40)
    return ReviewContextStage(
        runs=SqlAlchemyRunRepository(factory, allow_unscoped=True),
        checkpoint=_checkpoint,
        vcs=vcs,
        merge_base=vcs,
        prepare_policy=PreparePolicySnapshot(
            uow_factory=lambda: SqlAlchemyPolicySnapshotUnitOfWork(
                factory, execution_profile=make_complete_execution_profile()
            ),
            source_factory=lambda pins: Source(),
        ),
        context_builder=lambda run: BuildReviewContext(
            source=SourceTree(), parser=Parser(), counter=EstimatedTokenCounter()
        ),
    )


async def _prepare_with_stage(
    factory: async_sessionmaker[AsyncSession], run_id: UUID, events: list[str]
) -> None:
    """The worker's processor and stage over the database; the conventions model only logs."""
    await ReviewRunProcessor(
        SqlAlchemyRunRepository(factory, allow_unscoped=True),
        None,
        conventions=cast(GenerateRepoConventions, Conventions(events)),
        vcs_provider=GitHub(),
        uow_factory=lambda: SqlAlchemyRunProcessingUnitOfWork(factory),
        context_stage=_stage(factory),
    ).prepare(run_id)


@pytest.mark.integration
def test_run_reads_carry_the_pinned_review_prompt_and_the_origin_provider(
    prompt_env: Env,
) -> None:
    async def scenario() -> None:
        engine = prompt_env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        runs = SqlAlchemyRunRepository(factory, allow_unscoped=True)
        prompts = SqlAlchemyReviewPromptRepository(factory)
        conventions = GeneratedConventions(
            CachedConventions(REPO_A, None, PROMPT, (), (), {}), None, True
        )

        async def bindings() -> list[ReviewBinding | None]:
            conventions_input = await runs.get_run_conventions_input(RUN_ATTEMPTED)
            prompt_input = await prompts.get_review_prompt_input(RUN_ATTEMPTED, conventions)
            assert conventions_input is not None and prompt_input is not None
            return [conventions_input.review_binding, prompt_input.review_binding]

        try:
            async with factory.begin() as session:
                await seed_prompt_versions(session, _assets("review.conventions"))
            assert await bindings() == [ReviewBinding(PROMPT, "review.system", "v1", "github")] * 2
            async with factory.begin() as session:
                await session.execute(
                    text("UPDATE provider_installations SET provider='gitlab' WHERE id=:id"),
                    {"id": INSTALL_A},
                )
            assert await bindings() == [ReviewBinding(PROMPT, "review.system", "v1", "gitlab")] * 2
            async with factory.begin() as session:
                await session.execute(
                    text("UPDATE runs SET origin_provider_installation_id=NULL WHERE id=:id"),
                    {"id": RUN_ATTEMPTED},
                )
            # A run without an origin installation names no provider: a v4 run is refused.
            assert await bindings() == [ReviewBinding(PROMPT, "review.system", "v1", None)] * 2
            async with factory.begin() as session:
                await session.execute(
                    text("UPDATE prompt_versions SET key='review.fixture' WHERE id=:id"),
                    {"id": PROMPT},
                )
            # The key is read off the pinned row: only a review.system row picks the path.
            assert await bindings() == [ReviewBinding(PROMPT, "review.fixture", "v1", None)] * 2
        finally:
            await engine.dispose()

    asyncio.run(scenario())


@pytest.mark.integration
def test_stage_gives_a_v4_run_with_all_pins_its_stored_snapshot(prompt_env: Env) -> None:
    # The stage keys the path on the provider it builds the diff map with (GitHub until #220),
    # so the GitLab case is pinned on the run reads above, not here.
    async def scenario() -> None:
        engine = prompt_env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            await _ready(factory)
            async with factory.begin() as session:
                await seed_prompt_versions(
                    session, _assets("review.conventions", "review.verifier")
                )
                run_id = await _insert(session)
            v4 = await _bind_v4(factory, run_id)
            await _claim(factory, run_id, 1)
            stage = _stage(factory)
            await stage.begin(run_id)
            async with SqlAlchemyPolicySnapshotUnitOfWork(
                factory, execution_profile=make_complete_execution_profile()
            ) as work:
                stored = await work.policy.get_snapshot(run_id)
            assert stored is not None and stored.is_ready_for_llm
            assert stored.prompt_bindings.review_system_version == "v4"
            assert stored.prompt_bindings.pins_review_prompt(v4)
            assert stage.snapshot_for_llm == stored
        finally:
            await engine.dispose()

    asyncio.run(scenario())


@pytest.mark.integration
def test_v4_run_with_a_null_pin_fails_before_the_diff_and_the_conventions_call(
    prompt_env: Env,
) -> None:
    async def scenario() -> None:
        engine = prompt_env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        events: list[str] = []
        try:
            await _ready(factory)
            # D-Q2/Q4: no active verifier row, so the run pins no verifier prompt.
            async with factory.begin() as session:
                await seed_prompt_versions(session, _assets("review.conventions"))
                run_id = await _insert(session)
            await _bind_v4(factory, run_id)
            await _claim(factory, run_id, 1)
            with pytest.raises(PolicyError) as raised:
                await _prepare_with_stage(factory, run_id, events)
            assert raised.value.code is PolicyErrorCode.SNAPSHOT_INTEGRITY_ERROR
            assert "Review prompt v4 takes the policy snapshot path" in str(raised.value)
            assert "not ready for the LLM" in str(raised.value)
            assert events == []
        finally:
            await engine.dispose()

    asyncio.run(scenario())


@pytest.mark.integration
def test_review_only_snapshot_of_a_run_that_now_pins_all_three_fails_before_any_call(
    prompt_env: Env,
) -> None:
    """D-1 under TL-10 (a): C's terminal error stays, also for a v3-bound run."""

    async def scenario() -> None:
        engine = prompt_env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        events: list[str] = []
        try:
            await _ready(factory)
            async with factory.begin() as session:
                await seed_prompt_versions(session, _assets("review.system"))
                run_id = await _insert(session)
            await _claim(factory, run_id, 1)
            # The snapshot is stored while the run pins its review prompt only.
            await _stage(factory).begin(run_id)
            async with factory.begin() as session:
                await seed_prompt_versions(
                    session, _assets("review.conventions", "review.verifier")
                )
                await session.execute(
                    text(
                        "UPDATE runs SET "
                        "conventions_prompt_version_id=(SELECT id FROM prompt_versions "
                        "WHERE key='review.conventions' AND is_active), "
                        "verifier_prompt_version_id=(SELECT id FROM prompt_versions "
                        "WHERE key='review.verifier' AND is_active) WHERE id=:id"
                    ),
                    {"id": run_id},
                )
                review = await session.scalar(
                    select(PromptVersion.version)
                    .join(Run, Run.prompt_version_id == PromptVersion.id)
                    .where(Run.id == run_id)
                )
            assert review == 3
            with pytest.raises(PolicyError) as raised:
                await _prepare_with_stage(factory, run_id, events)
            assert raised.value.code is PolicyErrorCode.SNAPSHOT_INTEGRITY_ERROR
            assert "The stored prompt_bindings are not the prompt versions of the run" in str(
                raised.value
            )
            assert events == []
        finally:
            await engine.dispose()

    asyncio.run(scenario())
