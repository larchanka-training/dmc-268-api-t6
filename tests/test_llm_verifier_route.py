"""The verifier model pair: ``LLM_VERIFIER_*`` settings and the gateway's verifier route (#130).

No network and no keys: settings come from literal mappings, provider answers from
``httpx.MockTransport``.
"""

from __future__ import annotations

import pytest

from app.modules.reviews.infrastructure.llm.settings import LlmConfigError, LlmSettings

EUROUTER = "https://api.eurouter.ai/api/v1"
REVIEW_ENV = {
    "LLM_MODEL": "mistral-small-4",
    "LLM_API_KEYS": "sk-eu-1",
    "LLM_FALLBACK_MODEL": "mistral-small-3.2-24b",
}
SELF_HOSTED_ENV = {
    "LLM_MODEL": "local-review",
    "LLM_BASE_URL": "http://localhost:1234/v1",
    "LLM_CONTEXT_WINDOW": "32768",
}


# ---------- settings ----------


@pytest.mark.parametrize("value", [None, ""], ids=["absent", "empty"])
def test_without_verifier_variables_the_verifier_route_is_the_review_pair(
    value: str | None,
) -> None:
    env = dict(REVIEW_ENV)
    if value is not None:
        env |= {"LLM_VERIFIER_MODEL": value, "LLM_VERIFIER_FALLBACK_MODEL": value}

    settings = LlmSettings.from_env(env)

    assert settings.verifier is None
    assert settings.verifier_fallback is None
    assert settings.fallback is not None
    assert settings.verifier_route() == (settings.primary, settings.fallback)
    assert [profile.model for profile in settings.verifier_route() if profile] == [
        "mistral-small-4",
        "mistral-small-3.2-24b",
    ]


def test_a_known_verifier_model_on_the_primary_endpoint_inherits_its_keys() -> None:
    settings = LlmSettings.from_env({**REVIEW_ENV, "LLM_VERIFIER_MODEL": "mistral-small-3.2-24b"})

    verifier, verifier_fallback = settings.verifier_route()
    assert settings.verifier is verifier
    assert verifier.model == "mistral-small-3.2-24b"
    assert verifier.base_url == EUROUTER
    assert verifier.api_keys == ("sk-eu-1",)
    assert verifier.context_window == 128_000
    assert settings.primary.model == "mistral-small-4"
    # Without LLM_VERIFIER_FALLBACK_MODEL the already-parsed review fallback is reused.
    assert verifier_fallback is settings.fallback


def test_only_a_verifier_fallback_keeps_the_review_primary() -> None:
    env = {"LLM_MODEL": "mistral-small-3.2-24b", "LLM_API_KEYS": "sk-eu-1"}

    settings = LlmSettings.from_env({**env, "LLM_VERIFIER_FALLBACK_MODEL": "mistral-small-4"})

    verifier, verifier_fallback = settings.verifier_route()
    assert settings.verifier is None
    assert settings.fallback is None
    assert verifier is settings.primary
    assert verifier_fallback is settings.verifier_fallback
    assert verifier_fallback is not None
    assert verifier_fallback.model == "mistral-small-4"
    assert verifier_fallback.api_keys == ("sk-eu-1",)
    assert verifier_fallback.context_window == 262_144


def test_the_verifier_fallback_inherits_from_the_verifier_not_the_review_primary() -> None:
    settings = LlmSettings.from_env(
        {
            **REVIEW_ENV,
            "LLM_VERIFIER_MODEL": "verifier-model",
            "LLM_VERIFIER_BASE_URL": "https://verifier.test/v1",
            "LLM_VERIFIER_API_KEYS": "sk-verifier-1",
            "LLM_VERIFIER_CONTEXT_WINDOW": "64000",
            "LLM_VERIFIER_FALLBACK_MODEL": "verifier-fallback-model",
            "LLM_VERIFIER_FALLBACK_CONTEXT_WINDOW": "32000",
        }
    )

    _, verifier_fallback = settings.verifier_route()
    assert verifier_fallback is not None
    assert verifier_fallback.base_url == "https://verifier.test/v1"
    assert verifier_fallback.api_keys == ("sk-verifier-1",)


def test_a_known_verifier_model_behind_a_self_hosted_primary_needs_its_own_keys() -> None:
    with pytest.raises(LlmConfigError) as caught:
        LlmSettings.from_env({**SELF_HOSTED_ENV, "LLM_VERIFIER_MODEL": "mistral-small-4"})

    assert str(caught.value) == "LLM_VERIFIER_API_KEYS must be set for mistral-small-4"


def test_an_unknown_verifier_model_without_a_base_url_uses_the_primary_endpoint() -> None:
    settings = LlmSettings.from_env(
        {
            **SELF_HOSTED_ENV,
            "LLM_VERIFIER_MODEL": "local-verifier",
            "LLM_VERIFIER_CONTEXT_WINDOW": "16384",
        }
    )

    assert settings.verifier is not None
    assert settings.verifier.base_url == "http://localhost:1234/v1"
    assert settings.verifier.context_window == 16_384


def test_verifier_prompt_json_requires_the_dev_flag() -> None:
    env = {
        **REVIEW_ENV,
        "LLM_VERIFIER_MODEL": "mistral-small-3.2-24b",
        "LLM_VERIFIER_STRUCTURED_OUTPUT": "prompt_json",
    }

    with pytest.raises(LlmConfigError) as caught:
        LlmSettings.from_env(env)

    assert str(caught.value) == (
        "LLM_VERIFIER_STRUCTURED_OUTPUT=prompt_json needs LLM_ALLOW_PROMPT_JSON=1 (dev/eval only)"
    )


def test_primary_keys_are_not_sent_to_another_verifier_host() -> None:
    settings = LlmSettings.from_env(
        {
            **REVIEW_ENV,
            "LLM_VERIFIER_MODEL": "verifier-model",
            "LLM_VERIFIER_BASE_URL": "https://verifier.test/v1",
            "LLM_VERIFIER_CONTEXT_WINDOW": "64000",
        }
    )

    assert settings.verifier is not None
    assert settings.verifier.api_keys == ()
    assert settings.verifier.provider == "self-hosted"
