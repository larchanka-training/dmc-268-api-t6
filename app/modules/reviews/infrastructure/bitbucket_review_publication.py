"""Idempotent Bitbucket Cloud pull-request comments for one pinned head."""

from __future__ import annotations

import hashlib
import re
from typing import Protocol
from uuid import UUID

from app.modules.integrations.bitbucket.infrastructure.commit_hash import (
    pr_commit_hash,
    resolved_commit_hash,
)
from app.modules.reviews.application.diff_map import is_supported_path
from app.modules.reviews.application.publish_run_review import (
    PublishError,
    ReviewSubmission,
    SubmittedReview,
)
from app.modules.reviews.application.review_output import PublishedFinding
from app.modules.reviews.application.run_failures import RunCancelled

_MAX_PAGES = 20
_MAX_COMMENTS = 2000


class BitbucketCommentPort(Protocol):
    async def checkpoint(self) -> None: ...

    async def pull_request(self) -> object: ...

    async def source_commit(self, repository_uuid: UUID, head_sha: str) -> object: ...

    async def list_comments(self, cursor: str | None) -> object: ...

    async def post_comment(self, payload: dict[str, object]) -> object: ...

    async def update_comment(self, comment_id: int, payload: dict[str, object]) -> object: ...


def _uuid(value: object) -> UUID:
    if not isinstance(value, str):
        raise PublishError("retryable", "Bitbucket PR identity is malformed")
    try:
        return UUID(value.strip("{}"))
    except ValueError:
        raise PublishError("retryable", "Bitbucket PR identity is malformed") from None


def _mapping(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise PublishError("retryable", "Bitbucket publication response is malformed")
    return value


def _comment_id(value: object) -> int:
    data = _mapping(value)
    comment_id = data.get("id")
    if type(comment_id) is not int or comment_id <= 0:
        raise PublishError("retryable", "Bitbucket comment response has no id")
    return comment_id


def _raw_comment(value: object) -> str | None:
    data = _mapping(value)
    if data.get("deleted") is True or data.get("pending") is True or data.get("parent"):
        return None
    content = _mapping(data.get("content"))
    raw = content.get("raw")
    if not isinstance(raw, str):
        raise PublishError("retryable", "Bitbucket comment list is malformed")
    return raw


def _fence(suggestion: str, *, suggestion_syntax: bool) -> str:
    longest = max((len(match.group()) for match in re.finditer(r"`+", suggestion)), default=0)
    # Use at least three ticks; a longer fence keeps nested markdown from closing it.
    ticks = "`" * max(3, longest + 3)
    language = "suggestion" if suggestion_syntax else ""
    return f"{ticks}{language}\n{suggestion}\n{ticks}"


def _comment_body(finding: PublishedFinding) -> str:
    body = f"**{finding.title}**\n\n{finding.body}"
    if finding.suggestion:
        body += "\n\nSuggested patch to apply manually:\n" + _fence(
            finding.suggestion, suggestion_syntax=finding.start_line is None
        )
    return body


class BitbucketPullRequestCommentGateway:
    """Uses durable comment markers to recover partial/ambiguous POST outcomes."""

    def __init__(self, port: BitbucketCommentPort, *, run_id: UUID, repository_uuid: UUID) -> None:
        self._port = port
        self._run_id = run_id
        self._repository_uuid = repository_uuid
        self._source_uuid: UUID | None = None

    async def checkpoint(self) -> None:
        await self._port.checkpoint()

    def _marker(self, submission: ReviewSubmission, finding: PublishedFinding | None) -> str:
        if finding is None:
            suffix = "summary"
        else:
            fingerprint = hashlib.sha256(
                f"{finding.path}\0{finding.line}\0{finding.start_line}\0"
                f"{finding.bitbucket_old_line}\0{_comment_body(finding)}".encode()
            ).hexdigest()[:16]
            suffix = f"inline={fingerprint}"
        return f"<!-- ai-review head={submission.commit_sha} {suffix} -->"

    async def _current(self, submission: ReviewSubmission) -> None:
        await self._port.checkpoint()
        data = _mapping(await self._port.pull_request())
        if data.get("id") != submission.pr_number:
            raise PublishError("forbidden", "Bitbucket PR identity changed")
        if data.get("state") != "OPEN":
            raise RunCancelled("pr_closed")
        source = _mapping(data.get("source"))
        destination = _mapping(data.get("destination"))
        destination_uuid = _uuid(_mapping(destination.get("repository")).get("uuid"))
        source_uuid = _uuid(_mapping(source.get("repository")).get("uuid"))
        if destination_uuid != self._repository_uuid:
            raise PublishError("forbidden", "Bitbucket PR destination identity changed")
        if self._source_uuid is not None and source_uuid != self._source_uuid:
            raise PublishError("stale_commit", "Bitbucket PR source moved")
        self._source_uuid = source_uuid
        try:
            prefix = pr_commit_hash(_mapping(source.get("commit")).get("hash"))
        except ValueError:
            raise PublishError("retryable", "Bitbucket PR head is malformed") from None
        if len(prefix) == 12 or source_uuid != self._repository_uuid:
            source_commit = _mapping(await self._port.source_commit(source_uuid, prefix))
            try:
                head = resolved_commit_hash(source_commit.get("hash"), prefix)
            except ValueError:
                raise PublishError("retryable", "Bitbucket fork source is unavailable") from None
        else:
            head = prefix
        if head != submission.commit_sha:
            raise PublishError("stale_commit", "Bitbucket PR head moved")
        await self._port.checkpoint()

    async def _comments(self) -> list[dict[str, object]]:
        collected: list[dict[str, object]] = []
        cursor: str | None = None
        seen: set[str] = set()
        for _ in range(_MAX_PAGES):
            page = _mapping(await self._port.list_comments(cursor))
            values = page.get("values")
            if not isinstance(values, list) or not all(isinstance(item, dict) for item in values):
                raise PublishError("retryable", "Bitbucket comment pagination is malformed")
            collected.extend(values)
            if len(collected) > _MAX_COMMENTS:
                raise PublishError("retryable", "Bitbucket comment pagination limit exceeded")
            next_cursor = page.get("next")
            if next_cursor is None:
                size = page.get("size")
                if type(size) is int and size != len(collected):
                    raise PublishError("retryable", "Bitbucket comment pagination is incomplete")
                return collected
            if (
                not isinstance(next_cursor, str)
                or not next_cursor
                or next_cursor in seen
                or next_cursor == cursor
            ):
                raise PublishError("retryable", "Bitbucket comment pagination is incomplete")
            seen.add(next_cursor)
            cursor = next_cursor
        raise PublishError("retryable", "Bitbucket comment pagination limit exceeded")

    @staticmethod
    def _existing(comments: list[dict[str, object]], marker: str) -> tuple[int, str] | None:
        for comment in comments:
            raw = _raw_comment(comment)
            if raw is not None and marker in raw:
                return _comment_id(comment), raw
        return None

    async def _post(
        self, submission: ReviewSubmission, *, body: str, inline: dict[str, object] | None
    ) -> int:
        await self._current(submission)
        payload: dict[str, object] = {"content": {"raw": body}}
        if inline is not None:
            payload["inline"] = inline
        await self._port.checkpoint()
        return _comment_id(await self._port.post_comment(payload))

    async def _update_summary(
        self, submission: ReviewSubmission, *, comment_id: int, body: str
    ) -> None:
        await self._current(submission)
        await self._port.checkpoint()
        updated_id = _comment_id(
            await self._port.update_comment(comment_id, {"content": {"raw": body}})
        )
        if updated_id != comment_id:
            raise PublishError("retryable", "Bitbucket summary update changed comment identity")

    async def submit_review(self, submission: ReviewSubmission) -> SubmittedReview:
        await self._current(submission)
        comments = await self._comments()
        posted: set[UUID] = set()
        for index, finding in enumerate(submission.findings):
            marker = self._marker(submission, finding)
            if self._existing(comments, marker) is not None:
                if finding.finding_id is not None:
                    posted.add(finding.finding_id)
                continue
            if (
                not is_supported_path(finding.path)
                or finding.line <= 0
                or finding.start_line is not None
                and not 0 < finding.start_line < finding.line
                or not finding.bitbucket_anchor_valid
                or finding.bitbucket_old_line is not None
                and finding.bitbucket_old_line <= 0
            ):
                raise PublishError(
                    "coordinates",
                    "Bitbucket review coordinates are invalid",
                    unposted=submission.findings[index:],
                )
            inline: dict[str, object] = {"path": finding.path}
            if finding.bitbucket_old_line is not None:
                inline["from"] = finding.bitbucket_old_line
                if finding.start_line is not None:
                    inline["start_from"] = finding.start_line
            else:
                inline["to"] = finding.line
                if finding.start_line is not None:
                    inline["start_to"] = finding.start_line
            try:
                comment_id = await self._post(
                    submission,
                    body=f"{_comment_body(finding)}\n\n{marker}",
                    inline=inline,
                )
            except PublishError as exc:
                if exc.kind == "coordinates":
                    raise PublishError(
                        "coordinates",
                        str(exc),
                        http_status=exc.http_status,
                        unposted=submission.findings[index:],
                    ) from exc
                raise
            comments.append({"id": comment_id, "content": {"raw": marker}})
            if finding.finding_id is not None:
                posted.add(finding.finding_id)
        summary_marker = self._marker(submission, None)
        summary_body = f"{submission.body}\n\n{summary_marker}"
        existing_summary = self._existing(comments, summary_marker)
        if existing_summary is None:
            summary_id = await self._post(submission, body=summary_body, inline=None)
        else:
            summary_id, existing_body = existing_summary
            if existing_body != summary_body:
                await self._update_summary(submission, comment_id=summary_id, body=summary_body)
        return SubmittedReview(summary_id, (), posted_inline_finding_ids=frozenset(posted))
