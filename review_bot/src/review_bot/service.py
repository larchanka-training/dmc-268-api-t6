"""One poll of the bot (`Bot.tick`) and the Telegram commands (`Bot.handle_updates`).

A tick: read the open pull requests of every repo, track who is requested (a reviewer who
reviewed and is requested again starts round N+1), request a reviewer on a pull request without
requested reviewers or completed reviews, then send the evening digest or the batch message when
their rule holds. GitHub and Telegram calls never run inside a store transaction.
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Callable, Collection, Mapping
from dataclasses import replace
from datetime import UTC, datetime

from review_bot import messages, selection
from review_bot.config import Repo, Settings
from review_bot.models import Item, PullRequest, Request, Status
from review_bot.ports import (
    GitHubError,
    GitHubPort,
    ReviewerRejected,
    TelegramError,
    TelegramPort,
    TelegramRejected,
    Update,
)
from review_bot.store import Key, Store

log = logging.getLogger("review_bot")

META_LAST_ASSIGNED = "last_assigned"
META_DIGEST_DATE = "digest_date"
META_TELEGRAM_OFFSET = "telegram_offset"


def utc_now() -> datetime:
    return datetime.now(UTC)


def _reviews_needed(requested: Mapping[str, str], tracked: Mapping[str, Request]) -> bool:
    for key in requested:
        old = tracked.get(key)
        if old is None or old.status is Status.REVIEWED:
            return True
    for key, old in tracked.items():
        if old.status is Status.WAITING and key not in requested:
            return True
    return False


def next_request_changes(
    pr: PullRequest,
    tracked: Mapping[str, Request],
    requested: Mapping[str, str],
    submitted_counts: Mapping[str, int],
    now: datetime,
) -> list[Request]:
    """Pure round/status transition for tracked reviewers of one pull request."""
    changes: list[Request] = []
    for key, login in requested.items():
        old = tracked.get(key)
        if old is None:
            changes.append(
                Request(
                    repo=pr.repo,
                    number=pr.number,
                    reviewer=key,
                    login=login,
                    round=1,
                    status=Status.WAITING,
                    queued_at=now,
                    notified=False,
                    reviews_seen=submitted_counts.get(key, 0),
                )
            )
        elif old.status is Status.REVIEWED:
            # Re-request after a review: GitHub has no event for it, the state change is it.
            changes.append(
                replace(
                    old,
                    round=old.round + 1,
                    status=Status.WAITING,
                    queued_at=now,
                    notified=False,
                    reviews_seen=submitted_counts.get(key, 0),
                )
            )
        elif old.status is Status.REMOVED:
            # Added back without a review in between: the same round, notified or not.
            changes.append(replace(old, status=Status.WAITING))
    for key, old in tracked.items():
        if old.status is Status.WAITING and key not in requested:
            reviewed = submitted_counts.get(key, 0) > old.reviews_seen
            changes.append(replace(old, status=Status.REVIEWED if reviewed else Status.REMOVED))
    return changes


class Bot:
    def __init__(
        self,
        settings: Settings,
        store: Store,
        github: GitHubPort,
        telegram: TelegramPort,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.settings = settings
        self.store = store
        self.github = github
        self.telegram = telegram
        self.clock = clock
        self._repos = {repo.full_name: repo for repo in settings.repos}

    @property
    def _aliases(self) -> dict[str, str]:
        return {name: repo.alias for name, repo in self._repos.items()}

    # the poll

    def tick(self) -> None:
        now = self.clock()
        polled: dict[str, list[PullRequest]] = {}
        try:
            for repo in self.settings.repos:
                polled[repo.full_name] = self.github.open_pull_requests(repo.owner, repo.name)
        except GitHubError as error:
            # Nothing changes on a partial poll: a missing repo would look like closed PRs.
            # Evening digest reads the last SQLite snapshot and does not need a fresh poll.
            log.warning("poll skipped: %s", error)
            self._digest(now)
            return
        for full_name, prs in polled.items():
            with self.store.transaction():
                self.store.save_open_prs(full_name, prs)
        for full_name, prs in polled.items():
            for pr in prs:
                # A draft is not tracked: its requests count from the moment it is ready.
                if not pr.draft:
                    self._sync_requests(self._repos[full_name], pr, now)
        self._assign(polled, now)
        if not self._digest(now):
            self._batch(now)

    def _sync_requests(self, repo: Repo, pr: PullRequest, now: datetime) -> None:
        tracked = self.store.requests_of(pr.repo, pr.number)
        requested = {login.lower(): login for login in pr.requested_reviewers}
        submitted_counts: Counter[str] = Counter()
        if _reviews_needed(requested, tracked):
            try:
                submitted_counts = Counter(
                    review.login.lower()
                    for review in self.github.reviews(repo.owner, repo.name, pr.number)
                    if review.submitted
                )
            except GitHubError as error:
                log.warning(
                    "%s#%d: reviews not read, sync postponed: %s", pr.repo, pr.number, error
                )
                return
        changes = next_request_changes(pr, tracked, requested, submitted_counts, now)
        if changes:
            with self.store.transaction():
                for change in changes:
                    self.store.put_request(change)

    def _assign(self, polled: dict[str, list[PullRequest]], now: datetime) -> None:
        load: Counter[str] = Counter()
        for prs in polled.values():
            for pr in prs:
                if not pr.draft:
                    load.update(login.lower() for login in pr.requested_reviewers)
        for full_name, prs in polled.items():
            repo = self._repos[full_name]
            for pr in prs:
                if pr.draft or pr.requested_reviewers or pr.requested_teams:
                    continue
                tracked = self.store.requests_of(pr.repo, pr.number)
                if any(req.status is Status.REVIEWED for req in tracked.values()):
                    continue
                if self.store.assign_done(pr.repo, pr.number):
                    continue
                removed = frozenset(
                    key for key, req in tracked.items() if req.status is Status.REMOVED
                )
                chosen = self._assign_one(repo, pr, load, now, excluded=removed)
                if chosen is not None:
                    load[chosen] += 1

    def _assign_one(
        self,
        repo: Repo,
        pr: PullRequest,
        load: Counter[str],
        now: datetime,
        *,
        excluded: Collection[str] = (),
    ) -> str | None:
        try:
            reviewed = any(
                review.submitted and review.login.lower() != pr.author.lower()
                for review in self.github.reviews(repo.owner, repo.name, pr.number)
            )
        except GitHubError as error:
            log.warning("%s#%d: not assigned, retry next poll: %s", pr.repo, pr.number, error)
            return None
        if reviewed:
            # Someone already reviewed it (e.g. before the bot started): never assign over that.
            with self.store.transaction():
                self.store.mark_assign_done(pr.repo, pr.number)
            return None
        last = self.store.get_meta(META_LAST_ASSIGNED)
        for member in selection.candidates(
            self.settings.team, pr.author, load, last, excluded=excluded
        ):
            try:
                self.github.request_reviewer(repo.owner, repo.name, pr.number, member.github)
            except ReviewerRejected:
                log.warning("%s#%d: GitHub refused reviewer %s", pr.repo, pr.number, member.key)
                continue
            except GitHubError as error:
                log.warning("%s#%d: not assigned, retry next poll: %s", pr.repo, pr.number, error)
                return None
            with self.store.transaction():
                self.store.set_meta(META_LAST_ASSIGNED, member.key)
                self.store.put_request(
                    Request(
                        repo=pr.repo,
                        number=pr.number,
                        reviewer=member.key,
                        login=member.github,
                        round=1,
                        status=Status.WAITING,
                        queued_at=now,
                        notified=False,
                        reviews_seen=0,
                    )
                )
            log.info("%s#%d: requested review from %s", pr.repo, pr.number, member.key)
            return member.key
        log.warning("%s#%d: no team member could be requested", pr.repo, pr.number)
        return None

    # what is waiting

    def _items(self) -> list[Item]:
        """Waiting requests on open, non-draft pull requests (closed, merged, draft and reviewed
        ones drop out here)."""
        prs = self.store.ready_prs()
        urgent_label = self.settings.thresholds.urgent_label
        items: list[Item] = []
        for request in self.store.waiting():
            pr = prs.get((request.repo, request.number))
            if pr is None:
                continue
            if request.reviewer not in {login.lower() for login in pr.requested_reviewers}:
                continue
            items.append(Item(request=request, pr=pr, urgent=urgent_label in pr.labels))
        return items

    def queue(self) -> list[Item]:
        """The shared queue: waiting requests nobody has been told about yet this round."""
        return [item for item in self._items() if not item.request.notified]

    def should_send(self, queue: list[Item], now: datetime) -> bool:
        if not queue:
            return False
        limits = self.settings.thresholds
        if any(item.urgent for item in queue):
            return True
        times = [item.request.queued_at for item in queue]
        if now - min(times) >= limits.max_wait:
            return True
        prs = {(item.pr.repo, item.pr.number) for item in queue}
        return len(prs) >= limits.batch_min_prs and now - max(times) >= limits.quiet

    # sending

    def _send_marked(self, chunks: list[tuple[str, list[Item]]]) -> tuple[int, bool]:
        """Mark unnotified chunk items notified, then send each chunk.

        Returns ``(delivered_chunks, all_succeeded)``. Only a refusal by Telegram
        (nothing delivered for that chunk) unmarks the chunk's newly marked items;
        already-notified items and transport errors keep their marks.
        """
        delivered = 0
        for text, chunk_items in chunks:
            fresh = [i for i in chunk_items if not i.request.notified]
            keys: list[Key] = [
                (i.request.repo, i.request.number, i.request.reviewer) for i in fresh
            ]
            if keys:
                with self.store.transaction():
                    self.store.set_notified(keys, True)
            try:
                self.telegram.send_message(self.settings.chat_id, text)
            except TelegramRejected as error:
                if keys:
                    with self.store.transaction():
                        self.store.set_notified(keys, False)
                log.warning("message not sent, retry next poll: %s", error)
                return delivered, False
            except TelegramError as error:
                log.warning("message delivery unknown, not repeated: %s", error)
            delivered += 1
        return delivered, True

    def _batch(self, now: datetime) -> None:
        queue = self.queue()
        if not self.should_send(queue, now):
            return
        chunks = messages.batch_messages(queue, self.settings.team, self._aliases, now)
        _, ok = self._send_marked(chunks)
        if ok:
            log.info("batch sent: %d lines in %d message(s)", len(queue), len(chunks))

    def _digest(self, now: datetime) -> bool:
        """The evening digest, once a day in its window. True when it went out this tick."""
        limits = self.settings.thresholds
        local = now.astimezone(limits.digest_tz)
        start = datetime.combine(local.date(), limits.digest_at, tzinfo=limits.digest_tz)
        if not start <= local < start + limits.digest_window:
            return False
        today = local.date().isoformat()
        if self.store.get_meta(META_DIGEST_DATE) == today:
            return False
        items = self._items()
        if not items:
            with self.store.transaction():
                self.store.set_meta(META_DIGEST_DATE, today)
            log.info("digest: nothing waits, not sent")
            return False
        chunks = messages.digest_messages(
            items,
            self.settings.team,
            self._aliases,
            now,
            limits.digest_at.strftime("%H:%M"),
            limits.age_shown_after,
        )
        delivered, ok = self._send_marked(chunks)
        if delivered == 0 and not ok:
            return False
        with self.store.transaction():
            self.store.set_meta(META_DIGEST_DATE, today)
        if ok:
            log.info("digest sent: %d lines in %d message(s)", len(items), len(chunks))
        else:
            log.warning(
                "digest partially sent: %d/%d chunk(s), date marked to avoid repeats",
                delivered,
                len(chunks),
            )
        return True

    # Telegram commands

    def poll_commands(self, timeout: int) -> bool:
        """Read and answer commands; False when Telegram could not be read."""
        raw = self.store.get_meta(META_TELEGRAM_OFFSET)
        try:
            updates = self.telegram.get_updates(None if raw is None else int(raw), timeout)
        except TelegramError as error:
            log.warning("telegram updates not read: %s", error)
            return False
        self.handle_updates(updates)
        return True

    def handle_updates(self, updates: list[Update]) -> None:
        for update in updates:
            # The offset moves first: a command that fails is not answered twice.
            with self.store.transaction():
                self.store.set_meta(META_TELEGRAM_OFFSET, str(update.update_id + 1))
            if update.chat_id != self.settings.chat_id:
                continue
            command = update.text.strip().split(maxsplit=1)[0] if update.text.strip() else ""
            if command.split("@", 1)[0] != "/mine":
                continue
            try:
                self.telegram.send_message(
                    self.settings.chat_id, self.mine_text(update.username), update.message_id
                )
            except TelegramError as error:
                log.warning("/mine not answered: %s", error)

    def mine_text(self, username: str | None) -> str:
        member = None
        if username:
            wanted = username.lower().lstrip("@")
            member = next((m for m in self.settings.team if m.telegram.lower() == wanted), None)
        if member is None:
            return messages.unknown_sender_message()
        own = [item for item in self._items() if item.request.reviewer == member.key]
        return messages.mine_message(
            own, self._aliases, self.clock(), self.settings.thresholds.age_shown_after
        )
