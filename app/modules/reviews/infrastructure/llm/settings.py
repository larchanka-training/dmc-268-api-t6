"""LLM gateway configuration: model profiles from env, failure policy from PIPELINE_SPEC.

EUrouter and a self-hosted server (LM Studio, Ollama, vLLM) are both OpenAI-compatible,
so a provider is only a base URL, a model and a list of keys (docs/SECRETS.md).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Literal
from urllib.parse import urlsplit

from app.modules.reviews.application.llm import EngineName

logger = logging.getLogger(__name__)

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
    """Defaults of docs/PIPELINE_SPEC.md §3, §4.5, §5.1.

    Set in code (``LlmSettings(policy=…)`` in the composition root or tests); ``from_env``
    reads only the model profiles, the policy stays the reviewed spec value.
    """

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
    rate_limit_default_delay_s: float = 2.0


@dataclass(frozen=True)
class LlmSettings:
    primary: ModelProfile
    fallback: ModelProfile | None
    policy: GatewayPolicy = field(default_factory=GatewayPolicy)
    eur_to_usd_rate: Decimal | None = None

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> LlmSettings:
        """Read ``LLM_*`` (primary) and ``LLM_FALLBACK_*`` (fallback) variables.

        A known fallback model keeps its own endpoint; an unknown one without
        ``LLM_FALLBACK_BASE_URL`` uses the primary's. Keys, provider and output mode are
        inherited only on the primary's endpoint, never sent to another host.
        The prompt-JSON path is refused unless ``LLM_ALLOW_PROMPT_JSON=1``:
        it exists for local and self-hosted models in dev and eval only (D7).
        """
        allow_prompt_json = env.get("LLM_ALLOW_PROMPT_JSON", "") == "1"
        primary = _profile_from_env(env, "LLM_", None, allow_prompt_json)
        fallback = (
            _profile_from_env(env, "LLM_FALLBACK_", primary, allow_prompt_json)
            if env.get("LLM_FALLBACK_MODEL")
            else None
        )
        rate_raw = env.get("LLM_EUR_TO_USD_RATE")
        rate = None
        if rate_raw:
            try:
                rate = Decimal(rate_raw)
            except InvalidOperation:
                raise LlmConfigError(
                    "LLM_EUR_TO_USD_RATE must be a positive finite decimal"
                ) from None
            if not rate.is_finite() or rate <= 0:
                raise LlmConfigError("LLM_EUR_TO_USD_RATE must be a positive finite decimal")
        return cls(primary=primary, fallback=fallback, eur_to_usd_rate=rate)


# Chosen for OQ-2 (docs/SYSTEM_DESIGN.md §15) in #46: catalog values of EUrouter on
# 2026-10-05. Env variables override every field, so a price change needs no release.
KNOWN_MODELS: Mapping[str, ModelProfile] = {
    # Price of the Mistral AI route, which served every live run. Regolo (EUR 0.50 / 2.10 per
    # 1M) is up to ~3.5x higher, so the pre-call estimate may undershoot there; the worst case
    # in docs/SYSTEM_DESIGN.md §15 uses Regolo.
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
    # Every endpoint bills in EUR, so the stored values are USD converted from EUR: the most
    # expensive endpoint (GreenPT, EUR 0.20 / 0.40 per 1M) at EUrouter's own rate of
    # 1.1225 USD/EUR (usage.cost / usage.cost_eur, 2026-10-05).
    # The catalog has no cache-read price, so cached tokens count at the input price.
    # The window is the smallest endpoint's (Scaleway, GreenPT).
    "mistral-small-3.2-24b": ModelProfile(
        provider="eurouter",
        base_url="https://api.eurouter.ai/api/v1",
        model="mistral-small-3.2-24b",
        context_window=128_000,
        max_output_tokens=8_000,
        price=ModelPrice(Decimal("0.2245"), Decimal("0.449"), Decimal("0.2245")),
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

    # A known model keeps its own endpoint; only an unknown one inherits the primary's.
    explicit_url = value("BASE_URL")
    if explicit_url is not None:
        base_url = explicit_url.rstrip("/")
    elif known is not None:
        base_url = known.base_url
    elif inherit is not None:
        base_url = inherit.base_url
    else:
        raise LlmConfigError(f"{prefix}BASE_URL must be set for an unknown model")
    same_endpoint = inherit is not None and base_url == inherit.base_url

    keys_raw = value("API_KEYS")
    if keys_raw is not None:
        api_keys = tuple(item.strip() for item in keys_raw.split(",") if item.strip())
    elif same_endpoint and inherit is not None:
        # Keys follow their endpoint: they are never sent to another host.
        api_keys = inherit.api_keys
    else:
        api_keys = ()
    if known is not None and base_url == known.base_url and not api_keys:
        raise LlmConfigError(f"{prefix}API_KEYS must be set for {model}")
    if api_keys and urlsplit(base_url).scheme.lower() != "https" and not _is_local(base_url):
        raise LlmConfigError(f"{prefix}BASE_URL must use https when keys are sent")

    provider = value("PROVIDER")
    if provider is None and known is not None and base_url == known.base_url:
        provider = known.provider
    if provider is None and same_endpoint and inherit is not None:
        provider = inherit.provider
    provider = provider or "self-hosted"

    structured = value("STRUCTURED_OUTPUT") or (known.structured_output if known else None)
    if structured is None and same_endpoint and inherit is not None:
        structured = inherit.structured_output
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
            parsed = int(raw)
        except ValueError as error:
            raise LlmConfigError(f"{prefix}{name} must be an integer") from error
        if parsed <= 0:
            raise LlmConfigError(f"{prefix}{name} must be positive")
        return parsed

    def price(name: str, default: Decimal | None) -> Decimal:
        raw = value(name)
        if raw is None:
            return default if default is not None else Decimal(0)
        try:
            parsed = Decimal(raw)
        except ArithmeticError as error:
            raise LlmConfigError(f"{prefix}{name} must be a decimal") from error
        if not parsed.is_finite() or parsed < 0:
            raise LlmConfigError(f"{prefix}{name} must be a non-negative decimal")
        return parsed

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

    context_window = number("CONTEXT_WINDOW", known.context_window if known else None)
    max_output_tokens = number("MAX_OUTPUT_TOKENS", known.max_output_tokens if known else 8_000)
    if max_output_tokens >= context_window:
        raise LlmConfigError(f"{prefix}MAX_OUTPUT_TOKENS must be below {prefix}CONTEXT_WINDOW")
    profile = ModelProfile(
        provider=provider,
        base_url=base_url,
        model=model,
        context_window=context_window,
        max_output_tokens=max_output_tokens,
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
    if not _is_local(base_url) and not (
        profile.price.input_per_mtok or profile.price.output_per_mtok
    ):
        # Without a price the run cost limit only sees provider-reported usage.cost.
        logger.warning(
            "llm model has no price, the run cost limit relies on usage.cost",
            extra={"model": model, "variable": f"{prefix}PRICE_INPUT_PER_MTOK"},
        )
    return profile


_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "host.docker.internal"})


def _is_local(base_url: str) -> bool:
    host = urlsplit(base_url).hostname or ""
    return host in _LOCAL_HOSTS or host.endswith((".localhost", ".local", ".internal"))
