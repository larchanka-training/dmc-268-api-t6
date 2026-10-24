"""Unit contracts for versioned prompt assets used by the deploy seed command."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from dataclasses import replace
from hashlib import sha256
from importlib.resources import files
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.schema import CreateSchema, DropSchema

from alembic import command
from app.bootstrap.seed_prompts import PromptAsset, load_prompt_assets, seed_prompt_versions
from app.modules.reviews.application.policy_snapshot import PINNED_PROMPT_CHECKSUMS
from app.modules.reviews.infrastructure.models import PromptVersion

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def migrated_prompt_database() -> Iterator[tuple[str, str]]:
    """Provide a disposable, migrated PostgreSQL schema when explicitly configured."""
    database_url = os.environ.get("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("set TEST_DATABASE_URL to run PostgreSQL integration tests")
    schema = f"test_prompt_seed_{uuid4().hex}"
    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            connection.execute(CreateSchema(schema))
            connection.execute(text(f'SET search_path TO "{schema}"'))
            connection.commit()
            config = Config("alembic.ini")
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
            yield database_url, schema
            connection.rollback()
            connection.execute(text("SET search_path TO public"))
            connection.execute(DropSchema(schema, cascade=True))
            connection.commit()
    finally:
        engine.dispose()


def test_load_prompt_assets_preserves_full_file_and_checksum() -> None:
    prompts_dir = REPO_ROOT / "review" / "prompts"

    assets = load_prompt_assets(prompts_dir)

    assert [(asset.key, asset.version) for asset in assets] == [
        ("review.conventions", 1),
        ("review.conventions", 2),
        ("review.system", 1),
        ("review.system", 2),
        ("review.system", 3),
        ("review.verifier", 1),
        ("review.verifier", 2),
    ]
    for asset in assets:
        expected_content = (prompts_dir / f"{asset.key}.v{asset.version}.md").read_text(
            encoding="utf-8"
        )
        assert asset.content == expected_content
        assert asset.checksum == sha256(expected_content.encode("utf-8")).hexdigest()


def test_pinned_checksums_match_files_and_golden_fixtures() -> None:
    prompts_dir = REPO_ROOT / "review" / "prompts"

    for key_ver, pinned_hash in PINNED_PROMPT_CHECKSUMS.items():
        key, ver = key_ver.split(":")
        file_path = prompts_dir / f"{key}.{ver}.md"
        assert file_path.exists(), f"Prompt file {file_path} must exist"
        actual_hash = sha256(file_path.read_bytes()).hexdigest()
        assert actual_hash == pinned_hash, (
            f"Checksum for {key_ver} must match {pinned_hash}, got {actual_hash}"
        )

    # Verify golden fixture matches system prompt v2
    system_v2_raw = (prompts_dir / "review.system.v2.md").read_text(encoding="utf-8")
    sample_fixture = (
        REPO_ROOT / "tests" / "fixtures" / "prompt_builder" / "sample.system.txt"
    ).read_text(encoding="utf-8")
    assert sample_fixture == system_v2_raw


def test_seeded_verifier_checksums_match_the_builder_reading() -> None:
    # A run pins the seeded checksum; VerifierPromptBuilder checks it against a text-mode
    # read of the packaged resource. A newline translation would surface only as
    # SNAPSHOT_INTEGRITY_ERROR once the verifier runs live.
    verifiers = [
        asset
        for asset in load_prompt_assets(REPO_ROOT / "review" / "prompts")
        if asset.key == "review.verifier"
    ]

    assert [(asset.version, asset.checksum) for asset in verifiers] == [
        (1, "e00128b211ca64c9a2dd2bb902ade9abf4a29f30c7ec9066501dc7694ddc5940"),
        (2, "1f676b4fea8477d77b83a106a6c62e78f67d19f71937d4a02093d11a9d276088"),
    ]
    for asset in verifiers:
        resource = files("review.prompts").joinpath(f"review.verifier.v{asset.version}.md")
        builder_reading = resource.read_text(encoding="utf-8")
        assert sha256(builder_reading.encode("utf-8")).hexdigest() == asset.checksum


def test_load_prompt_assets_preserves_crlf_bytes_and_checksum(tmp_path: Path) -> None:
    source_bytes = b"---\r\nkey: review.system\r\nversion: 1\r\n---\r\nPrompt\r\n"
    source_path = tmp_path / "review.system.v1.md"
    source_path.write_bytes(source_bytes)

    (asset,) = load_prompt_assets(tmp_path)

    assert asset.content == source_bytes.decode("utf-8")
    assert asset.content.encode("utf-8") == source_bytes
    assert asset.checksum == sha256(source_bytes).hexdigest()


def test_load_prompt_assets_rejects_filename_version_mismatch(tmp_path: Path) -> None:
    (tmp_path / "review.system.v2.md").write_text(
        "---\nkey: review.system\nversion: 1\n---\nPrompt\n", encoding="utf-8"
    )

    try:
        load_prompt_assets(tmp_path)
    except ValueError as error:
        assert "does not match frontmatter version" in str(error)
    else:
        raise AssertionError("expected a filename/frontmatter mismatch to be rejected")


@pytest.mark.integration
def test_first_seed_makes_the_newest_version_of_each_prompt_active(
    migrated_prompt_database: tuple[str, str],
) -> None:
    database_url, schema = migrated_prompt_database

    async def seed_and_read_active_versions() -> list[tuple[str, int]]:
        engine = create_async_engine(
            database_url,
            connect_args={"options": f"-csearch_path={schema}"},
        )
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with session_factory() as session:
                inserted = await seed_prompt_versions(
                    session, load_prompt_assets(REPO_ROOT / "review" / "prompts")
                )
                await session.commit()
                rows = await session.execute(
                    select(PromptVersion.key, PromptVersion.version)
                    .where(PromptVersion.is_active.is_(True))
                    .order_by(PromptVersion.key)
                )
                active_versions = [(key, version) for key, version in rows.tuples().all()]
                assert inserted == 7
                return active_versions
        finally:
            await engine.dispose()

    assert asyncio.run(seed_and_read_active_versions()) == [
        ("review.conventions", 2),
        ("review.system", 3),
        ("review.verifier", 2),
    ]


@pytest.mark.integration
def test_seed_activates_version_two_after_deactivating_version_one(
    migrated_prompt_database: tuple[str, str],
) -> None:
    """PostgreSQL's partial unique index permits only one active version per key."""
    database_url, schema = migrated_prompt_database
    version_one = PromptAsset(
        key="review.system",
        version=1,
        content="version one",
        checksum=sha256(b"version one").hexdigest(),
    )
    version_two = PromptAsset(
        key="review.system",
        version=2,
        content="version two",
        checksum=sha256(b"version two").hexdigest(),
    )

    async def seed_versions() -> list[tuple[int, bool]]:
        engine = create_async_engine(
            database_url,
            connect_args={"options": f"-csearch_path={schema}"},
        )
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with session_factory() as session:
                assert await seed_prompt_versions(session, (version_one,)) == 1
                await session.commit()

                assert await seed_prompt_versions(session, (version_two,)) == 1
                await session.commit()

                rows = await session.execute(
                    select(PromptVersion.version, PromptVersion.is_active).order_by(
                        PromptVersion.version
                    )
                )
                return list(rows.tuples().all())
        finally:
            await engine.dispose()

    assert asyncio.run(seed_versions()) == [(1, False), (2, True)]


@pytest.mark.integration
def test_full_seed_upgrades_v2_to_v3_once_and_preserves_old_content(
    migrated_prompt_database: tuple[str, str],
) -> None:
    database_url, schema = migrated_prompt_database
    assets = load_prompt_assets(REPO_ROOT / "review/prompts")
    legacy_assets = tuple(
        asset for asset in assets if (asset.key, asset.version) != ("review.system", 3)
    )
    old_content = (REPO_ROOT / "tests/fixtures/prompt_builder/sample.system.txt").read_text()
    new_content = (REPO_ROOT / "tests/fixtures/prompt_builder/sample.system.v3.txt").read_text()

    async def exercise() -> None:
        engine = create_async_engine(
            database_url, connect_args={"options": f"-csearch_path={schema}"}
        )
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with factory.begin() as session:
                assert await seed_prompt_versions(session, legacy_assets) == 6
            async with factory() as session:
                old = (
                    await session.scalars(
                        select(PromptVersion).where(
                            PromptVersion.key == "review.system", PromptVersion.is_active.is_(True)
                        )
                    )
                ).one()
                assert old.version == 2
                old_id = old.id
            async with factory.begin() as session:
                assert await seed_prompt_versions(session, assets) == 1
            async with factory.begin() as session:
                assert await seed_prompt_versions(session, assets) == 0
            # Read persisted rows through a fresh session, after both commits.
            async with factory() as session:
                rows = list((await session.scalars(select(PromptVersion))).all())
                assert len(rows) == 7
                active = [row for row in rows if row.key == "review.system" and row.is_active]
                assert len(active) == 1
                assert (active[0].version, active[0].content, active[0].checksum) == (
                    3,
                    new_content,
                    "eb851e7fc2329eb7bdff015ec0fda945c46111ec5c884934ed6f6d6803883452",
                )
                old = (
                    await session.scalars(select(PromptVersion).where(PromptVersion.id == old_id))
                ).one()
                assert (old.version, old.content, old.checksum, old.is_active) == (
                    2,
                    old_content,
                    "9177e358c23d1a7cd8987cbf667cf8b705c2afb27eaf8b2237480e4531431d75",
                    False,
                )
        finally:
            await engine.dispose()

    asyncio.run(exercise())


@pytest.mark.integration
def test_seed_rejects_changed_v2_without_overwriting_its_persisted_content(
    migrated_prompt_database: tuple[str, str],
) -> None:
    database_url, schema = migrated_prompt_database
    assets = load_prompt_assets(REPO_ROOT / "review/prompts")
    old = next(asset for asset in assets if (asset.key, asset.version) == ("review.system", 2))
    tampered = replace(old, content="changed v2", checksum=sha256(b"changed v2").hexdigest())
    old_content = (REPO_ROOT / "tests/fixtures/prompt_builder/sample.system.txt").read_text()

    async def exercise() -> None:
        engine = create_async_engine(
            database_url, connect_args={"options": f"-csearch_path={schema}"}
        )
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with factory.begin() as session:
                assert await seed_prompt_versions(session, assets) == 7
            with pytest.raises(ValueError, match=r"immutable prompt changed: review\.system\.v2"):
                async with factory.begin() as session:
                    await seed_prompt_versions(session, (tampered,))
            async with factory() as session:
                persisted = (
                    await session.scalars(
                        select(PromptVersion).where(
                            PromptVersion.key == "review.system", PromptVersion.version == 2
                        )
                    )
                ).one()
                assert (persisted.content, persisted.checksum, persisted.is_active) == (
                    old_content,
                    "9177e358c23d1a7cd8987cbf667cf8b705c2afb27eaf8b2237480e4531431d75",
                    False,
                )
                active = (
                    await session.scalars(
                        select(PromptVersion).where(
                            PromptVersion.key == "review.system", PromptVersion.is_active.is_(True)
                        )
                    )
                ).one()
                assert active.version == 3
        finally:
            await engine.dispose()

    asyncio.run(exercise())
