"""The verifier's gateway calls on the snapshot path (#130 F1-7).

- ``LlmGateway.max_prompt_tokens(engine, route)`` and ``GatewayVerifierModel`` name the
  verifier route's prompt window; ``ReflectionPass`` fits to it (R10).
- A stored verifier ``llm.call`` response carries no ``decisions[].reason`` (E-2, TL-3):
  the answer text of a JSON object is stored without it, any other text is withheld whole.
  Review and conventions responses stay as the provider sent them.
- ``llm.verify`` names the stored manifest of the accepted verifier call.
- A manifest write that fails ends the run with ``internal_error``, before any call.

No network: provider answers come from ``httpx.MockTransport``.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import replace
from uuid import UUID

import httpx
import pytest

from app.modules.reviews.application.llm import ManifestEntry, ManifestStore
from app.modules.reviews.application.reflection_pass import ReflectionPass, VerificationRecord
from app.modules.reviews.application.run_failures import RunFailure
from app.modules.reviews.infrastructure.llm.gateway import LlmGateway, StructuredTask
from app.modules.reviews.infrastructure.llm.models import GatewayVerifierModel
from app.modules.reviews.infrastructure.llm.settings import LlmSettings
from app.modules.reviews.infrastructure.llm.transport import (
    ChatMessage,
    OpenAICompatibleTransport,
    ResponseSchema,
)
from tests.llm_fakes import FakeClock, ScriptedProvider, completion, context_checkpoint
from tests.test_gateway_verifier_model import (
    NOW,
    REVIEW,
    RUN,
    SETTINGS,
    VERIFIER,
    _answer,
    _candidate,
    _context,
    _decision,
    _snapshot,
)


class _Store:
    def __init__(self) -> None:
        self.entries: list[ManifestEntry] = []

    async def record_manifest(self, run_id: UUID, entry: ManifestEntry) -> None:
        self.entries.append(entry)


class _BrokenStore:
    async def record_manifest(self, run_id: UUID, entry: ManifestEntry) -> None:
        raise OSError("database is down")


def _run[T](
    provider: ScriptedProvider,
    call: Callable[[LlmGateway], Awaitable[T]],
    *,
    settings: LlmSettings = SETTINGS,
    store: ManifestStore | None = None,
) -> T:
    async def exercise() -> T:
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider.handle)) as client:
            clock = FakeClock(NOW)
            gateway = LlmGateway(
                settings,
                OpenAICompatibleTransport(client),
                provider.ledger,
                provider.trace,
                checkpoint=context_checkpoint,
                clock=clock,
                sleep=clock.sleep,
                jitter=lambda: 0.0,
                monotonic=clock.monotonic,
                manifest_store=store,
            )
            return await call(gateway)

    return asyncio.run(exercise())


def _verify(
    provider: ScriptedProvider,
    *,
    settings: LlmSettings = SETTINGS,
    store: ManifestStore | None = None,
) -> VerificationRecord:
    async def verify(gateway: LlmGateway) -> VerificationRecord:
        return await ReflectionPass(GatewayVerifierModel(gateway, RUN)).verify_recorded(
            candidates=(_candidate("c-1"),), context=_context(_snapshot())
        )

    return _run(provider, verify, settings=settings, store=store)


def _stored_contents(provider: ScriptedProvider) -> list[str]:
    return [
        record.response_json()["choices"][0]["message"]["content"]
        for _, record in provider.trace.records
    ]


# ---------- the verifier route window ----------


def test_the_gateway_names_the_prompt_window_of_each_route() -> None:
    # fast caps the window at 60 000; the verifier model has 20 000 and answers in 1 000.
    small = replace(VERIFIER, context_window=20_000)

    async def windows(gateway: LlmGateway) -> tuple[int, int, int]:
        return (
            gateway.max_prompt_tokens("fast"),
            gateway.max_prompt_tokens("fast", route="verifier"),
            GatewayVerifierModel(gateway, RUN).max_prompt_tokens(),
        )

    assert _run(ScriptedProvider([]), windows, settings=replace(SETTINGS, verifier=small)) == (
        59_000,
        19_000,
        19_000,
    )


def test_without_a_verifier_pair_the_verifier_window_is_the_review_window() -> None:
    async def window(gateway: LlmGateway) -> int:
        return GatewayVerifierModel(gateway, RUN).max_prompt_tokens()

    settings = LlmSettings(primary=replace(REVIEW, context_window=30_000), fallback=None)

    assert _run(ScriptedProvider([]), window, settings=settings) == 29_000


def test_the_reflection_pass_fits_to_the_verifier_route_window() -> None:
    # A verifier window of 100 tokens holds no verifier prompt; the review window would.
    tiny = replace(VERIFIER, context_window=1_100)
    provider = ScriptedProvider([])

    with pytest.raises(RunFailure) as raised:
        _verify(provider, settings=replace(SETTINGS, verifier=tiny))

    assert raised.value.error_code == "verification_context_overflow"
    assert raised.value.message.endswith("limit is 100")
    assert provider.requests == []


# ---------- E-2: no reason in the stored response ----------


def test_the_stored_verifier_answers_carry_no_reason() -> None:
    # The first answer is a JSON object the strict schema rejects (an extra key): repaired.
    extra = {**_decision("c-1"), "note": "an extra key"}
    provider = ScriptedProvider([completion(_answer(extra)), completion(_answer(_decision("c-1")))])

    record = _verify(provider)

    assert provider.kinds() == ["primary", "repair"]
    first, second = (json.loads(content) for content in _stored_contents(provider))
    without_reason = {key: value for key, value in _decision("c-1").items() if key != "reason"}
    assert first == {"decisions": [{**without_reason, "note": "an extra key"}]}
    assert second == {"decisions": [without_reason]}
    # The accepted answer keeps its reason in memory: the decisions are validated with it.
    assert record.decisions[0].reason == _decision("c-1")["reason"]


def test_a_verifier_answer_that_is_not_a_json_object_is_withheld_whole() -> None:
    prose = "Sure: " + _answer(_decision("c-1"))
    provider = ScriptedProvider([completion(prose), completion(_answer(_decision("c-1")))])

    _verify(provider)

    first, _ = _stored_contents(provider)
    assert first == f"[withheld: {len(prose)} characters that are not a JSON object]"


def test_other_tasks_store_the_provider_answer_as_is() -> None:
    answer = json.dumps({"decisions": [{"reason": "kept"}]})
    provider = ScriptedProvider([completion(answer)])
    task = StructuredTask(
        operation="review",
        messages=(ChatMessage("system", "SYS"), ChatMessage("user", "USER")),
        schema=ResponseSchema("Answer", {"type": "object"}),
        validate=lambda text: json.loads(text),
    )

    _run(provider, lambda gateway: gateway.generate(task, RUN))

    assert _stored_contents(provider) == [answer]


# ---------- llm.verify names the accepted manifest ----------


def test_llm_verify_names_the_stored_manifest_of_the_accepted_repair_call() -> None:
    extra = {**_decision("c-1"), "note": "an extra key"}
    provider = ScriptedProvider([completion(_answer(extra)), completion(_answer(_decision("c-1")))])
    store = _Store()

    record = _verify(provider, store=store)

    base, repair = store.entries
    assert (base.manifest.recovery_kind, repair.manifest.recovery_kind) == ("none", "repair")
    assert {entry.manifest.stage for entry in store.entries} == {"verification"}
    assert record.manifest_id == repair.manifest_id
    assert record.accepted_call_id == repair.manifest.call_id
    assert [stored.manifest_id for _, stored in provider.trace.records] == [
        base.manifest_id,
        repair.manifest_id,
    ]


def test_a_verifier_without_a_manifest_store_names_no_manifest() -> None:
    provider = ScriptedProvider([completion(_answer(_decision("c-1")))])

    record = _verify(provider)

    assert record.manifest_id is None
    assert record.accepted_call_id is not None


def test_a_failed_verifier_manifest_write_ends_the_run_with_internal_error() -> None:
    provider = ScriptedProvider([completion(_answer(_decision("c-1")))])

    with pytest.raises(RunFailure) as raised:
        _verify(provider, store=_BrokenStore())

    assert raised.value.error_code == "internal_error"
    assert raised.value.message == "the context manifest could not be stored: OSError"
    assert provider.requests == []
