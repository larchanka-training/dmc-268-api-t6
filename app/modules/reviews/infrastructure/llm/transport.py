"""OpenAI-compatible ``/chat/completions`` transport with key rotation.

One ``complete`` call is one gateway call (one ``llm.call`` record): trying the next key
after 401, 403 or 429 happens inside it and is not counted (docs/PIPELINE_SPEC.md §4.5).
HTTP failures are mapped to the classes of §5.1; a key never reaches a message.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
from collections.abc import Mapping
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Any, Literal, Protocol

import httpx

from app.modules.reviews.application.llm import LlmErrorCode
from app.modules.reviews.infrastructure.llm.settings import ModelProfile

logger = logging.getLogger(__name__)

type Role = Literal["system", "user", "assistant"]

_ROTATE_STATUSES = frozenset({401, 403, 429})
_CONTEXT_MARKERS = (
    "context_length_exceeded",
    "context length",
    "context window",
    "maximum context",
    "too many tokens",
    "prompt is too long",
    "input is too long",
)
_MAX_ERROR_TEXT = 300


@dataclass(frozen=True)
class ChatMessage:
    role: Role
    content: str


@dataclass(frozen=True)
class ResponseSchema:
    """A strict JSON Schema for ``response_format`` (structured output)."""

    name: str
    schema: Mapping[str, object]


@dataclass(frozen=True)
class ChatRequest:
    profile: ModelProfile
    messages: tuple[ChatMessage, ...]
    response_schema: ResponseSchema | None
    timeout_s: float


@dataclass(frozen=True)
class ChatResponse:
    """The provider body as is, plus the fields the gateway reads from it."""

    raw: dict[str, Any]
    content: str | None
    finish_reason: str | None
    model: str
    prompt_tokens: int
    completion_tokens: int
    cached_tokens: int
    cost: Decimal | None
    cost_currency: str | None
    refusal: str | None = None


class TransportError(Exception):
    """A failed call, already classified; ``retryable`` allows a same-model retry."""

    error_class: LlmErrorCode = LlmErrorCode.UNAVAILABLE

    def __init__(
        self,
        message: str,
        *,
        http_status: int | None = None,
        retry_after_s: float | None = None,
        retryable: bool = True,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.http_status = http_status
        self.retry_after_s = retry_after_s
        self.retryable = retryable


class TransportTimeout(TransportError):
    error_class = LlmErrorCode.TIMEOUT


class TransportRateLimited(TransportError):
    error_class = LlmErrorCode.RATE_LIMITED


class TransportUnavailable(TransportError):
    error_class = LlmErrorCode.UNAVAILABLE


class TransportContextOverflow(TransportError):
    error_class = LlmErrorCode.CONTEXT_OVERFLOW


class TransportPaidAnswerError(TransportUnavailable):
    """A paid HTTP 200 whose usage metadata cannot be safely charged as USD."""

    def __init__(self, message: str, paid_response: ChatResponse) -> None:
        super().__init__(message, http_status=200, retryable=False)
        self.paid_response = paid_response


class ChatTransport(Protocol):
    async def complete(self, request: ChatRequest) -> ChatResponse: ...


class OpenAICompatibleTransport:
    """One adapter for EUrouter and self-hosted servers; they differ only in config.

    The client is owned by the caller (worker lifespan). The next call of a provider
    starts with the key that answered last, so a revoked key is skipped once.
    """

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client
        self._current_key: dict[tuple[str, tuple[str, ...]], int] = {}

    async def complete(self, request: ChatRequest) -> ChatResponse:
        profile = request.profile
        keys: tuple[str | None, ...] = profile.api_keys or (None,)
        pool = (profile.base_url, profile.api_keys)
        start = self._current_key.get(pool, 0) % len(keys)
        body = _request_body(request)
        last: TransportError | None = None
        rate_limited: TransportError | None = None
        for offset in range(len(keys)):
            index = (start + offset) % len(keys)
            try:
                response = await self._post(request, body, keys[index])
            except TransportError as error:
                raise _redacted(error, profile.api_keys) from None
            if response.status_code in _ROTATE_STATUSES:
                last = _status_error(response, profile.api_keys)
                if isinstance(last, TransportRateLimited):
                    rate_limited = last
                logger.warning(
                    "llm provider rejected key",
                    extra={
                        "provider": profile.provider,
                        "status": response.status_code,
                        "key_index": index,
                    },
                )
                continue
            self._current_key[pool] = index
            return _parse_response(response, profile)
        assert last is not None
        # A key that is only rate limited outranks a dead one: the gateway then waits
        # for Retry-After and reports llm_rate_limited, not llm_unavailable.
        raise rate_limited or last

    async def _post(
        self, request: ChatRequest, body: dict[str, Any], key: str | None
    ) -> httpx.Response:
        headers = {"Content-Type": "application/json"}
        if key is not None:
            headers["Authorization"] = f"Bearer {key}"
        url = f"{request.profile.base_url}/chat/completions"
        try:
            async with asyncio.timeout(request.timeout_s):
                return await self._client.post(
                    url,
                    json=body,
                    headers=headers,
                    timeout=httpx.Timeout(request.timeout_s),
                )
        except (TimeoutError, httpx.TimeoutException) as error:
            raise TransportTimeout(
                f"no answer within {request.timeout_s:g} s ({type(error).__name__})"
            ) from None
        except httpx.HTTPError as error:
            raise TransportUnavailable(f"connection failed ({type(error).__name__})") from None


def _request_body(request: ChatRequest) -> dict[str, Any]:
    """Defaults, then ``extra_body`` on top of them, then the fields config cannot change.

    ``extra_body`` overrides sampling defaults (a reasoning model rejects
    ``temperature: 0`` and wants ``max_completion_tokens``); a ``null`` value drops the
    key. The model, the messages and strict ``response_format`` (D7) stay fixed.
    """
    profile = request.profile
    defaults: dict[str, Any] = {
        "max_tokens": profile.max_output_tokens,
        "temperature": 0,
    }
    merged = {**defaults, **profile.extra_body}
    body: dict[str, Any] = {key: value for key, value in merged.items() if value is not None}
    body["model"] = profile.model
    body["messages"] = [
        {"role": message.role, "content": message.content} for message in request.messages
    ]
    if request.response_schema is not None and profile.structured_output == "json_schema":
        body["response_format"] = {
            "type": "json_schema",
            "json_schema": {
                "name": request.response_schema.name,
                "strict": True,
                "schema": dict(request.response_schema.schema),
            },
        }
    return body


def _status_error(response: httpx.Response, keys: tuple[str, ...]) -> TransportError:
    status = response.status_code
    text = _error_text(response, keys)
    if status == 429:
        return TransportRateLimited(
            f"HTTP 429: {text}", http_status=status, retry_after_s=_retry_after(response)
        )
    if status in (401, 403):
        # Every key was refused: a same-model retry cannot help, the fallback may.
        return TransportUnavailable(f"HTTP {status}: {text}", http_status=status, retryable=False)
    if status == 413 or (
        status == 400 and any(marker in text.lower() for marker in _CONTEXT_MARKERS)
    ):
        return TransportContextOverflow(
            f"HTTP {status}: {text}", http_status=status, retryable=False
        )
    if status >= 500:
        return TransportUnavailable(f"HTTP {status}: {text}", http_status=status)
    return TransportUnavailable(f"HTTP {status}: {text}", http_status=status, retryable=False)


def _parse_response(response: httpx.Response, profile: ModelProfile) -> ChatResponse:
    if response.status_code != 200:
        raise _status_error(response, profile.api_keys)
    try:
        raw = response.json()
        choice = raw["choices"][0]
        message = choice["message"]
        content = message.get("content")
        if isinstance(content, list):
            # Content parts: the text of every ``text`` part, in order.
            content = "".join(
                str(part.get("text", ""))
                for part in content
                if isinstance(part, dict) and part.get("type") == "text"
            )
        refusal = message.get("refusal")
        usage = raw.get("usage") or {}
        details = usage.get("prompt_tokens_details") or {}
        currency_raw = usage.get("cost_currency")
        parsed = ChatResponse(
            raw=raw,
            content=content if isinstance(content, str) else None,
            finish_reason=choice.get("finish_reason"),
            model=str(raw.get("model") or ""),
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
            cached_tokens=int(details.get("cached_tokens") or 0),
            cost=None,
            cost_currency=currency_raw if isinstance(currency_raw, str) else None,
            refusal=refusal if isinstance(refusal, str) and refusal else None,
        )
        if currency_raw is not None and (
            not isinstance(currency_raw, str) or currency_raw.upper() not in ("USD", "EUR")
        ):
            raise TransportPaidAnswerError("HTTP 200 with unsupported usage.cost_currency", parsed)
        currency = currency_raw.upper() if currency_raw is not None else None
        try:
            cost = _exact_cost(response) if usage.get("cost") is not None else None
        except (ValueError, KeyError, TypeError, AttributeError):
            raise TransportPaidAnswerError("HTTP 200 with invalid usage.cost", parsed) from None
        if cost is not None and currency is None:
            logger.warning(
                "llm usage cost has no currency; treating as USD",
                extra={"provider": profile.provider, "model": profile.model},
            )
        return replace(parsed, cost=cost, cost_currency=currency)
    except (ValueError, KeyError, IndexError, TypeError, AttributeError):
        raise TransportUnavailable(
            "HTTP 200 with a body that is not a chat completion", http_status=200
        ) from None


def _exact_cost(response: httpx.Response) -> Decimal:
    """Parse the JSON number separately so the raw trace keeps JSON-native types."""
    precise = json.loads(response.content, parse_float=Decimal)
    value = precise["usage"]["cost"]
    if isinstance(value, bool) or not isinstance(value, (int, Decimal)):
        raise ValueError("invalid usage.cost")
    cost = Decimal(value)
    if not cost.is_finite() or cost < 0:
        raise ValueError("invalid usage.cost")
    return cost


def _error_text(response: httpx.Response, keys: tuple[str, ...]) -> str:
    """Redact first, cut second: a key on the cut boundary must not leave a prefix."""
    try:
        payload = response.json()
    except ValueError:
        return _redact(response.text, keys)[:_MAX_ERROR_TEXT]
    error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error, dict):
        parts = [str(error.get(name)) for name in ("code", "message") if error.get(name)]
        return _redact(" ".join(parts), keys)[:_MAX_ERROR_TEXT]
    return _redact(str(payload), keys)[:_MAX_ERROR_TEXT]


def _retry_after(response: httpx.Response) -> float | None:
    """Seconds only; an HTTP-date or a negative / non-finite value counts as no header."""
    raw = response.headers.get("Retry-After")
    if raw is None:
        return None
    try:
        seconds = float(raw)
    except ValueError:
        return None
    return seconds if math.isfinite(seconds) and seconds >= 0 else None


def _redact(text: str, keys: tuple[str, ...]) -> str:
    for key in keys:
        if key:
            text = text.replace(key, "***")
    return text


def _redacted(error: TransportError, keys: tuple[str, ...]) -> TransportError:
    error.message = _redact(error.message, keys)
    error.args = (error.message,)
    return error
