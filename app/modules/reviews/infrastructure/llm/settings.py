"""LLM gateway configuration: model profiles from env, failure policy from PIPELINE_SPEC.

EUrouter and a self-hosted server (LM Studio, Ollama, vLLM) are both OpenAI-compatible,
so a provider is only a base URL, a model and a list of keys (docs/SECRETS.md).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Literal

from app.modules.reviews.application.llm import EngineName

type StructuredOutput = Literal["json_schema", "prompt_json"]

_MILLION = Decimal(1_000_000)


@dataclass(frozen=True)
class ModelPrice:
    """USD per one million tokens; cache reads are billed separately from other input."""

    input_per_mtok: Decimal
    output_per_mtok: Decimal
    cache_read_per_mtok: Decimal = Decimal(0)

    def cost_usd(self, *, tokens_in: int, tokens_out: int, cache_read_tokens: int = 0) -> Decimal:
        uncached = max(tokens_in - cache_read_tokens, 0)
        total = (
            uncached * self.input_per_mtok
            + cache_read_tokens * self.cache_read_per_mtok
            + tokens_out * self.output_per_mtok
        ) / _MILLION
        return total.quantize(Decimal("0.000001"))


@dataclass(frozen=True)
class ModelProfile:
    """One model behind an OpenAI-compatible endpoint. Keys never appear in its repr."""

    provider: str
    base_url: str
    model: str
    context_window: int
    max_output_tokens: int
    price: ModelPrice
    structured_output: StructuredOutput = "json_schema"
    chars_per_token: float = 3.0
    api_keys: tuple[str, ...] = field(default=(), repr=False)
    extra_body: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class GatewayPolicy:
    """Defaults of docs/PIPELINE_SPEC.md §3, §4.5, §5.1; overridable in config."""

    call_timeout_s: Mapping[EngineName, float] = field(
        default_factory=lambda: {"fast": 90.0, "deep": 300.0}
    )
    input_token_limit: Mapping[EngineName, int] = field(
        default_factory=lambda: {"fast": 60_000, "deep": 150_000}
    )
    run_cost_limit_usd: Mapping[EngineName, Decimal] = field(
        default_factory=lambda: {"fast": Decimal("0.50"), "deep": Decimal("3")}
    )
    max_calls_per_attempt: int = 4
    timeout_retry_delays_s: tuple[float, ...] = (2.0,)
    unavailable_retry_delays_s: tuple[float, ...] = (2.0, 8.0)
    max_jitter_s: float = 1.0
    max_retry_after_s: float = 30.0


@dataclass(frozen=True)
class LlmSettings:
    primary: ModelProfile
    fallback: ModelProfile | None
    policy: GatewayPolicy = field(default_factory=GatewayPolicy)

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> LlmSettings:
        """Read ``LLM_*`` (primary) and ``LLM_FALLBACK_*`` (fallback) variables.

        Fallback variables that are not set inherit the provider, base URL and keys of
        the primary. The prompt-JSON path is refused unless ``LLM_ALLOW_PROMPT_JSON=1``:
        it exists for local and self-hosted models in dev and eval only (D7).
        """
        allow_prompt_json = env.get("LLM_ALLOW_PROMPT_JSON", "") == "1"
        primary = _profile_from_env(env, "LLM_", None, allow_prompt_json)
        fallback = (
            _profile_from_env(env, "LLM_FALLBACK_", primary, allow_prompt_json)
            if env.get("LLM_FALLBACK_MODEL")
            else None
        )
        return cls(primary=primary, fallback=fallback)


# Chosen and checked for OQ-2 (docs/SYSTEM_DESIGN.md §15): catalog values of EUrouter
# on 2026-09-29. Env variables override every field, so a price change needs no release.
KNOWN_MODELS: Mapping[str, ModelProfile] = {
    "gpt-4.1-mini": ModelProfile(
        provider="eurouter",
        base_url="https://api.eurouter.ai/api/v1",
        model="gpt-4.1-mini",
        context_window=1_047_576,
        max_output_tokens=8_000,
        price=ModelPrice(Decimal("0.44"), Decimal("1.76"), Decimal("0.11")),
        structured_output="json_schema",
        chars_per_token=3.0,
    ),
    "mistral-small-4": ModelProfile(
        provider="eurouter",
        base_url="https://api.eurouter.ai/api/v1",
        model="mistral-small-4",
        context_window=262_144,
        max_output_tokens=8_000,
        price=ModelPrice(Decimal("0.165"), Decimal("0.66"), Decimal("0.0165")),
        structured_output="json_schema",
        chars_per_token=3.0,
    ),
}


class LlmConfigError(ValueError):
    """The LLM environment is incomplete or unsafe; the message names variables only."""


def _profile_from_env(
    env: Mapping[str, str],
    prefix: str,
    inherit: ModelProfile | None,
    allow_prompt_json: bool,
) -> ModelProfile:
    model = env.get(f"{prefix}MODEL")
    if not model:
        raise LlmConfigError(f"{prefix}MODEL must be set")
    known = KNOWN_MODELS.get(model)

    def value(name: str) -> str | None:
        raw = env.get(f"{prefix}{name}")
        return raw if raw else None

    base_url = value("BASE_URL") or (inherit.base_url if inherit else None)
    base_url = base_url or (known.base_url if known else None)
    if base_url is None:
        raise LlmConfigError(f"{prefix}BASE_URL must be set for an unknown model")

    keys_raw = value("API_KEYS")
    if keys_raw is not None:
        api_keys = tuple(item.strip() for item in keys_raw.split(",") if item.strip())
    else:
        api_keys = inherit.api_keys if inherit else ()

    provider = value("PROVIDER") or (inherit.provider if inherit else None)
    provider = provider or (known.provider if known else "self-hosted")

    structured = value("STRUCTURED_OUTPUT") or (known.structured_output if known else None)
    structured = structured or "json_schema"
    if structured not in ("json_schema", "prompt_json"):
        raise LlmConfigError(f"{prefix}STRUCTURED_OUTPUT must be json_schema or prompt_json")
    if structured == "prompt_json" and not allow_prompt_json:
        raise LlmConfigError(
            f"{prefix}STRUCTURED_OUTPUT=prompt_json needs LLM_ALLOW_PROMPT_JSON=1 (dev/eval only)"
        )

    def number(name: str, default: int | None) -> int:
        raw = value(name)
        if raw is None:
            if default is None:
                raise LlmConfigError(f"{prefix}{name} must be set for an unknown model")
            return default
        try:
            return int(raw)
        except ValueError as error:
            raise LlmConfigError(f"{prefix}{name} must be an integer") from error

    def price(name: str, default: Decimal | None) -> Decimal:
        raw = value(name)
        if raw is None:
            return default if default is not None else Decimal(0)
        try:
            return Decimal(raw)
        except ArithmeticError as error:
            raise LlmConfigError(f"{prefix}{name} must be a decimal") from error

    extra_raw = value("EXTRA_BODY")
    extra_body: Mapping[str, object] = {}
    if extra_raw is not None:
        try:
            parsed = json.loads(extra_raw)
        except json.JSONDecodeError as error:
            raise LlmConfigError(f"{prefix}EXTRA_BODY must be a JSON object") from error
        if not isinstance(parsed, dict):
            raise LlmConfigError(f"{prefix}EXTRA_BODY must be a JSON object")
        extra_body = parsed

    return ModelProfile(
        provider=provider,
        base_url=base_url.rstrip("/"),
        model=model,
        context_window=number("CONTEXT_WINDOW", known.context_window if known else None),
        max_output_tokens=number("MAX_OUTPUT_TOKENS", known.max_output_tokens if known else 8_000),
        price=ModelPrice(
            input_per_mtok=price(
                "PRICE_INPUT_PER_MTOK", known.price.input_per_mtok if known else None
            ),
            output_per_mtok=price(
                "PRICE_OUTPUT_PER_MTOK", known.price.output_per_mtok if known else None
            ),
            cache_read_per_mtok=price(
                "PRICE_CACHE_READ_PER_MTOK", known.price.cache_read_per_mtok if known else None
            ),
        ),
        structured_output="prompt_json" if structured == "prompt_json" else "json_schema",
        chars_per_token=known.chars_per_token if known else 3.0,
        api_keys=api_keys,
        extra_body=extra_body,
    )
