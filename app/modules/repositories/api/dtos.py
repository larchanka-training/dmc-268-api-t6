"""HTTP contract of repositories (contracts/openapi.yaml).

Schemas: Repository, RepositoryUpdate and the RulesStatus variants.
"""

from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from pydantic import ConfigDict, Field, field_validator, model_validator

from app.common.application.branch_patterns import validate_branch_patterns
from app.modules.code_index.domain.enums import RagIndexStatus
from app.modules.reviews.api.dtos import ApiDto, UtcDatetime


class RagIndexDto(ApiDto):
    """Read-only; the publication fields are set only for ready and stale (#145)."""

    status: RagIndexStatus
    indexed_commit_sha: str | None
    indexed_at: UtcDatetime | None
    file_count: int
    symbol_count: int
    last_error_code: str | None


class RepositoryDto(ApiDto):
    id: UUID
    provider: Literal["github", "gitlab", "bitbucket"]
    full_name: str
    url: str
    default_branch: str
    enabled: bool
    # deep (SandboxEngine) is phase 3 (SD §13): it has no worker in the MVP.
    default_engine: Literal["fast"]
    wait_for_ci: Literal["auto", "always", "never"]
    max_comments: int
    review_event: Literal["COMMENT", "REQUEST_CHANGES"]
    branch_filter: list[str]
    severity_threshold: Literal["all", "warning", "critical"]
    # Read-only: ``enabled`` is the stored preference AND this availability (SD §6.9).
    installation_available: bool
    # Like ``enabled``: the stored preference AND ``installation_available`` (#145).
    rag_enabled: bool
    rag_index: RagIndexDto


class RepositoryUpdateDto(ApiDto):
    """Omitted fields keep their value; at least one field is required."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool | None = None
    default_engine: Literal["fast"] | None = None
    wait_for_ci: Literal["auto", "always", "never"] | None = None
    max_comments: int | None = Field(default=None, ge=1, le=10)
    review_event: Literal["COMMENT", "REQUEST_CHANGES"] | None = None
    branch_filter: list[str] | None = None
    severity_threshold: Literal["all", "warning", "critical"] | None = None
    rag_enabled: bool | None = None

    @field_validator("branch_filter")
    @classmethod
    def validate_branch_filter(cls, value: list[str] | None) -> list[str] | None:
        if value is not None:
            validate_branch_patterns(value)
        return value

    @model_validator(mode="after")
    def require_one_field(self) -> RepositoryUpdateDto:
        if not self.model_fields_set:
            raise ValueError("at least one setting is required")
        for name in self.model_fields_set:
            if getattr(self, name) is None:
                raise ValueError(f"{name} must not be null")
        return self


# The UI schema takes a 40 hex commit SHA only.
CommitSha = Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]


def _is_none(value: object) -> bool:
    return value is None


class RulesDiagnosticDto(ApiDto):
    """RULES_FORMAT_SPEC §9 in camelCase; ``limit``, ``actual`` and ``patternIndex`` are
    omitted, not null, when the error has none."""

    code: str
    message: str
    line: int | None
    column: int | None
    retryable: bool
    limit: int | None = Field(default=None, exclude_if=_is_none)
    actual: int | None = Field(default=None, exclude_if=_is_none)
    pattern_index: int | None = Field(default=None, exclude_if=_is_none)


class PresentRulesStatusDto(ApiDto):
    """The file is there and was read; an empty file is present too."""

    status: Literal["present"]
    branch: str
    path: str
    sha: CommitSha
    checked_at: UtcDatetime
    text: str
    checksum: str
    ignore_masks: list[str]
    diagnostics: list[RulesDiagnosticDto]


class MissingRulesStatusDto(ApiDto):
    """The file is reliably absent; the service defaults of ``defaultsVersion`` apply."""

    status: Literal["missing"]
    branch: str
    path: str
    sha: CommitSha
    checked_at: UtcDatetime
    defaults_version: str


class InvalidRulesStatusDto(ApiDto):
    """The file is there and was refused; ``text`` is null when it could not be decoded."""

    status: Literal["invalid"]
    branch: str
    path: str
    sha: CommitSha
    checked_at: UtcDatetime
    text: str | None
    diagnostics: list[RulesDiagnosticDto] = Field(min_length=1)


class UnavailableRulesStatusDto(ApiDto):
    """The read proved nothing about the file; ``sha`` is null without a resolved revision."""

    status: Literal["unavailable"]
    branch: str
    path: str
    sha: CommitSha | None
    checked_at: UtcDatetime
    diagnostics: list[RulesDiagnosticDto] = Field(min_length=1)


class PendingRulesStatusDto(ApiDto):
    """Not read yet: the read is requested and a later GET returns its result."""

    status: Literal["pending"]
    branch: str
    path: str
    sha: None
    checked_at: None


# The variant is told by ``status``, never by a message text.
RulesStatusDto = Annotated[
    PresentRulesStatusDto
    | MissingRulesStatusDto
    | InvalidRulesStatusDto
    | UnavailableRulesStatusDto
    | PendingRulesStatusDto,
    Field(discriminator="status"),
]
