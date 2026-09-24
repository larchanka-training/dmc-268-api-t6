"""Producer-facing orchestration for immutable review-run inputs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from app.modules.reviews.application.get_run_diff import (
    DiffSnapshot,
    DiffSnapshotRepository,
    StoreDiffSnapshot,
)


class RunDiffProvider(Protocol):
    """The provider operation run processing uses before reviewing a head SHA."""

    async def fetch_diff(self, *, code_change_id: UUID, head_sha: str) -> list[DiffSnapshot]: ...


@dataclass(frozen=True)
class RunDiffInput:
    code_change_id: UUID
    head_sha: str


class RunProcessingRepository(DiffSnapshotRepository, Protocol):
    async def get_run_diff_input(self, run_id: UUID) -> RunDiffInput | None: ...


class ReviewRunProcessor:
    """Persist provider inputs before downstream review actions consume them."""

    def __init__(self, repository: RunProcessingRepository, provider: RunDiffProvider) -> None:
        self._repository = repository
        self._provider = provider

    async def execute(self, run_id: UUID) -> bool:
        """Fetch and snapshot the exact head associated with a durable run."""

        run = await self._repository.get_run_diff_input(run_id)
        if run is None:
            return False
        files = await self._provider.fetch_diff(
            code_change_id=run.code_change_id,
            head_sha=run.head_sha,
        )
        await StoreDiffSnapshot(self._repository).execute(
            code_change_id=run.code_change_id,
            head_sha=run.head_sha,
            files=files,
        )
        return True
