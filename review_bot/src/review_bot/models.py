"""Plain data the bot works with: pull requests as GitHub returns them and tracked requests."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


@dataclass(frozen=True)
class PullRequest:
    repo: str  # owner/name
    number: int
    title: str
    author: str
    url: str
    draft: bool
    labels: frozenset[str]
    requested_reviewers: tuple[str, ...]  # user logins as GitHub returns them
    requested_teams: tuple[str, ...] = ()


@dataclass(frozen=True)
class Review:
    login: str
    submitted: bool  # False for a pending (unsubmitted) review


class Status(StrEnum):
    # Requested in GitHub, the reviewer has not reviewed this round yet.
    WAITING = "waiting"
    # The reviewer left a review after the request; a new request starts the next round.
    REVIEWED = "reviewed"
    # Unrequested without a review; a new request continues the same round.
    REMOVED = "removed"


@dataclass(frozen=True)
class Request:
    """One reviewer on one pull request, as the bot tracks it."""

    repo: str
    number: int
    reviewer: str  # lower-case login
    login: str  # login as GitHub spells it
    round: int
    status: Status
    queued_at: datetime
    notified: bool
    reviews_seen: int


@dataclass(frozen=True)
class Item:
    """A line of a message: a waiting request with its pull request."""

    request: Request
    pr: PullRequest
    urgent: bool
