"""Map GitHub diff read failures to their ``error_code`` (PIPELINE_SPEC §5.2)."""

from __future__ import annotations

from datetime import timedelta
from typing import Literal

import httpx

from app.modules.reviews.application.check_runs import RunProviderCheckpoint
from app.modules.reviews.application.run_failures import RETRY_AFTER_CEILING, RunFailure
from app.modules.reviews.application.vcs_diff import (
    CommitMessagePrefix,
    PullRequestLocator,
    VcsFile,
    VcsMetadataProvider,
    VcsPullRequest,
)

_MAX_RETRY_AFTER_SECONDS = str(timedelta.max // timedelta(seconds=1))


def _retry_after_delay(value: str | None) -> timedelta | None:
    """Ignore malformed values; a delay too large to represent waits the ceiling."""
    if not value or not value.isascii() or not value.isdecimal():
        return None
    # Strip leading zeros before bounding/converting, including >4,300-digit inputs.
    seconds = value.lstrip("0") or "0"
    if len(seconds) > len(_MAX_RETRY_AFTER_SECONDS) or (
        len(seconds) == len(_MAX_RETRY_AFTER_SECONDS) and seconds > _MAX_RETRY_AFTER_SECONDS
    ):
        return RETRY_AFTER_CEILING
    return timedelta(seconds=int(seconds))


def classify_vcs_error(
    exc: httpx.HTTPError,
    *,
    fetch_error_code: Literal[
        "diff_fetch_failed", "commit_messages_fetch_failed"
    ] = "diff_fetch_failed",
) -> RunFailure:
    if isinstance(exc, httpx.HTTPStatusError):
        response = exc.response
        delay = _retry_after_delay(response.headers.get("Retry-After"))
        limited = response.headers.get("X-RateLimit-Remaining") == "0" or delay is not None
        status = response.status_code
        if status == 404 or (status == 403 and not limited):
            return RunFailure("github_forbidden", f"GitHub answered {status}")
        return RunFailure(fetch_error_code, f"GitHub answered {status}", retry_after=delay)
    return RunFailure(fetch_error_code, f"GitHub request failed: {type(exc).__name__}")


class ClassifiedVcsProvider:
    """Decorate the diff reads of the attempt; blob reads stay best effort."""

    def __init__(self, inner: VcsMetadataProvider) -> None:
        self._inner = inner

    async def get_pull_request(
        self, locator: PullRequestLocator, *, checkpoint: RunProviderCheckpoint
    ) -> VcsPullRequest:
        try:
            return await self._inner.get_pull_request(locator, checkpoint=checkpoint)
        except httpx.HTTPError as exc:
            raise classify_vcs_error(exc) from exc

    async def get_commit_messages(
        self, locator: PullRequestLocator, *, checkpoint: RunProviderCheckpoint
    ) -> CommitMessagePrefix:
        try:
            return await self._inner.get_commit_messages(locator, checkpoint=checkpoint)
        except httpx.HTTPError as exc:
            raise classify_vcs_error(exc, fetch_error_code="commit_messages_fetch_failed") from None

    async def get_diff(
        self, pull_request: VcsPullRequest, *, checkpoint: RunProviderCheckpoint
    ) -> tuple[VcsFile, ...]:
        try:
            return await self._inner.get_diff(pull_request, checkpoint=checkpoint)
        except httpx.HTTPError as exc:
            raise classify_vcs_error(exc) from exc

    async def get_blob(
        self, locator: PullRequestLocator, sha: str, *, checkpoint: RunProviderCheckpoint
    ) -> bytes:
        return await self._inner.get_blob(locator, sha, checkpoint=checkpoint)
