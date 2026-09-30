"""Map GitHub diff read failures to their ``error_code`` (PIPELINE_SPEC §5.2)."""

from __future__ import annotations

from datetime import timedelta

import httpx

from app.modules.reviews.application.run_failures import RunFailure
from app.modules.reviews.application.vcs_diff import (
    PullRequestLocator,
    VcsFile,
    VcsProvider,
    VcsPullRequest,
)


def classify_vcs_error(exc: httpx.HTTPError) -> RunFailure:
    if isinstance(exc, httpx.HTTPStatusError):
        response = exc.response
        retry_after = response.headers.get("Retry-After")
        delay = (
            timedelta(seconds=int(retry_after)) if retry_after and retry_after.isdigit() else None
        )
        limited = response.headers.get("X-RateLimit-Remaining") == "0" or delay is not None
        status = response.status_code
        if status == 404 or (status == 403 and not limited):
            return RunFailure("github_forbidden", f"GitHub answered {status}")
        return RunFailure("diff_fetch_failed", f"GitHub answered {status}", retry_after=delay)
    return RunFailure("diff_fetch_failed", f"GitHub request failed: {type(exc).__name__}")


class ClassifiedVcsProvider:
    """Decorate the diff reads of the attempt; blob reads stay best effort."""

    def __init__(self, inner: VcsProvider) -> None:
        self._inner = inner

    async def get_pull_request(self, locator: PullRequestLocator) -> VcsPullRequest:
        try:
            return await self._inner.get_pull_request(locator)
        except httpx.HTTPError as exc:
            raise classify_vcs_error(exc) from exc

    async def get_diff(self, pull_request: VcsPullRequest) -> tuple[VcsFile, ...]:
        try:
            return await self._inner.get_diff(pull_request)
        except httpx.HTTPError as exc:
            raise classify_vcs_error(exc) from exc

    async def get_blob(self, locator: PullRequestLocator, sha: str) -> bytes:
        return await self._inner.get_blob(locator, sha)
