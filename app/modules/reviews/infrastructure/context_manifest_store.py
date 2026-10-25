"""``context_manifests`` rows and their ``context.manifest`` steps (#130 F1-6, CVS §5).

The manifest is the checkpoint of what one snapshot-path provider call was shown: every
``ManifestBlock`` field of every block, so a block of any source role (#144's
``retrieved_excerpt`` too) round-trips without a schema change.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from hashlib import sha256
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.common.infrastructure.db.unit_of_work import SqlAlchemyUnitOfWork
from app.modules.reviews.application.llm import ManifestEntry
from app.modules.reviews.application.policy_snapshot import ManifestBlock
from app.modules.reviews.infrastructure.models import ContextManifest
from app.modules.reviews.infrastructure.run_action_records import insert_run_action

CONTEXT_MANIFEST_TOOL = "context.manifest"


def manifest_block_json(block: ManifestBlock) -> dict[str, Any]:
    """Every field of the block as JSON: ranges as lists, the repository id as text."""
    value = asdict(block)
    value["source_role"] = block.source_role.value
    value["shown_ranges"] = [list(item) for item in block.shown_ranges]
    value["repository_id"] = None if block.repository_id is None else str(block.repository_id)
    return value


def tokens_by_role(blocks: tuple[ManifestBlock, ...]) -> dict[str, int]:
    """The counted tokens of the blocks per source role, in first-seen order."""
    totals: dict[str, int] = {}
    for block in blocks:
        if block.tokens is not None:
            role = block.source_role.value
            totals[role] = totals.get(role, 0) + block.tokens
    return totals


class SqlAlchemyContextManifestRepository:
    """``ContextManifestRepository``: the row and its step in the caller's transaction."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, run_id: UUID, entry: ManifestEntry) -> None:
        manifest = entry.manifest
        blocks = [manifest_block_json(block) for block in manifest.blocks]
        canonical = json.dumps(blocks, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        digest = sha256(canonical.encode("utf-8")).hexdigest()
        self._session.add(
            ContextManifest(
                id=entry.manifest_id,
                run_id=run_id,
                attempt=entry.attempt,
                call_id=manifest.call_id,
                stage=manifest.stage,
                recovery_kind=manifest.recovery_kind,
                parent_call_id=entry.parent_call_id,
                policy_snapshot_id=entry.policy_snapshot_id,
                digest=digest,
                block_count=len(blocks),
                input_tokens=entry.input_tokens,
                blocks=blocks,
            )
        )
        await self._session.flush()
        # PIPELINE_SPEC §2: ids, digest and counters only; the blocks stay in the row.
        await insert_run_action(
            self._session,
            run_id=run_id,
            tool=CONTEXT_MANIFEST_TOOL,
            request={
                "call_id": str(manifest.call_id),
                "stage": manifest.stage,
                "policy_snapshot_id": str(entry.policy_snapshot_id),
                "diff_snapshot_id": None,
            },
            response={
                "manifest_id": str(entry.manifest_id),
                "digest": digest,
                "block_count": len(blocks),
                "input_tokens": entry.input_tokens,
                "tokens_by_role": tokens_by_role(manifest.blocks),
            },
            started_at=entry.started_at,
            # The step is its own write; the time it takes is the transaction's.
            duration_ms=0,
        )


class SqlAlchemyContextManifestUnitOfWork(SqlAlchemyUnitOfWork):
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        super().__init__(session_factory)

    @property
    def manifests(self) -> SqlAlchemyContextManifestRepository:
        return SqlAlchemyContextManifestRepository(self.session)
