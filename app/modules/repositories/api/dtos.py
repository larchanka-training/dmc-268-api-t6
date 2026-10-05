"""HTTP contract of repository settings (contracts/openapi.yaml: Repository, RepositoryUpdate)."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import ConfigDict, Field, model_validator

from app.modules.reviews.api.dtos import ApiDto


class RepositoryDto(ApiDto):
    id: UUID
    full_name: str
    url: str
    default_branch: str
    enabled: bool
    # deep (SandboxEngine) is phase 3 (SD §13): it has no worker in the MVP.
    default_engine: Literal["fast"]
    wait_for_ci: Literal["auto", "always", "never"]
    max_comments: int
    review_event: Literal["COMMENT", "REQUEST_CHANGES"]


class RepositoryUpdateDto(ApiDto):
    """Omitted fields keep their value; at least one field is required."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool | None = None
    default_engine: Literal["fast"] | None = None
    wait_for_ci: Literal["auto", "always", "never"] | None = None
    max_comments: int | None = Field(default=None, ge=1, le=10)
    review_event: Literal["COMMENT", "REQUEST_CHANGES"] | None = None

    @model_validator(mode="after")
    def require_one_field(self) -> RepositoryUpdateDto:
        if not self.model_fields_set:
            raise ValueError("at least one setting is required")
        for name in self.model_fields_set:
            if getattr(self, name) is None:
                raise ValueError(f"{name} must not be null")
        return self
