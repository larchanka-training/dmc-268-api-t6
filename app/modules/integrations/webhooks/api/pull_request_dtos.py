"""Strict GitHub pull-request webhook payload at the transport boundary."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal, get_args

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, HttpUrl

from app.modules.reviews.application.project_github_pull_request import (
    PullRequestEvent,
    PullRequestLabelEvent,
    PullRequestState,
)

_MAX_BIGINT = 2**63 - 1
_SHA_PATTERN = r"^(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})$"
PullRequestWebhookAction = Literal[
    "opened",
    "synchronize",
    "edited",
    "review_requested",
    "review_request_removed",
    "closed",
    "reopened",
    "labeled",
    "unlabeled",
]
SUPPORTED_PULL_REQUEST_ACTIONS = frozenset(get_args(PullRequestWebhookAction))


class PullRequestPayloadValidationError(ValueError):
    """A pull request payload breaks a rule the schema alone does not check.

    ``fields`` names the failing payload fields, never their values, so a caller can
    log why a payload was rejected safely.
    """

    def __init__(self, message: str, *, fields: tuple[str, ...]) -> None:
        super().__init__(message)
        self.fields = fields


class _GitHubIdDto(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    id: int = Field(gt=0, le=_MAX_BIGINT)


class _GitHubRepositoryDto(_GitHubIdDto):
    full_name: str = Field(pattern=r"^[^/]+/[^/]+$", max_length=512)


class _GitHubUserDto(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    login: str = Field(min_length=1, max_length=255)


class _GitHubSenderDto(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    type: str = Field(min_length=1, max_length=50)
    login: str | None = Field(default=None, min_length=1, max_length=255)


class _GitHubLabelDto(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    name: str = Field(min_length=1, max_length=255)


class _GitHubRefDto(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    ref: str = Field(min_length=1, max_length=255)
    sha: str = Field(pattern=_SHA_PATTERN)


class _GitHubPullRequestDto(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    id: int = Field(gt=0, le=_MAX_BIGINT)
    number: int = Field(gt=0, le=2**31 - 1)
    title: str = Field(min_length=1, max_length=500)
    body: str | None = None
    html_url: HttpUrl
    user: _GitHubUserDto
    head: _GitHubRefDto
    base: _GitHubRefDto
    state: Literal["open", "closed"]
    merged: bool = False
    updated_at: AwareDatetime = Field(strict=False)


class _GitHubPullRequestPayloadDto(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    action: PullRequestWebhookAction
    number: int | None = Field(default=None, gt=0, le=2**31 - 1)
    installation: _GitHubIdDto
    repository: _GitHubRepositoryDto
    pull_request: _GitHubPullRequestDto
    requested_reviewer: _GitHubUserDto | None = None
    label: _GitHubLabelDto | None = None
    sender: _GitHubSenderDto | None = None


def parse_pull_request_event(payload: Mapping[str, object]) -> PullRequestEvent:
    parsed = _GitHubPullRequestPayloadDto.model_validate(payload)
    return _event_from_payload(parsed)


def parse_pull_request_label_event(payload: Mapping[str, object]) -> PullRequestLabelEvent:
    parsed = _GitHubPullRequestPayloadDto.model_validate(payload)
    if parsed.action not in {"labeled", "unlabeled"} or parsed.label is None:
        raise PullRequestPayloadValidationError(
            "GitHub pull request label action requires a label name", fields=("label",)
        )
    return PullRequestLabelEvent(_event_from_payload(parsed), parsed.label.name)


def _event_from_payload(parsed: _GitHubPullRequestPayloadDto) -> PullRequestEvent:
    item = parsed.pull_request
    if parsed.number is not None and parsed.number != item.number:
        raise PullRequestPayloadValidationError(
            "GitHub pull request number mismatch", fields=("number",)
        )
    state = (
        PullRequestState.MERGED
        if item.merged
        else PullRequestState.OPEN
        if item.state == "open"
        else PullRequestState.CLOSED
    )
    return PullRequestEvent(
        action=parsed.action,
        installation_external_id=parsed.installation.id,
        repository_external_id=parsed.repository.id,
        external_id=item.id,
        number=item.number,
        title=item.title,
        description=item.body,
        author_login=item.user.login,
        web_url=str(item.html_url),
        source_branch=item.head.ref,
        target_branch=item.base.ref,
        base_sha=item.base.sha,
        head_sha=item.head.sha,
        state=state,
        provider_updated_at=item.updated_at,
        repository_full_name=parsed.repository.full_name,
        requested_reviewer_login=(
            parsed.requested_reviewer.login if parsed.requested_reviewer is not None else None
        ),
        sender_type=parsed.sender.type if parsed.sender is not None else None,
        sender_login=parsed.sender.login if parsed.sender is not None else None,
    )
