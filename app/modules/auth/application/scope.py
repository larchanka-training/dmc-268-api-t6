"""Authenticated portal identity and Workspace claim."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True)
class AuthScope:
    user_id: int
    workspace_ids: tuple[UUID, ...]
    expires_at: int | None = None
