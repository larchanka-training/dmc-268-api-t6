"""Reject nonfinite numbers while FastAPI parses JSON request bodies."""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Coroutine
from typing import Any, NoReturn

from fastapi import Request, Response
from fastapi.routing import APIRoute


class _NonfiniteJsonNumber(ValueError):
    pass


def _reject_nonfinite_constant(_value: str) -> NoReturn:
    raise _NonfiniteJsonNumber


def _finite_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise _NonfiniteJsonNumber
    return parsed


class StrictJsonRequest(Request):
    async def json(self) -> Any:
        if not hasattr(self, "_json"):
            body = await self.body()
            try:
                self._json = json.loads(
                    body,
                    parse_constant=_reject_nonfinite_constant,
                    parse_float=_finite_float,
                )
            except _NonfiniteJsonNumber as error:
                raise json.JSONDecodeError("Nonfinite JSON number", "", 0) from error
        return self._json


class StrictJsonRoute(APIRoute):
    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        original_handler = super().get_route_handler()

        async def strict_json_handler(request: Request) -> Response:
            return await original_handler(StrictJsonRequest(request.scope, request.receive))

        return strict_json_handler
