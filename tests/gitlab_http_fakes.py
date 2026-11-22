"""Reusable synthetic GitLab HTTP response streams."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass

import httpx


@dataclass
class Stream(httpx.AsyncByteStream):
    chunks: tuple[bytes, ...]
    closed: bool = False
    failure: BaseException | None = None
    close_error: BaseException | None = None
    on_close: Callable[[], None] | None = None

    async def __aiter__(self) -> AsyncIterator[bytes]:
        if self.failure is not None:
            raise self.failure
        for value in self.chunks:
            yield value

    async def aclose(self) -> None:
        self.closed = True
        if self.on_close is not None:
            self.on_close()
        if self.close_error is not None:
            raise self.close_error
