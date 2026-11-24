"""In-memory GitHub and Telegram, a settable clock and a settings builder. No network."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta

from review_bot.config import Member, Repo, Settings, Thresholds
from review_bot.models import PullRequest, Review
from review_bot.ports import (
    GitHubError,
    ReviewerRejected,
    TelegramError,
    TelegramRejected,
    Update,
)

API = "acme/api"
UI = "acme/ui"
CHAT = "-100500"

TEAM = (
    Member(github="Alice-Example", telegram="alice_tg"),
    Member(github="bob-example", telegram="bob_tg"),
    Member(github="carol-example", telegram="carol_tg"),
)


def make_settings(db_path: str, team: tuple[Member, ...] = TEAM) -> Settings:
    return Settings(
        app_id="1",
        app_private_key="unused",
        telegram_token="unused",
        chat_id=CHAT,
        team=team,
        repos=(Repo("acme", "api", "api"), Repo("acme", "ui", "ui")),
        db_path=db_path,
        poll_seconds=60,
        heartbeat_path=db_path + ".alive",
        thresholds=Thresholds(),
    )


class Clock:
    def __init__(self, start: datetime | None = None) -> None:
        # A Monday morning in Berlin, far from the 18:00 digest.
        self.now = start or datetime(2026, 11, 16, 8, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> None:
        self.now += timedelta(**delta)


@dataclass
class FakeGitHub:
    prs: dict[tuple[str, int], PullRequest] = field(default_factory=dict)
    review_log: dict[tuple[str, int], list[Review]] = field(default_factory=dict)
    requested_log: list[tuple[str, int, str]] = field(default_factory=list)
    rejected: set[str] = field(default_factory=set)
    fail_list: bool = False

    def open(
        self,
        repo: str,
        number: int,
        author: str,
        *,
        title: str | None = None,
        draft: bool = False,
        reviewers: tuple[str, ...] = (),
        teams: tuple[str, ...] = (),
        labels: frozenset[str] = frozenset(),
    ) -> None:
        self.prs[(repo, number)] = PullRequest(
            repo=repo,
            number=number,
            title=title or f"Change {number}",
            author=author,
            url=f"https://github.com/{repo}/pull/{number}",
            draft=draft,
            labels=labels,
            requested_reviewers=reviewers,
            requested_teams=teams,
        )

    def update(self, repo: str, number: int, **changes: object) -> None:
        self.prs[(repo, number)] = replace(self.prs[(repo, number)], **changes)  # type: ignore[arg-type]

    def close(self, repo: str, number: int) -> None:
        del self.prs[(repo, number)]

    def submit_review(self, repo: str, number: int, login: str) -> None:
        """A review as GitHub does it: the review is stored and the request is removed."""
        self.review_log.setdefault((repo, number), []).append(Review(login=login, submitted=True))
        pr = self.prs[(repo, number)]
        self.update(
            repo,
            number,
            requested_reviewers=tuple(r for r in pr.requested_reviewers if r != login),
        )

    def add_reviewer(self, repo: str, number: int, login: str) -> None:
        pr = self.prs[(repo, number)]
        self.update(repo, number, requested_reviewers=(*pr.requested_reviewers, login))

    def remove_reviewer(self, repo: str, number: int, login: str) -> None:
        pr = self.prs[(repo, number)]
        self.update(
            repo,
            number,
            requested_reviewers=tuple(r for r in pr.requested_reviewers if r != login),
        )

    # GitHubPort

    def open_pull_requests(self, owner: str, name: str) -> list[PullRequest]:
        if self.fail_list:
            raise GitHubError("GitHub GET /pulls: HTTP 502")
        repo = f"{owner}/{name}"
        return [pr for (r, _), pr in sorted(self.prs.items()) if r == repo]

    def reviews(self, owner: str, name: str, number: int) -> list[Review]:
        return list(self.review_log.get((f"{owner}/{name}", number), []))

    def request_reviewer(self, owner: str, name: str, number: int, login: str) -> None:
        if login in self.rejected:
            raise ReviewerRejected("HTTP 422")
        repo = f"{owner}/{name}"
        self.requested_log.append((repo, number, login))
        self.add_reviewer(repo, number, login)


@dataclass
class FakeTelegram:
    sent: list[tuple[str, str, int | None]] = field(default_factory=list)
    updates: list[Update] = field(default_factory=list)
    reject_next: int = 0
    fail_next: int = 0
    reject_after: int | None = None

    def send_message(self, chat_id: str, text: str, reply_to: int | None = None) -> None:
        if self.reject_next:
            self.reject_next -= 1
            raise TelegramRejected("Telegram sendMessage: HTTP 400")
        if self.fail_next:
            self.fail_next -= 1
            raise TelegramError("Telegram sendMessage: connection reset")
        if self.reject_after is not None and len(self.sent) >= self.reject_after:
            raise TelegramRejected("Telegram sendMessage: HTTP 400")
        self.sent.append((chat_id, text, reply_to))

    def get_updates(self, offset: int | None, timeout: int) -> list[Update]:
        pending = [u for u in self.updates if offset is None or u.update_id >= offset]
        return pending

    @property
    def texts(self) -> list[str]:
        return [text for _, text, _ in self.sent]
