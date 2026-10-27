"""Map GitHub diff read failures to their ``error_code`` (PIPELINE_SPEC §5.2).

The rate-limit evidence of a failed GitHub answer is read here for the policy reads too.
"""

from __future__ import annotations

import contextlib
import json
from datetime import timedelta
from typing import Literal

import httpx

from app.modules.reviews.application.check_runs import RunProviderCheckpoint
from app.modules.reviews.application.run_failures import RunFailure, parse_retry_after
from app.modules.reviews.application.vcs_diff import (
    CommitMessagePrefix,
    PullRequestLocator,
    VcsFile,
    VcsMetadataProvider,
    VcsPullRequest,
)

# At most this much of a failed answer is read: a secondary rate limit names itself in a
# short JSON message, and an error is no reason to download more.
MAX_ERROR_BODY_BYTES = 4096
# GitHub asks to wait at least a minute after a secondary rate limit that sends neither
# Retry-After nor X-RateLimit-Remaining: 0.
SECONDARY_RATE_LIMIT_DELAY = timedelta(seconds=60)
# GitHub documents that a secondary rate limit answers 403 or 429 with a message saying so,
# not the exact text: only this phrase is looked for, in any case.
_SECONDARY_RATE_LIMIT = "secondary rate limit"


class StreamedStatusError(httpx.HTTPStatusError):
    """A failed streamed read, with the start of its body read while the stream was open.

    The stream is closed by the time a classifier sees it, so ``body`` is all there is.
    """

    def __init__(self, error: httpx.HTTPStatusError, body: bytes) -> None:
        super().__init__(str(error), request=error.request, response=error.response)
        self.body = body


async def raise_for_streamed_status(response: httpx.Response) -> None:
    """``raise_for_status`` of a streamed answer that keeps at most 4 KiB of its body."""
    if response.is_success:
        return
    body = await _error_body(response)
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise StreamedStatusError(exc, body) from None


async def _error_body(response: httpx.Response) -> bytes:
    if response.headers.get("Content-Encoding", "").strip().casefold() not in {"", "identity"}:
        # Never decoded: a decoder could expand one chunk far past the cap. The policy and
        # commit streams ask for identity; get_repository_blob keeps the client default.
        return b""
    body = bytearray()
    # The status and the headers are the answer; a body cut short only lacks the marker.
    with contextlib.suppress(httpx.HTTPError):
        async for chunk in response.aiter_bytes():
            body.extend(chunk[: MAX_ERROR_BODY_BYTES - len(body)])
            if len(body) >= MAX_ERROR_BODY_BYTES:
                break
    return bytes(body)


def plain_error_body(exc: httpx.HTTPError) -> bytes:
    """The body of a failed plain request, which httpx read whole before it raised."""
    # CONTRACT: called only around plain (unstreamed) requests; a stream raises a
    # StreamedStatusError, whose response is never read.
    if isinstance(exc, httpx.HTTPStatusError) and not isinstance(exc, StreamedStatusError):
        return exc.response.content
    return b""


def github_rate_limit(
    exc: httpx.HTTPStatusError, *, body: bytes = b""
) -> tuple[bool, timedelta | None]:
    """Whether a failed GitHub answer is a rate limit, and the wait it asks for.

    ``Retry-After`` (seconds or an HTTP-date) and ``X-RateLimit-Remaining: 0`` decide first.
    Without them, a 403 or 429 whose JSON ``message`` names a secondary rate limit is one,
    with ``SECONDARY_RATE_LIMIT_DELAY``. ``body`` is that of a plain request; a stream hands
    its own with a ``StreamedStatusError``.
    """
    response = exc.response
    delay = parse_retry_after(
        response.headers.get("Retry-After"), date=response.headers.get("Date")
    )
    # A date already past is a 0 s delay and still a limit, so test for None.
    if response.headers.get("X-RateLimit-Remaining") == "0" or delay is not None:
        return True, delay
    if isinstance(exc, StreamedStatusError):
        body = exc.body
    if response.status_code in {403, 429} and _names_secondary_rate_limit(body):
        return True, SECONDARY_RATE_LIMIT_DELAY
    return False, None


def _names_secondary_rate_limit(body: bytes) -> bool:
    try:
        payload = json.loads(body[:MAX_ERROR_BODY_BYTES])
    except ValueError:
        # Not JSON, or cut at the cap: no message to read.
        return False
    message = payload.get("message") if isinstance(payload, dict) else None
    return isinstance(message, str) and _SECONDARY_RATE_LIMIT in message.casefold()


def classify_vcs_error(
    exc: httpx.HTTPError,
    *,
    fetch_error_code: Literal[
        "diff_fetch_failed", "commit_messages_fetch_failed"
    ] = "diff_fetch_failed",
    body: bytes = b"",
) -> RunFailure:
    if isinstance(exc, httpx.HTTPStatusError):
        limited, delay = github_rate_limit(exc, body=body)
        status = exc.response.status_code
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
            raise classify_vcs_error(exc, body=plain_error_body(exc)) from exc

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
            raise classify_vcs_error(exc, body=plain_error_body(exc)) from exc

    async def get_blob(
        self, locator: PullRequestLocator, sha: str, *, checkpoint: RunProviderCheckpoint
    ) -> bytes:
        return await self._inner.get_blob(locator, sha, checkpoint=checkpoint)
