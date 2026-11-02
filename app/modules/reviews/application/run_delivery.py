"""Admission of detached queue claims against a persisted immutable Run identity."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from app.modules.reviews.application.handle_review_run import DeliveryOutcome


@dataclass(frozen=True)
class RunDeliveryPointer:
    """Immutable claims only; delivery clocks, attempts and mutable names are omitted."""

    run_id: UUID
    provider: str
    workspace_id: UUID
    repository_id: UUID
    repository_external_id: int
    installation_id: int
    pr_number: int
    head_sha: str
    base_sha: str
    engine: str
    rule_version_id: UUID
    prompt_version_id: UUID
    trigger: str


@dataclass(frozen=True)
class PersistedRunIdentity:
    """Detached claims reached through the Run's persisted origin, never a wire ID."""

    pointer: RunDeliveryPointer
    origin_provider_installation_id: UUID


class RunDeliveryReader(Protocol):
    async def read(self, run_id: UUID) -> PersistedRunIdentity | None: ...


class RunDeliveryUnavailable(Exception):
    def __init__(self) -> None:
        super().__init__("Run delivery identity unavailable")


class HandleRunDelivery:
    """Compare admission claims before the existing lifecycle handler takes authority."""

    def __init__(
        self,
        reader: RunDeliveryReader,
        handler: Callable[[UUID], Awaitable[DeliveryOutcome]],
    ) -> None:
        self._reader = reader
        self._handler = handler

    async def execute(self, pointer: RunDeliveryPointer) -> DeliveryOutcome:
        identity = await self._reader.read(pointer.run_id)
        if identity is None:
            return DeliveryOutcome.ACK
        if pointer.provider not in {"github", "gitlab", "bitbucket"} or pointer != identity.pointer:
            return DeliveryOutcome.DEAD_LETTER
        return await self._handler(identity.pointer.run_id)
