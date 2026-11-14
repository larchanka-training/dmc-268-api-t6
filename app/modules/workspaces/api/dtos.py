"""Data transfer objects for workspace management endpoints (#550)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class DeleteWorkspaceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirmation_name: str = Field(
        min_length=1,
        max_length=255,
        description="Exact name of the workspace to confirm permanent deletion.",
    )
