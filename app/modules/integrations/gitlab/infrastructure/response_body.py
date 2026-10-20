"""Shared bounded raw GitLab response reads; caller owns status and response lifetime."""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Literal

import httpx


class GitLabResponseBodyError(Exception):
    def __init__(self, kind: Literal["invalid_response", "body_too_large"]) -> None:
        self.kind = kind
        super().__init__(kind)


def validate_response_headers(response: httpx.Response) -> None:
    raw = response.headers.raw
    if len(raw) > 100 or sum(len(k) + len(v) + 4 for k, v in raw) > 16384:
        raise GitLabResponseBodyError("invalid_response")
    names: set[str] = set()
    for name, value in response.headers.multi_items():
        if name in names or any(char in value for char in "\r\n\x00"):
            raise GitLabResponseBodyError("invalid_response")
        names.add(name)


async def read_response_body(
    response: httpx.Response,
    maximum: int,
    *,
    json_content: bool,
    head: bool = False,
    charge: Callable[[int], None] | None = None,
) -> bytes:
    """Read already header-validated raw bytes; charge each chunk before checking its cap.

    HEAD's Content-Length describes the representation, not its empty response body.
    A caller's charge exception (for example an aggregate budget) passes through unchanged.
    """
    headers = response.headers
    if (
        headers.get("Content-Encoding", "identity").lower() != "identity"
        or headers.get("Transfer-Encoding", "chunked").lower() != "chunked"
        or "Content-Length" in headers
        and "Transfer-Encoding" in headers
        or response.is_stream_consumed
    ):
        raise GitLabResponseBodyError("invalid_response")
    declared: int | None = None
    if "Content-Length" in headers:
        value = headers["Content-Length"]
        if re.fullmatch(r"[0-9]{1,10}", value) is None:
            raise GitLabResponseBodyError("invalid_response")
        declared = int(value)
        if not head and declared > maximum:
            raise GitLabResponseBodyError("body_too_large")
    if json_content:
        media = headers.get("Content-Type", "").lower().split(";")
        if (
            media[0].strip() != "application/json"
            or len(media) > 2
            or len(media) == 2
            and re.fullmatch(r'\s*charset\s*=\s*(?:utf-8|"utf-8")\s*', media[1]) is None
        ):
            raise GitLabResponseBodyError("invalid_response")
    body = bytearray()
    async for chunk in response.aiter_raw():
        if charge is not None:
            charge(len(chunk))
        if len(body) + len(chunk) > maximum:
            raise GitLabResponseBodyError("invalid_response" if head else "body_too_large")
        body.extend(chunk)
    if not head and declared is not None and declared != len(body):
        raise GitLabResponseBodyError("invalid_response")
    return bytes(body)
