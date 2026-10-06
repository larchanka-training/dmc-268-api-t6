"""GitHub App adapters for the bot check-run and the pull request review (SD §8.3)."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import httpx

from app.common.infrastructure.github_repository_path import (
    commit_sha_segment,
    repository_path_from_full_name,
)
from app.modules.reviews.application.check_runs import CheckRunTarget, CheckRunView
from app.modules.reviews.application.publish_run_review import (
    GitHubPublishError,
    ReviewSubmission,
    SubmittedReview,
)
from app.modules.reviews.infrastructure.github_vcs import InstallationTokenProvider

CHECK_RUN_NAME = "AI Review"
_REQUEST_TIMEOUT = 10.0
_API_VERSION = "2022-11-28"


def _headers(token: str) -> dict[str, str]:
    return {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": _API_VERSION,
    }


def _marker(findings_hash: str) -> str:
    """Hidden body marker that makes a repeated publication find the posted review (Р-5)."""
    return f"<!-- ai-review findings_hash={findings_hash} -->"


class GitHubCheckRunGateway:
    def __init__(
        self, *, client: httpx.AsyncClient, token_provider: InstallationTokenProvider
    ) -> None:
        self._client = client
        self._tokens = token_provider

    async def upsert(self, target: CheckRunTarget, view: CheckRunView) -> None:
        prefix = repository_path_from_full_name(target.repository_full_name)
        commit_check_runs = f"{prefix}/commits/{commit_sha_segment(target.head_sha)}/check-runs"
        headers = _headers(await self._tokens.get_installation_access_token(target.installation_id))
        payload: dict[str, Any] = {
            "status": view.status,
            "output": {"title": view.title, "summary": view.summary},
        }
        if view.conclusion is not None:
            payload["conclusion"] = view.conclusion
        existing = await self._find(commit_check_runs, headers, target)
        if existing is None:
            payload |= {
                "name": CHECK_RUN_NAME,
                "head_sha": target.head_sha,
                "external_id": str(target.run_id),
            }
            response = await self._client.post(
                f"{prefix}/check-runs", json=payload, headers=headers, timeout=_REQUEST_TIMEOUT
            )
        else:
            response = await self._client.patch(
                f"{prefix}/check-runs/{existing}",
                json=payload,
                headers=headers,
                timeout=_REQUEST_TIMEOUT,
            )
        response.raise_for_status()

    async def _find(self, path: str, headers: dict[str, str], target: CheckRunTarget) -> int | None:
        response = await self._client.get(
            path,
            params={"check_name": CHECK_RUN_NAME, "filter": "all", "per_page": 100},
            headers=headers,
            timeout=_REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        for item in response.json().get("check_runs", []):
            if item.get("external_id") == str(target.run_id):
                return int(item["id"])
        return None


def _publish_error(response: httpx.Response) -> GitHubPublishError:
    status = response.status_code
    try:
        message = str(response.json().get("message", ""))
        errors = str(response.json().get("errors", ""))
    except ValueError:
        message, errors = response.text[:500], ""
    detail = f"{message} {errors}".strip()
    retry_after = response.headers.get("Retry-After")
    delay = timedelta(seconds=int(retry_after)) if retry_after and retry_after.isdigit() else None
    rate_limited = response.headers.get("X-RateLimit-Remaining") == "0" or delay is not None
    if status == 422:
        lowered = detail.lower()
        if "commit_id" in lowered or "not part of the pull request" in lowered:
            return GitHubPublishError("stale_commit", detail, http_status=status)
        return GitHubPublishError("coordinates", detail, http_status=status)
    if status == 404 or (status == 403 and not rate_limited):
        return GitHubPublishError("forbidden", detail, http_status=status)
    return GitHubPublishError("retryable", detail, http_status=status, retry_after=delay)


class GitHubPullRequestReviewGateway:
    def __init__(
        self, *, client: httpx.AsyncClient, token_provider: InstallationTokenProvider
    ) -> None:
        self._client = client
        self._tokens = token_provider

    async def submit_review(self, submission: ReviewSubmission) -> SubmittedReview:
        repository_path = repository_path_from_full_name(submission.repository_full_name)
        prefix = f"{repository_path}/pulls/{submission.pr_number}"
        try:
            headers = _headers(
                await self._tokens.get_installation_access_token(submission.installation_id)
            )
            review_id = await self._find(prefix, headers, submission.findings_hash)
            if review_id is None:
                await self._ensure_current_head(prefix, headers, submission.commit_sha)
                review_id = await self._post(prefix, headers, submission)
            # A failed read raises: the retry finds the posted review by its marker.
            return await self._with_comments(prefix, headers, review_id)
        except httpx.TransportError as exc:
            raise GitHubPublishError("retryable", f"GitHub request failed: {exc}") from exc

    async def _ensure_current_head(
        self, prefix: str, headers: dict[str, str], commit_sha: str
    ) -> None:
        """GitHub, not PostgreSQL, knows a push that the webhook worker has not projected yet."""
        response = await self._client.get(prefix, headers=headers, timeout=_REQUEST_TIMEOUT)
        if response.is_error:
            raise _publish_error(response)
        head = response.json().get("head", {}).get("sha")
        if head != commit_sha:
            raise GitHubPublishError("stale_commit", f"pull request head moved to {head}")

    async def _post(
        self, prefix: str, headers: dict[str, str], submission: ReviewSubmission
    ) -> int:
        response = await self._client.post(
            f"{prefix}/reviews",
            json={
                "commit_id": submission.commit_sha,
                "event": submission.event,
                "body": f"{submission.body}\n\n{_marker(submission.findings_hash)}",
                "comments": [
                    {
                        "path": item.path,
                        "line": item.line,
                        "side": "RIGHT",
                        **({"start_line": item.start_line} if item.start_line else {}),
                        "body": _comment_body(item.title, item.body, item.suggestion),
                    }
                    for item in submission.findings
                ],
            },
            headers=headers,
            timeout=_REQUEST_TIMEOUT,
        )
        if response.is_error:
            raise _publish_error(response)
        return int(response.json()["id"])

    async def _find(self, prefix: str, headers: dict[str, str], findings_hash: str) -> int | None:
        response = await self._client.get(
            f"{prefix}/reviews",
            params={"per_page": 100},
            headers=headers,
            timeout=_REQUEST_TIMEOUT,
        )
        if response.is_error:
            raise _publish_error(response)
        marker = _marker(findings_hash)
        for item in response.json():
            if marker in (item.get("body") or ""):
                return int(item["id"])
        return None

    async def _with_comments(
        self, prefix: str, headers: dict[str, str], review_id: int
    ) -> SubmittedReview:
        response = await self._client.get(
            f"{prefix}/reviews/{review_id}/comments",
            params={"per_page": 100},
            headers=headers,
            timeout=_REQUEST_TIMEOUT,
        )
        if response.is_error:
            raise _publish_error(response)
        return SubmittedReview(review_id, tuple(int(item["id"]) for item in response.json()))


def _comment_body(title: str, body: str, suggestion: str | None) -> str:
    text = f"**{title}**\n\n{body}"
    if suggestion:
        text += f"\n\n```suggestion\n{suggestion}\n```"
    return text
