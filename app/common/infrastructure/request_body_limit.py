"""Stop reading an oversized request body on every route whose body FastAPI parses."""

from __future__ import annotations

from fastapi import HTTPException
from fastapi.routing import APIRoute
from starlette.datastructures import Headers
from starlette.types import ASGIApp, Message, Receive, Scope, Send

MAX_REQUEST_BODY_BYTES = 64 * 1024


def _declares_too_much(scope: Scope) -> bool:
    content_length = Headers(scope=scope).get("content-length")
    try:
        return content_length is not None and int(content_length) > MAX_REQUEST_BODY_BYTES
    except ValueError:
        return False  # A malformed header decides nothing: the bytes are still counted.


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
            if _declares_too_much(scope):
                raise HTTPException(status_code=413, detail="payload too large")
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > MAX_REQUEST_BODY_BYTES:
                    raise HTTPException(status_code=413, detail="payload too large")
            return message

        await self._app(scope, limited_receive, send)
