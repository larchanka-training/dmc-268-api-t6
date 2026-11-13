"""Shared provider profiles and HTTP completion for gateway tests."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import httpx

from app.modules.reviews.infrastructure.llm.settings import ModelPrice, ModelProfile

PRIMARY = ModelProfile(
    provider="eurouter",
    base_url="https://router.test/api/v1",
    model="primary-model",
    context_window=100_000,
    max_output_tokens=1_000,
    price=ModelPrice(Decimal("1"), Decimal("2")),
    api_keys=("sk-primary-1",),
)


FALLBACK = ModelProfile(
    provider="eurouter",
    base_url="https://router.test/api/v1",
    model="fallback-model",
    context_window=100_000,
    max_output_tokens=1_000,
    price=ModelPrice(Decimal("1"), Decimal("2")),
    api_keys=("sk-fallback-1",),
)


def completion(
    content: str | None,
    *,
    model: str = "primary-model-2026-09-01",
    prompt_tokens: int = 1200,
    completion_tokens: int = 300,
    cached_tokens: int = 0,
    cost: float | None = None,
    cost_currency: str | None = None,
    finish_reason: str = "stop",
) -> httpx.Response:
    usage: dict[str, Any] = {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
        "prompt_tokens_details": {"cached_tokens": cached_tokens},
    }
    if cost is not None:
        usage["cost"] = cost
    if cost_currency is not None:
        usage["cost_currency"] = cost_currency
    return httpx.Response(
        200,
        json={
            "id": "chatcmpl-1",
            "object": "chat.completion",
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": finish_reason,
                }
            ],
            "usage": usage,
        },
    )
