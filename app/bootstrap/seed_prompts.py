"""Seed immutable, versioned review prompts into ``prompt_versions`` at deploy time.

Run from a checkout that contains ``review/prompts``::

    uv run python -m app.bootstrap.seed_prompts

The application never reads these files at runtime; this command copies their complete
contents into PostgreSQL before the application image is deployed.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.modules.reviews.infrastructure.models import PromptVersion

_FILENAME_RE = re.compile(r"^(?P<key>.+)\.v(?P<version>\d+)\.md$")
_FRONTMATTER_RE = re.compile(r"\A---\r?\n(?P<fields>.*?)\r?\n---\r?\n", re.DOTALL)
_FIELD_RE = re.compile(r"^(?P<name>key|version):\s*(?P<value>.+?)\s*$")


@dataclass(frozen=True)
class PromptAsset:
    """A prompt artifact ready to be persisted, including its source checksum."""

    key: str
    version: int
    content: str
    checksum: str


def load_prompt_assets(prompts_dir: Path) -> tuple[PromptAsset, ...]:
    """Load and validate all ``*.vN.md`` assets from a deploy checkout."""
    assets: list[PromptAsset] = []
    for path in sorted(prompts_dir.glob("*.md")):
        filename = _FILENAME_RE.fullmatch(path.name)
        if filename is None:
            raise ValueError(f"prompt filename must match <key>.v<N>.md: {path}")

        source_bytes = path.read_bytes()
        content = source_bytes.decode("utf-8")
        frontmatter = _FRONTMATTER_RE.match(content)
        if frontmatter is None:
            raise ValueError(f"prompt has no YAML frontmatter: {path}")
        fields = _parse_frontmatter(frontmatter.group("fields"), path)
        key = fields["key"]
        version = _parse_version(fields["version"], path)

        if key != filename.group("key"):
            raise ValueError(f"{path}: filename key does not match frontmatter key")
        if version != int(filename.group("version")):
            raise ValueError(f"{path}: filename version does not match frontmatter version")

        assets.append(
            PromptAsset(
                key=key,
                version=version,
                content=content,
                checksum=sha256(source_bytes).hexdigest(),
            )
        )
    if not assets:
        raise ValueError(f"no prompt files found in {prompts_dir}")
    return tuple(assets)


def _parse_frontmatter(text: str, path: Path) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in text.splitlines():
        match = _FIELD_RE.fullmatch(line)
        if match is not None:
            fields[match.group("name")] = match.group("value").strip("\"'")
    missing = {"key", "version"} - fields.keys()
    if missing:
        raise ValueError(f"{path}: frontmatter lacks {', '.join(sorted(missing))}")
    return fields


def _parse_version(value: str, path: Path) -> int:
    try:
        return int(value)
    except ValueError as error:
        raise ValueError(f"{path}: frontmatter version must be an integer") from error


async def seed_prompt_versions(session: AsyncSession, assets: tuple[PromptAsset, ...]) -> int:
    """Insert assets without mutating old versions and activate each newest version.

    An already-persisted ``(key, version)`` whose checksum differs is an immutable
    artifact violation and aborts the deploy.  The newest known version for every
    seeded key is the active one.
    """
    inserted = 0
    assets_by_key: dict[str, list[PromptAsset]] = {}
    for asset in assets:
        assets_by_key.setdefault(asset.key, []).append(asset)

    for key, key_assets in assets_by_key.items():
        result = await session.scalars(select(PromptVersion).where(PromptVersion.key == key))
        existing = {row.version: row for row in result}
        for asset in key_assets:
            current = existing.get(asset.version)
            if current is None:
                session.add(
                    PromptVersion(
                        key=asset.key,
                        version=asset.version,
                        content=asset.content,
                        checksum=asset.checksum,
                        is_active=False,
                    )
                )
                inserted += 1
            elif current.checksum != asset.checksum:
                raise ValueError(
                    f"immutable prompt changed: {asset.key}.v{asset.version} "
                    f"has checksum {current.checksum} in database"
                )

        active_version = max((*existing.keys(), *(asset.version for asset in key_assets)))
        await session.execute(
            update(PromptVersion)
            .where(PromptVersion.key == key)
            .values(is_active=PromptVersion.version == active_version)
        )
    return inserted


def _default_prompts_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "review" / "prompts"


async def _seed(database_url: str, prompts_dir: Path) -> int:
    engine = create_async_engine(database_url, pool_pre_ping=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            inserted = await seed_prompt_versions(session, load_prompt_assets(prompts_dir))
            await session.commit()
            return inserted
    finally:
        await engine.dispose()


def main() -> None:
    """Run the deploy-time prompt seed command."""
    parser = argparse.ArgumentParser(description="Seed versioned review prompts into PostgreSQL.")
    parser.add_argument("--prompts-dir", type=Path, default=_default_prompts_dir())
    args = parser.parse_args()
    database_url = os.environ.get("DATABASE_URL")
    if database_url is None:
        parser.error("DATABASE_URL must be set")
    inserted = asyncio.run(_seed(database_url, args.prompts_dir))
    print(f"Seeded {inserted} prompt version(s).")


if __name__ == "__main__":
    main()
