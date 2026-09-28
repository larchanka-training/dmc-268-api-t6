"""Strict transport DTO for signed GitHub webhook JSON."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

_POSTGRES_BIGINT_MAX = 9223372036854775807


class GitHubInstallationDto(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: int = Field(strict=True, ge=1, le=_POSTGRES_BIGINT_MAX)


class GitHubWebhookPayloadDto(BaseModel):
    """Validate known fields while retaining the complete provider payload."""

    model_config = ConfigDict(extra="allow")

    action: str | None = Field(default=None, max_length=100, strict=True)
    installation: GitHubInstallationDto | None = None
