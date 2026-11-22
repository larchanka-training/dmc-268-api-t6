"""Shared versioned prompt assets and seed helpers for review tests."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bootstrap.seed_prompts import PromptAsset, load_prompt_assets, seed_prompt_versions
from tests.review_prompt_fakes import REPO_ROOT


def _up_to(assets: tuple[PromptAsset, ...], version: int) -> tuple[PromptAsset, ...]:
    return tuple(
        asset for asset in assets if not (asset.key == "review.system" and asset.version > version)
    )


def _packaged(*keys: str) -> tuple[PromptAsset, ...]:
    # portal_schema seeds a fake review.system v1 whose checksum is not the packaged file's;
    # seeding that version again would be refused as an immutable prompt change.
    return tuple(
        asset
        for asset in load_prompt_assets(REPO_ROOT / "review" / "prompts")
        if (asset.key, asset.version) != ("review.system", 1) and (not keys or asset.key in keys)
    )


async def _seed(factory: async_sessionmaker[AsyncSession], assets: tuple[PromptAsset, ...]) -> None:
    async with factory.begin() as session:
        await seed_prompt_versions(session, assets)


def _live_assets() -> tuple[PromptAsset, ...]:
    # Bound to v3 explicitly: a newer packaged review.system (F2's v4) is not seeded here.
    return tuple(
        asset for asset in _packaged() if not (asset.key == "review.system" and asset.version > 3)
    )
