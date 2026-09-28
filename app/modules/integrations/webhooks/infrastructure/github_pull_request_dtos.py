"""Validate outbound GitHub REST PR snapshots inside the infrastructure adapter."""

from __future__ import annotations

from dataclasses import replace
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, HttpUrl

from app.modules.reviews.application.project_github_pull_request import (
    PullRequestEvent,
    PullRequestState,
)


class _User(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    login: str = Field(min_length=1, max_length=255)


class _Ref(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    ref: str = Field(min_length=1, max_length=255)
    sha: str = Field(pattern=r"^(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})$")


class _Label(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    name: str = Field(min_length=1, max_length=255)


class _CurrentPullRequest(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    id: int = Field(gt=0, le=2**63 - 1)
    number: int = Field(gt=0, le=2**31 - 1)
    title: str = Field(min_length=1, max_length=500)
    body: str | None = None
    html_url: HttpUrl
    user: _User
    head: _Ref
    base: _Ref
    state: Literal["open", "closed"]
    merged: bool = False
    updated_at: AwareDatetime = Field(strict=False)
    labels: list[_Label]


def parse_current_pull_request(event: PullRequestEvent, payload: object) -> PullRequestEvent:
    item = _CurrentPullRequest.model_validate(payload)
    if item.id != event.external_id or item.number != event.number:
        raise ValueError("GitHub current pull request identity mismatch")
    state = (
        PullRequestState.MERGED
        if item.merged
        else PullRequestState.OPEN
        if item.state == "open"
        else PullRequestState.CLOSED
    )
    return replace(
        event,
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
        current_label_names=frozenset(label.name for label in item.labels),
    )
