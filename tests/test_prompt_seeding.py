"""Unit contracts for versioned prompt assets used by the deploy seed command."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.schema import CreateSchema, DropSchema

from alembic import command
from app.bootstrap.seed_prompts import load_prompt_assets, seed_prompt_versions
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
        ("review.system", 1),
    ]
    for asset in assets:
        expected_content = (prompts_dir / f"{asset.key}.v{asset.version}.md").read_text(
            encoding="utf-8"
        )
        assert asset.content == expected_content
        assert asset.checksum == sha256(expected_content.encode("utf-8")).hexdigest()


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
def test_first_seed_makes_both_version_one_prompts_active(
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
                assert inserted == 2
                return active_versions
        finally:
            await engine.dispose()

    assert asyncio.run(seed_and_read_active_versions()) == [
        ("review.conventions", 1),
        ("review.system", 1),
    ]
