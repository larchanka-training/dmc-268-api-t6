"""Explicit active installation predicate for standalone provider/projector unit tests."""

from __future__ import annotations

from uuid import UUID


async def active_installation(provider_installation_id: UUID) -> bool:
    return True


async def provider_checkpoint() -> None:
    """Explicit trusted no-op for standalone HTTP adapter unit tests."""
