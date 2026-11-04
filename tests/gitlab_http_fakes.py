"""Reusable synthetic GitLab HTTP response streams."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass

import httpx


@dataclass
class Stream(httpx.AsyncByteStream):
    chunks: tuple[bytes, ...]
    closed: bool = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for value in self.chunks:
            yield value

    async def aclose(self) -> None:
        self.closed = True
