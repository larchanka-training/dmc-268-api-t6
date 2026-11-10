"""The packaged review.system rows and the binders that pin a run to one of them (#130, #498)."""

from __future__ import annotations

from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bootstrap.seed_prompts import PromptAsset, load_prompt_assets
from tests.review_prompt_fakes import REPO_ROOT


def _packaged_review(version: int) -> PromptAsset:
    return next(
        asset
        for asset in load_prompt_assets(REPO_ROOT / "review/prompts")
        if (asset.key, asset.version) == ("review.system", version)
    )


# The packaged review.system v4 (F2), v5 (#144) and v7 (#498), as the seed stores them.
_V4 = _packaged_review(4)
_V4_CONTENT = _V4.content
_V5 = _packaged_review(5)
_V5_CONTENT = _V5.content
_V7 = _packaged_review(7)
_V7_CONTENT = _V7.content


async def _bind_review(
    factory: async_sessionmaker[AsyncSession], run_id: UUID, review: PromptAsset
) -> UUID:
    """Bind the run to an inactive row of a packaged review.system; these tests seed up to
    v3 only."""
    prompt = uuid4()
    async with factory.begin() as session:
        await session.execute(
            text(
                "INSERT INTO prompt_versions (id, key, version, content, checksum, is_active) "
                "VALUES (:id, 'review.system', :version, :content, :checksum, false)"
            ),
            {
                "id": prompt,
                "version": review.version,
                "content": review.content,
                "checksum": review.checksum,
            },
        )
        await session.execute(
            text("UPDATE runs SET prompt_version_id=:prompt WHERE id=:id"),
            {"prompt": prompt, "id": run_id},
        )
    return prompt


async def _bind_v4(factory: async_sessionmaker[AsyncSession], run_id: UUID) -> UUID:
    return await _bind_review(factory, run_id, _V4)


async def _bind_v5(factory: async_sessionmaker[AsyncSession], run_id: UUID) -> UUID:
    return await _bind_review(factory, run_id, _V5)


async def _bind_v7(factory: async_sessionmaker[AsyncSession], run_id: UUID) -> UUID:
    return await _bind_review(factory, run_id, _V7)
