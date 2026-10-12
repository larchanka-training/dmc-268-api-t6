"""GitLab merge-request discussions for review.publish (issue #97)."""

from __future__ import annotations

import hashlib
import re
from typing import Any

import httpx

from app.modules.reviews.application.publish_run_review import (
    PublishError,
    ReviewSubmission,
    SubmittedReview,
)
from app.modules.reviews.application.review_output import PublishedFinding, comment_body

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


def _detail(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return response.text[:500]
    if isinstance(payload, dict):
        return str(payload.get("message", ""))[:500]
    return str(payload)[:500]


def _classify(response: httpx.Response) -> PublishError:
    status = response.status_code
    detail = _detail(response)
    if status == 400 and _POSITION_REJECTION.search(detail.lower()) is not None:
        return PublishError("coordinates", detail, http_status=status)
    if status in (401, 403):
        return PublishError("forbidden", detail, http_status=status)
    return PublishError("retryable", detail, http_status=status)


def _decoded(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError as exc:
        raise PublishError("retryable", "GitLab response was not JSON") from exc


def _header_int(response: httpx.Response, name: str) -> int | None:
    raw = response.headers.get(name, "").strip()
    if not raw:
        return None
    if not raw.isdigit():
        raise PublishError("retryable", "GitLab discussion list is short")
    return int(raw)


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


def _position(version: dict[str, str], finding: PublishedFinding) -> dict[str, Any]:
    return {
        "position_type": "text",
        "base_sha": version["base_commit_sha"],
        "start_sha": version["start_commit_sha"],
        "head_sha": version["head_commit_sha"],
        "old_path": finding.path,
        "new_path": finding.path,
        "new_line": finding.line,
    }


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

    def __init__(self, *, client: httpx.AsyncClient, token: str) -> None:
        self._client = client
        self._token = token

    async def submit_review(
        self, submission: ReviewSubmission, *, project_id: int
    ) -> SubmittedReview:
        prefix = f"/api/v4/projects/{project_id}/merge_requests/{submission.pr_number}"
        try:
            version = _matching_version(
                _decoded(await self._send("GET", f"{prefix}/versions")),
                submission.commit_sha,
            )
            discussions = await self._discussions(f"{prefix}/discussions")
            summary = _summary_marker(submission.findings_hash)
            existing = _note_id_for_marker(discussions, summary)
            if existing is not None:
                return SubmittedReview(existing, ())
            pending = list(submission.findings)
            for finding in submission.findings:
                marker = _inline_marker(submission.findings_hash, finding)
                if _note_id_for_marker(discussions, marker) is not None:
                    pending.pop(0)
                    continue
                comment = comment_body(finding.title, finding.body, finding.suggestion)
                try:
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
            summary_id = await self._create(
                f"{prefix}/discussions",
                body=f"{submission.body}\n\n{summary}",
                position=None,
            )
            return SubmittedReview(summary_id, ())
        except httpx.TransportError as exc:
            raise PublishError("retryable", f"GitLab request failed: {exc}") from exc

    async def _require_current_head(self, prefix: str, head_sha: str) -> None:
        """A position 400 is coordinates only while the latest version is still this head."""
        latest = _latest_head_sha(_decoded(await self._send("GET", f"{prefix}/versions")))
        if latest != head_sha:
            raise PublishError("stale_commit", f"merge request head moved from {head_sha}")

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
            next_page = response.headers.get("X-Next-Page", "").strip()
            if not next_page:
                if total is not None and len(collected) != total:
                    raise PublishError("retryable", "GitLab discussion list is short")
                return collected
            if not next_page.isdigit():
                raise PublishError("retryable", "GitLab discussion list is short")
            next_page_number = int(next_page)
            if next_page_number <= page:
                raise PublishError("retryable", "GitLab discussion list is short")
            page = next_page_number
        raise PublishError("retryable", "GitLab discussion list is short")

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
    ) -> httpx.Response:
        kwargs: dict[str, Any] = {"headers": {"PRIVATE-TOKEN": self._token}, "timeout": _TIMEOUT}
        if params is not None:
            kwargs["params"] = params
        if json_body is not None:
            kwargs["json"] = json_body
        response = await self._client.request(method, url, **kwargs)
        if response.is_error:
            raise _classify(response)
        return response
