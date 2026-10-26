"""Runs pin their conventions and verifier prompts at creation; policy pins read them (#130)."""

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
from app.modules.reviews.application.conventions import (
    CachedConventions,
    GeneratedConventions,
    conventions_cache_key,
)
from app.modules.reviews.application.policy_snapshot import (
    ExecutionProfile,
    PromptBindings,
)
from app.modules.reviews.application.prepare_policy_snapshot import (
    PreparePolicySnapshot,
    RunPolicyPins,
    build_policy_snapshot,
)
from app.modules.reviews.application.rerun_run import RerunOutcome, create_rerun
from app.modules.reviews.application.review_context import BuildReviewContext
from app.modules.reviews.application.review_context_stage import (
    EstimatedTokenCounter,
    ReviewContextStage,
)
from app.modules.reviews.application.review_ignore import parse_review_ignore
from app.modules.reviews.application.try_enqueue_webhook_run import (
    PendingRunMessage,
    RunInsertCandidate,
)
from app.modules.reviews.infrastructure.models import PromptVersion, Run
from app.modules.reviews.infrastructure.policy_snapshot_unit_of_work import (
    SqlAlchemyPolicySnapshotUnitOfWork,
)
from app.modules.reviews.infrastructure.rerun_store import (
    SqlAlchemyRerunStore,
    SqlAlchemyRerunUnitOfWork,
)
from app.modules.reviews.infrastructure.review_prompt_repository import (
    SqlAlchemyReviewPromptRepository,
)
from app.modules.reviews.infrastructure.run_lifecycle_store import SqlAlchemyRunLifecycleStore
from app.modules.reviews.infrastructure.run_repository import SqlAlchemyRunRepository
from app.modules.reviews.infrastructure.webhook_runs import SqlAlchemyWebhookRunUnitOfWork
from tests.policy_snapshot_fakes import make_complete_execution_profile
from tests.portal_postgres import (
    NOW,
    PR_NEW,
    PROMPT,
    REPO_A,
    RULE_A,
    RUN_ATTEMPTED,
    Env,
    portal_schema,
)
from tests.test_prepare_policy_snapshot import Source
from tests.test_review_context_stage import Parser, Vcs, _checkpoint
from tests.test_review_context_stage import Repository as SourceTree
from tests.test_run_publication_settings_postgres import _claim, _insert, _ready

REPO_ROOT = Path(__file__).resolve().parents[1]

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


def _row(asset: PromptAsset) -> PromptRow:
    return (asset.key, asset.version, asset.checksum)


def _newest(key: str) -> PromptRow:
    # From the packaged files, not the database: seeding them activates each key's newest one.
    return _row(max(_packaged(key), key=lambda asset: asset.version))


def _newer(key: str) -> PromptAsset:
    # Above every packaged version, so a newly packaged prompt neither wins nor collides.
    version = _newest(key)[1] + 1
    content = f"---\nkey: {key}\nversion: {version}\n---\nPinned by a #130 test.\n"
    return PromptAsset(key, version, content, sha256(content.encode("utf-8")).hexdigest())


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
                assert await _row_pins(work.session, run) == (
                    _newest("review.conventions"),
                    _newest("review.verifier"),
                )
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
                result = await create_rerun(work.runs, original, NOW)
                assert result.outcome is RerunOutcome.CREATED and result.message is not None
                await work.commit()
            assert await _pins(factory, result.message.run_id) == (
                _newest("review.conventions"),
                _row(_newer("review.verifier")),
            )
            assert await _pins(factory, original) == (
                _newest("review.conventions"),
                _newest("review.verifier"),
            )
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
            assert await _pins(factory, run_id) == (
                _newest("review.conventions"),
                _newest("review.verifier"),
            )
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
                assert await _row_pins(session, run) == (_newest("review.conventions"), None)
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
                assert await _row_pins(session, run) == (
                    _newest("review.conventions"),
                    _newest("review.verifier"),
                )
                await session.rollback()
        finally:
            await engine.dispose()

    asyncio.run(scenario())


@pytest.mark.integration
def test_get_pins_returns_complete_bindings_from_pinned_rows(env: Env) -> None:
    async def scenario() -> None:
        engine = env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            await _ready(factory)
            await _seed(factory, _packaged())
            async with factory.begin() as session:
                run_id = await _insert(session)
                await session.execute(
                    text("UPDATE runs SET state='succeeded' WHERE id=:run"), {"run": run_id}
                )
            # A pair above the "v2" defaults proves the versions come from the rows.
            await _seed(factory, (_newer("review.conventions"), _newer("review.verifier")))
            async with factory.begin() as session:
                rerun = await create_rerun(
                    SqlAlchemyRerunStore(session, allow_unscoped=True), run_id, NOW
                )
            assert rerun.message is not None
            for pinned, conventions, verifier in (
                (run_id, _newest("review.conventions"), _newest("review.verifier")),
                (
                    rerun.message.run_id,
                    _row(_newer("review.conventions")),
                    _row(_newer("review.verifier")),
                ),
            ):
                async with factory() as session:
                    run = await session.get(Run, pinned)
                    assert run is not None
                    system = await _describe(session, run.prompt_version_id)
                assert system is not None
                async with SqlAlchemyPolicySnapshotUnitOfWork(factory) as work:
                    pins = await work.policy.get_pins(pinned)
                assert pins is not None
                assert pins.prompt_bindings == PromptBindings(
                    review_system_checksum=system[2],
                    conventions_checksum=conventions[2],
                    verifier_checksum=verifier[2],
                    review_system_version=f"v{system[1]}",
                    conventions_version=f"v{conventions[1]}",
                    verifier_version=f"v{verifier[1]}",
                    review_system_id=run.prompt_version_id,
                    conventions_id=run.conventions_prompt_version_id,
                    verifier_id=run.verifier_prompt_version_id,
                )
                assert pins.prompt_bindings.is_complete is True
        finally:
            await engine.dispose()

    asyncio.run(scenario())


@pytest.mark.integration
def test_get_pins_binds_only_the_prompts_a_partially_pinned_run_pinned(env: Env) -> None:
    async def scenario() -> None:
        engine = env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            await _ready(factory)
            # No active verifier row: the run pins conventions only.
            await _seed(factory, _packaged("review.conventions"))
            async with factory.begin() as session:
                run_id = await _insert(session)
            async with factory() as session:
                run = await session.get(Run, run_id)
                assert run is not None
                system = await _describe(session, run.prompt_version_id)
            assert system is not None
            conventions = _newest("review.conventions")
            async with SqlAlchemyPolicySnapshotUnitOfWork(factory) as work:
                pins = await work.policy.get_pins(run_id)
            assert pins is not None
            assert pins.prompt_bindings == PromptBindings(
                review_system_checksum=system[2],
                conventions_checksum=conventions[2],
                verifier_checksum="",
                review_system_version=f"v{system[1]}",
                conventions_version=f"v{conventions[1]}",
                review_system_id=run.prompt_version_id,
                conventions_id=run.conventions_prompt_version_id,
            )
            assert pins.prompt_bindings.verifier_id is None
            assert pins.prompt_bindings.is_complete is False
        finally:
            await engine.dispose()

    asyncio.run(scenario())


@pytest.mark.integration
def test_historical_run_keeps_todays_pins_and_digest(env: Env) -> None:
    profile = ExecutionProfile.fast("model-a", "", "model-c")
    # What get_pins returned for this run before #130: the review prompt only, the
    # conventions and verifier versions at their defaults and the injected profile.
    legacy = RunPolicyPins(
        run_id=RUN_ATTEMPTED,
        repository_id=REPO_A,
        policy_base_sha="b" * 40,
        service_defaults_rule_version_id=RULE_A,
        defaults_checksum="1" * 64,
        prompt_bindings=PromptBindings(
            review_system_checksum="c" * 64,
            conventions_checksum="",
            verifier_checksum="",
            review_system_version="v1",
            review_system_id=PROMPT,
        ),
        execution_profile=profile,
    )

    async def scenario() -> None:
        engine = env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)

        def uow() -> SqlAlchemyPolicySnapshotUnitOfWork:
            return SqlAlchemyPolicySnapshotUnitOfWork(factory, execution_profile=profile)

        try:
            # Active conventions and verifier rows exist; the run pinned neither of them.
            await _seed(factory, _packaged())
            async with uow() as work:
                pins = await work.policy.get_pins(RUN_ATTEMPTED)
            assert pins == legacy
            assert pins.prompt_bindings.is_complete is False
            await _claim(factory, RUN_ATTEMPTED, 2)
            stored = await PreparePolicySnapshot(
                uow_factory=uow, source_factory=lambda pins: Source()
            ).execute(RUN_ATTEMPTED)
            expected = build_policy_snapshot(
                legacy,
                rules=stored.rules,
                agents=stored.agents,
                ignore=parse_review_ignore(stored.rules.text, policy_base_sha="b" * 40),
                snapshot_id=stored.snapshot_id,
            )
            assert stored.policy_digest == expected.policy_digest
        finally:
            await engine.dispose()

    asyncio.run(scenario())


@pytest.mark.integration
@pytest.mark.parametrize(
    ("v4_packaged", "version", "checksum"),
    [
        pytest.param(
            False,
            "v3",
            "eb851e7fc2329eb7bdff015ec0fda945c46111ec5c884934ed6f6d6803883452",
            id="seed-up-to-v3",
        ),
        pytest.param(
            True,
            "v4",
            "5738227c7352228d29975e0512b5b5d3354eff31c9d161ca72086a776e9c498f",
            id="full-seed",
        ),
    ],
)
def test_complete_pins_make_snapshot_ready_and_the_pinned_prompt_picks_the_path(
    env: Env, v4_packaged: bool, version: str, checksum: str
) -> None:
    # Up to v3 a new run stays legacy; the full seed (F2) activates v4 and the snapshot path.
    assets = tuple(
        asset
        for asset in _packaged()
        if v4_packaged or not (asset.key == "review.system" and asset.version > 3)
    )
    # The review prompt row a new run pins: the newest seeded one, as reviewed.
    pinned_row = ("review.system", int(version.removeprefix("v")), checksum)

    async def scenario() -> None:
        engine = env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)

        def uow() -> SqlAlchemyPolicySnapshotUnitOfWork:
            # The worker injects its complete profile the same way (worker.py:534).
            return SqlAlchemyPolicySnapshotUnitOfWork(
                factory, execution_profile=make_complete_execution_profile()
            )

        try:
            await _ready(factory)
            await _seed(factory, assets)
            async with factory.begin() as session:
                run_id = await _insert(session)
            await _claim(factory, run_id, 1)
            async with factory() as session:
                run = await session.get(Run, run_id)
                assert run is not None
                assert await _describe(session, run.prompt_version_id) == pinned_row
            prepare = PreparePolicySnapshot(uow_factory=uow, source_factory=lambda pins: Source())
            vcs = Vcs()
            stage = ReviewContextStage(
                runs=SqlAlchemyRunRepository(factory, allow_unscoped=True),
                checkpoint=_checkpoint,
                vcs=vcs,
                merge_base=vcs,
                prepare_policy=prepare,
                context_builder=lambda run: BuildReviewContext(
                    source=SourceTree(), parser=Parser(), counter=EstimatedTokenCounter()
                ),
            )
            await stage.begin(run_id)
            snapshot = await prepare.execute(run_id)
            # Lock 2 is lifted: the stored snapshot of a new run is ready for the LLM path.
            assert snapshot.prompt_bindings.is_complete is True
            assert snapshot.is_ready_for_llm is True
            assert snapshot.prompt_bindings.review_system_version == version
            assert snapshot.prompt_bindings.review_system_checksum == checksum
            if v4_packaged:
                # The snapshot path: the stage serves the snapshot, and the conventions cache
                # key is the repo-conventions-v2 digest (R5).
                assert stage.snapshot_for_llm == snapshot
                assert stage.policy_key == conventions_cache_key(snapshot, snapshot.ignore_digest)
            else:
                # Lock 1 holds: no snapshot for the LLM, and the conventions cache identity
                # still derives from the ignore policy alone.
                assert stage.snapshot_for_llm is None
                assert stage.policy_key == snapshot.ignore_digest
            conventions_id = await _prompt_id(factory, "review.conventions", 2)
            prompt_input = await SqlAlchemyReviewPromptRepository(factory).get_review_prompt_input(
                run_id,
                GeneratedConventions(
                    CachedConventions(REPO_A, None, conventions_id, (), (), {}), None, True
                ),
            )
            assert prompt_input is not None
            assert prompt_input.review_binding is not None
            assert prompt_input.review_binding.review_system_version == version
            # The repository never attaches a snapshot; the stage serves it (above).
            assert prompt_input.policy_snapshot is None
        finally:
            await engine.dispose()

    asyncio.run(scenario())
