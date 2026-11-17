"""What the bot needs from GitHub and Telegram; tests pass fakes, main passes the HTTP clients."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from review_bot.models import PullRequest, Review


class GitHubError(Exception):
    """A GitHub call failed. The message holds no token."""


class ReviewerRejected(GitHubError):
    """GitHub refused this reviewer for this pull request (HTTP 422: not a collaborator, ...)."""


class TelegramError(Exception):
    """A Telegram call failed in transport: the message may or may not have been delivered."""


class TelegramRejected(TelegramError):
    """Telegram answered and refused the call: the message was not delivered."""


@dataclass(frozen=True)
class Update:
    update_id: int
    chat_id: str
    message_id: int
    username: str | None
    text: str


class GitHubPort(Protocol):
    def open_pull_requests(self, owner: str, name: str) -> list[PullRequest]: ...

    def reviews(self, owner: str, name: str, number: int) -> list[Review]: ...

    def request_reviewer(self, owner: str, name: str, number: int, login: str) -> None: ...


class TelegramPort(Protocol):
    def send_message(self, chat_id: str, text: str, reply_to: int | None = None) -> None: ...

    def get_updates(self, offset: int | None, timeout: int) -> list[Update]: ...
