"""GitLab merge-request discussions for review.publish (issue #97)."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any, Protocol

import httpx

from app.modules.integrations.gitlab.infrastructure.run_http import (
    GitLabPublicationResponse,
    GitLabRunHttpError,
    JsonValue,
)
from uuid import UUID

from app.modules.reviews.application.publication_coordinates import gitlab_text_position
from app.modules.reviews.application.publish_run_review import (
    PublishError,
    ReviewSubmission,
    SubmittedReview,
)
from app.modules.reviews.application.review_output import (
    PublishedFinding,
    comment_body,
    gitlab_comment_body,
)

_TIMEOUT = 10.0
_MAX_DISCUSSION_PAGES = 20
_PER_PAGE = 100
_POSITION_REJECTION = re.compile(r"\b(?:line_code|line|position|diff refs)\b")


def _summary_marker(findings_hash: str) -> str:
    return f"<!-- ai-review findings_hash={findings_hash} summary -->"


def _finding_id(finding: PublishedFinding) -> str:
    """Identity of one inline note. Path and line alone collide on one line."""
    return hashlib.sha256(
        comment_body(finding.title, finding.body, finding.suggestion).encode()
    ).hexdigest()[:16]


def _inline_marker(findings_hash: str, finding: PublishedFinding) -> str:
    return (
        f"<!-- ai-review findings_hash={findings_hash} "
        f"inline={finding.path}:{finding.line} finding={_finding_id(finding)} -->"
    )


def _classify(response: GitLabPublicationResponse) -> PublishError:
    status = response.status_code
    coordinates = _POSITION_REJECTION.search(
        response.body.decode("utf-8", errors="replace").lower()
    ) is not None
    if status in (400, 422) and coordinates:
        return PublishError("coordinates", "GitLab rejected review coordinates", http_status=status)
    if status in (401, 403, 404, 409, 422):
        return PublishError("forbidden", "GitLab publication forbidden", http_status=status)
    return PublishError(
        "retryable", "GitLab publication request failed", http_status=status,
        retry_after=response.retry_after,
    )


def _decoded(response: GitLabPublicationResponse) -> Any:
    try:
        return response.decode_json()
    except GitLabRunHttpError:
        raise PublishError("retryable", "GitLab response was not JSON") from None


def _header_int(response: GitLabPublicationResponse, name: str) -> int | None:
    raw = response.headers.get(name.lower(), "").strip()
    if not raw:
        return None
    if not raw.isascii() or not raw.isdigit() or len(raw) > 19:
        raise PublishError("retryable", "GitLab discussion list is short")
    return int(raw)


class GitLabPublicationRequestPort(Protocol):
    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, int] | None = None,
        json_body: Mapping[str, JsonValue] | None = None,
    ) -> GitLabPublicationResponse: ...


def _matching_version(payload: Any, head_sha: str) -> dict[str, str]:
    if not isinstance(payload, list):
        raise PublishError("retryable", "GitLab version list is short")
    for item in payload:
        if not isinstance(item, dict) or item.get("head_commit_sha") != head_sha:
            continue
        base = item.get("base_commit_sha")
        start = item.get("start_commit_sha")
        head = item.get("head_commit_sha")
        if (
            isinstance(base, str)
            and base
            and isinstance(start, str)
            and start
            and isinstance(head, str)
            and head
        ):
            return {
                "base_commit_sha": base,
                "start_commit_sha": start,
                "head_commit_sha": head,
            }
        raise PublishError("retryable", "GitLab diff version is missing commit SHAs")
    raise PublishError("stale_commit", f"merge request head moved from {head_sha}")


def _latest_head_sha(payload: Any) -> str:
    """GitLab returns the current diff version first and keeps older versions after it."""
    if not isinstance(payload, list) or not payload or not isinstance(payload[0], dict):
        raise PublishError("retryable", "GitLab version list is short")
    head = payload[0].get("head_commit_sha")
    if not isinstance(head, str) or not head:
        raise PublishError("retryable", "GitLab diff version is missing commit SHAs")
    return head


def _require_latest_head(payload: Any, head_sha: str) -> None:
    """A historical versions row supplies diff SHAs only while it is still the MR head."""
    if _latest_head_sha(payload) != head_sha:
        raise PublishError("stale_commit", f"merge request head moved from {head_sha}")


def _position(version: dict[str, str], finding: PublishedFinding) -> dict[str, Any]:
    return gitlab_text_position(version, finding)


def _note_bodies(discussions: list[dict[str, Any]]) -> list[tuple[int | None, str]]:
    notes: list[tuple[int | None, str]] = []
    for discussion in discussions:
        raw_notes = discussion.get("notes")
        if not isinstance(raw_notes, list):
            continue
        for note in raw_notes:
            if not isinstance(note, dict):
                continue
            note_id = note.get("id")
            body = str(note.get("body") or "")
            notes.append((note_id if isinstance(note_id, int) else None, body))
    return notes


def _posted_inline_finding_ids(
    discussions: list[dict[str, Any]],
    findings_hash: str,
    findings: tuple[PublishedFinding, ...],
) -> frozenset[UUID]:
    posted: set[UUID] = set()
    for finding in findings:
        if finding.finding_id is None:
            continue
        marker = _inline_marker(findings_hash, finding)
        if _note_id_for_marker(discussions, marker) is not None:
            posted.add(finding.finding_id)
    return frozenset(posted)


def _note_id_for_marker(discussions: list[dict[str, Any]], marker: str) -> int | None:
    found = False
    for note_id, body in _note_bodies(discussions):
        if marker not in body:
            continue
        found = True
        if note_id is not None:
            return note_id
    if found:
        raise PublishError("retryable", "GitLab discussion list is short")
    return None


def _created_note_id(payload: Any) -> int:
    if isinstance(payload, dict):
        notes = payload.get("notes")
        if isinstance(notes, list) and notes and isinstance(notes[0], dict):
            note_id = notes[0].get("id")
            if isinstance(note_id, int):
                return note_id
    raise PublishError("retryable", "GitLab discussion response has no note id")


class GitLabMergeRequestDiscussionGateway:
    """Posts new-side inline discussions and one summary, skipping existing hash markers."""

    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        token: str | None = None,
        requests: GitLabPublicationRequestPort | None = None,
    ) -> None:
        if (requests is None and (client is None or token is None)) or (
            requests is not None and (client is not None or token is not None)
        ):
            raise ValueError("Configure exactly one GitLab publication request source")
        self._client = client
        self._token = token
        self._requests = requests

    async def submit_review(
        self, submission: ReviewSubmission, *, project_id: int
    ) -> SubmittedReview:
        prefix = f"/api/v4/projects/{project_id}/merge_requests/{submission.pr_number}"
        try:
            version = await self._load_version(prefix, submission.commit_sha)
            discussions = await self._discussions(f"{prefix}/discussions")
            summary = _summary_marker(submission.findings_hash)
            existing = _note_id_for_marker(discussions, summary)
            if existing is not None:
                posted: frozenset[UUID] | None = None
                if any(item.finding_id is not None for item in submission.findings):
                    posted = _posted_inline_finding_ids(
                        discussions, submission.findings_hash, submission.findings
                    )
                return SubmittedReview(existing, (), posted_inline_finding_ids=posted)
            pending = list(submission.findings)
            for finding in submission.findings:
                marker = _inline_marker(submission.findings_hash, finding)
                if _note_id_for_marker(discussions, marker) is not None:
                    pending.pop(0)
                    continue
                comment = gitlab_comment_body(
                    finding.title,
                    finding.body,
                    finding.suggestion,
                    start_line=finding.start_line,
                    line=finding.line,
                )
                try:
                    version = await self._load_version(prefix, submission.commit_sha)
                    await self._create(
                        f"{prefix}/discussions",
                        body=f"{comment}\n\n{marker}",
                        position=_position(version, finding),
                    )
                except PublishError as exc:
                    if exc.kind != "coordinates":
                        raise
                    await self._require_current_head(prefix, submission.commit_sha)
                    raise PublishError(
                        "coordinates",
                        str(exc),
                        http_status=exc.http_status,
                        unposted=tuple(pending),
                    ) from exc
                pending.pop(0)
            await self._load_version(prefix, submission.commit_sha)
            summary_id = await self._create(
                f"{prefix}/discussions",
                body=f"{submission.body}\n\n{summary}",
                position=None,
            )
            posted_inline: frozenset[UUID] | None = None
            if any(finding.finding_id is not None for finding in submission.findings):
                posted_inline = frozenset(
                    finding.finding_id
                    for finding in submission.findings
                    if finding.finding_id is not None
                )
            return SubmittedReview(summary_id, (), posted_inline_finding_ids=posted_inline)
        except httpx.TransportError:
            raise PublishError("retryable", "GitLab publication request failed") from None

    async def _load_version(self, prefix: str, head_sha: str) -> dict[str, str]:
        """Refresh MR versions and fail fast when the run head is no longer current."""
        versions = _decoded(await self._send("GET", f"{prefix}/versions"))
        _require_latest_head(versions, head_sha)
        return _matching_version(versions, head_sha)

    async def _require_current_head(self, prefix: str, head_sha: str) -> None:
        """A position 400 is coordinates only while the latest version is still this head."""
        await self._load_version(prefix, head_sha)

    async def _discussions(self, url: str) -> list[dict[str, Any]]:
        collected: list[dict[str, Any]] = []
        page = 1
        total: int | None = None
        for _ in range(_MAX_DISCUSSION_PAGES):
            response = await self._send("GET", url, params={"per_page": _PER_PAGE, "page": page})
            payload = _decoded(response)
            if not isinstance(payload, list) or not all(isinstance(item, dict) for item in payload):
                raise PublishError("retryable", "GitLab discussion list is short")
            collected.extend(payload)
            page_total = _header_int(response, "X-Total")
            if page_total is not None:
                total = page_total
            next_page = response.headers.get("x-next-page", "").strip()
            if not next_page:
                if total is not None and len(collected) != total:
                    raise PublishError("retryable", "GitLab discussion list is short")
                return collected
            if not next_page.isascii() or not next_page.isdigit() or len(next_page) > 19:
                raise PublishError("retryable", "GitLab discussion list is short")
            next_page_number = int(next_page)
            if next_page_number <= page:
                raise PublishError("retryable", "GitLab discussion list is short")
            page = next_page_number
        raise PublishError("forbidden", "GitLab discussion pagination limit exceeded")

    async def _create(self, url: str, *, body: str, position: dict[str, Any] | None) -> int:
        payload: dict[str, Any] = {"body": body}
        if position is not None:
            payload["position"] = position
        response = await self._send("POST", url, json_body=payload)
        return _created_note_id(_decoded(response))

    async def _send(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, int] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> GitLabPublicationResponse:
        if self._requests is not None:
            response = await self._requests.request(method, url, params=params, json_body=json_body)
        else:
            assert self._client is not None and self._token is not None
            kwargs: dict[str, Any] = {
                "headers": {"PRIVATE-TOKEN": self._token},
                "timeout": _TIMEOUT,
            }
            if params is not None:
                kwargs["params"] = params
            if json_body is not None:
                kwargs["json"] = json_body
            raw = await self._client.request(method, url, **kwargs)
            if len(raw.content) > 8388608:
                raise PublishError("retryable", "GitLab publication response is too large")
            response = GitLabPublicationResponse(
                raw.status_code,
                raw.content,
                MappingProxyType(dict(raw.headers)),
            )
        if response.status_code >= 300:
            raise _classify(response) from None
        return response
