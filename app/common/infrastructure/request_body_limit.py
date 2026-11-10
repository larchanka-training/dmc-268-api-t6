"""Stop reading an oversized request body on every route whose body FastAPI parses."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import HTTPException
from fastapi.routing import APIRoute
from starlette.datastructures import Headers
from starlette.types import ASGIApp, Message, Receive, Scope, Send

MAX_REQUEST_BODY_BYTES = 64 * 1024
_LIMIT_ATTRIBUTE = "max_request_body_bytes"


def allow_request_body[F: Callable[..., Any]](limit: int) -> Callable[[F], F]:
    """Give one endpoint its own body limit in place of ``MAX_REQUEST_BODY_BYTES``."""

    def mark(endpoint: F) -> F:
        setattr(endpoint, _LIMIT_ATTRIBUTE, limit)
        return endpoint

    return mark


def request_body_limit(route: APIRoute) -> int:
    """The body limit of a route: its endpoint's own, else ``MAX_REQUEST_BODY_BYTES``."""
    limit: int = getattr(route.endpoint, _LIMIT_ATTRIBUTE, MAX_REQUEST_BODY_BYTES)
    return limit


def _declares_too_much(scope: Scope, limit: int) -> bool:
    content_length = Headers(scope=scope).get("content-length")
    try:
        return content_length is not None and int(content_length) > limit
    except ValueError:
        return False  # A malformed header decides nothing: the bytes are still counted.


def _too_large() -> HTTPException:
    return HTTPException(status_code=413, detail="payload too large")


class RequestBodyLimitMiddleware:
    """Answer 413 from ``receive``: FastAPI reads the body before it runs any dependency.

    The router has set ``scope["route"]`` by the time the body is read. A route without a
    ``body_field`` passes untouched: the webhooks read ``request.stream()`` and count their own
    limits. FastAPI re-raises an ``HTTPException`` from the read; any other exception becomes 400.
    """

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            route = scope.get("route")
            if not isinstance(route, APIRoute) or route.body_field is None:
                return await receive()
            limit = request_body_limit(route)
            if _declares_too_much(scope, limit):
                raise _too_large()
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    raise _too_large()
            return message

        await self._app(scope, limited_receive, send)
