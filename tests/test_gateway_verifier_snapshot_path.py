"""The verifier's gateway calls on the snapshot path (#130 F1-7).

- ``LlmGateway.max_prompt_tokens(engine, route)`` and ``GatewayVerifierModel`` name the
  verifier route's prompt window; ``ReflectionPass`` fits to it (R10).
- A stored verifier ``llm.call`` response carries no ``decisions[].reason`` (E-2, TL-3),
  fail-closed (decided by the tech lead on 2026-10-25): each choice keeps its index, finish
  reason and a message of role and answer text; the answer text is the allow-list of a valid
  verifier answer (candidate_id, status and the refs of each decision), or it is withheld
  whole. Review and conventions responses stay as the provider sent them.
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
from app.modules.reviews.infrastructure.llm.answers import verifier_answer_without_reasons
from app.modules.reviews.infrastructure.llm.gateway import (
    LlmGateway,
    StructuredTask,
    _redacted_body,
)
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
    QUERY_BLOCK,
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

# A reason the model wrote; it must not reach any stored llm.call response.
SENTINEL = "SENTINEL-REASON-7f3a"
SENTINEL_ANSWER = _answer({**_decision("c-1"), "reason": SENTINEL})
# The allow-list of SENTINEL_ANSWER: every decision field but the reason.
STORED_DECISIONS = {
    "decisions": [
        {
            "candidate_id": "c-1",
            "status": "supported",
            "evidence_refs": [{"block_id": QUERY_BLOCK, "start_line": 10, "end_line": 10}],
            "requirement_refs": [
                {"block_id": "policy-rules", "start_line": 1, "end_line": 2, "check_id": None}
            ],
        }
    ]
}
USAGE = {"prompt_tokens": 10, "completion_tokens": 5}


def _withheld(answer: str) -> str:
    return f"[withheld: {len(answer)} characters that are not a valid verifier answer]"


def _stored_body(body: dict[str, object]) -> object:
    return _redacted_body(body, verifier_answer_without_reasons)


def _body(*choices: dict[str, object], **top: object) -> dict[str, object]:
    return {
        "id": "gen-1",
        "model": "answering-model",
        "usage": USAGE,
        **top,
        "choices": list(choices),
    }


def _choice(content: object, **message: object) -> dict[str, object]:
    return {
        "index": 0,
        "finish_reason": "stop",
        "message": {"role": "assistant", "content": content, **message},
    }


STORED_CHOICE = {
    "index": 0,
    "finish_reason": "stop",
    "message": {"role": "assistant", "content": json.dumps(STORED_DECISIONS)},
}


@pytest.mark.parametrize(
    "choice",
    [
        pytest.param(_choice([{"type": "text", "text": SENTINEL_ANSWER}]), id="list-form content"),
        pytest.param(_choice(SENTINEL_ANSWER, reasoning_content=SENTINEL), id="reasoning_content"),
        pytest.param(_choice(SENTINEL_ANSWER, reasoning=SENTINEL), id="reasoning"),
        pytest.param(
            _choice(
                SENTINEL_ANSWER,
                tool_calls=[
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {"name": "note", "arguments": json.dumps({"why": SENTINEL})},
                    }
                ],
            ),
            id="tool_calls",
        ),
        pytest.param(_choice(SENTINEL_ANSWER, refusal=SENTINEL), id="refusal"),
        pytest.param({**_choice(SENTINEL_ANSWER), "text": SENTINEL}, id="choice text"),
        pytest.param(
            {**_choice(SENTINEL_ANSWER), "logprobs": {"content": [{"token": SENTINEL}]}},
            id="logprobs",
        ),
    ],
)
def test_a_stored_verifier_choice_keeps_only_the_allow_listed_fields(
    choice: dict[str, object],
) -> None:
    stored = _stored_body(_body(choice))

    assert stored == _body(STORED_CHOICE)
    assert SENTINEL not in json.dumps(stored)


def test_the_top_level_of_a_stored_verifier_body_keeps_only_provider_metadata() -> None:
    body = _body(_choice(SENTINEL_ANSWER), created=1, object="chat.completion", output=SENTINEL)

    assert _stored_body(body) == _body(STORED_CHOICE, created=1, object="chat.completion")


def test_a_redacted_body_that_is_not_an_object_or_has_no_choice_list_keeps_no_text() -> None:
    assert _redacted_body([SENTINEL], verifier_answer_without_reasons) is None
    body: dict[str, object] = {"id": "gen-1", "choices": {"0": {"message": {"content": SENTINEL}}}}
    assert _stored_body(body) == {"id": "gen-1", "choices": []}


def test_every_choice_of_a_stored_verifier_body_is_allow_listed() -> None:
    prose = f"I think so, because {SENTINEL}."
    body = _body(
        _choice(SENTINEL_ANSWER),
        {**_choice([{"type": "text", "text": SENTINEL_ANSWER}]), "index": 1},
        {**_choice(prose, reasoning_content=SENTINEL), "index": 2},
    )

    stored = _stored_body(body)

    assert stored == _body(
        STORED_CHOICE,
        {**STORED_CHOICE, "index": 1},
        {
            "index": 2,
            "finish_reason": "stop",
            "message": {"role": "assistant", "content": _withheld(prose)},
        },
    )
    assert SENTINEL not in json.dumps(stored)


def test_a_choice_without_answer_text_stores_no_content() -> None:
    choice = _choice(None, tool_calls=[{"function": {"arguments": SENTINEL}}])

    assert _stored_body(_body(choice)) == _body(
        {"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": None}}
    )


@pytest.mark.parametrize(
    "answer",
    [
        pytest.param(
            json.dumps({"decisions": {"candidate_id": "c-1", "reason": SENTINEL}}),
            id="decisions as an object",
        ),
        pytest.param(
            json.dumps({"result": {"decisions": [{**_decision("c-1"), "reason": SENTINEL}]}}),
            id="nested decisions",
        ),
        pytest.param(
            _answer({**_decision("c-1"), "Reason": SENTINEL}), id="a capitalised Reason key"
        ),
        pytest.param(
            json.dumps({"decisions": [_decision("c-1")], "reason": SENTINEL}),
            id="a top-level reason",
        ),
        pytest.param(_answer({**_decision("c-1"), "note": SENTINEL}), id="an extra key"),
        pytest.param(
            _answer({**_decision("c-1"), "status": "maybe", "reason": SENTINEL}),
            id="a status outside the schema",
        ),
        pytest.param(f"Sure: {SENTINEL_ANSWER}", id="text around the object"),
        pytest.param(json.dumps([SENTINEL]), id="a JSON array"),
    ],
)
def test_an_answer_that_is_not_a_valid_verifier_answer_is_withheld_whole(answer: str) -> None:
    assert verifier_answer_without_reasons(answer) == _withheld(answer)


def test_the_stored_verifier_answers_carry_no_reason() -> None:
    # The first answer is a JSON object the strict schema rejects (an extra key): repaired.
    invalid = _answer({**_decision("c-1"), "note": SENTINEL})
    provider = ScriptedProvider([completion(invalid), completion(SENTINEL_ANSWER)])

    record = _verify(provider)

    assert provider.kinds() == ["primary", "repair"]
    first, second = _stored_contents(provider)
    assert first == _withheld(invalid)
    assert json.loads(second) == STORED_DECISIONS
    # The accepted answer keeps its reason in memory: the decisions are validated with it.
    assert record.decisions[0].reason == SENTINEL


def test_a_stored_verifier_response_keeps_no_reason_anywhere() -> None:
    # List-form answer text, a reasoning trace and a second choice, through the gateway.
    body = _body(
        _choice([{"type": "text", "text": SENTINEL_ANSWER}], reasoning_content=SENTINEL),
        {**_choice(SENTINEL_ANSWER, refusal=SENTINEL), "index": 1},
    )
    provider = ScriptedProvider([httpx.Response(200, json=body)])

    record = _verify(provider)

    assert record.decisions[0].reason == SENTINEL
    (_, stored), *_ = provider.trace.records
    assert stored.response_json() == _body(STORED_CHOICE, {**STORED_CHOICE, "index": 1})
    assert SENTINEL not in json.dumps([item.response_json() for _, item in provider.trace.records])


def test_a_verifier_answer_that_is_not_a_json_object_is_withheld_whole() -> None:
    prose = "Sure: " + _answer(_decision("c-1"))
    provider = ScriptedProvider([completion(prose), completion(_answer(_decision("c-1")))])

    _verify(provider)

    first, _ = _stored_contents(provider)
    assert first == _withheld(prose)


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
    # CVS §5.1: the verifier stage's own prompt pin, not the review prompt's.
    verifier_checksum = _snapshot().prompt_bindings.verifier_checksum
    assert {entry.prompt_checksum for entry in store.entries} == {verifier_checksum}
    assert verifier_checksum != _snapshot().prompt_bindings.review_system_checksum
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


# ---------- answers the JSON decoder cannot read (re-review of 172c8c5c) ----------

DEEP_NESTING = "[" * 20_000  # json.loads: RecursionError
HUGE_INTEGER = '{"decisions": ' + "1" * 5_000 + "}"  # json.loads: ValueError (int digit limit)


@pytest.mark.parametrize(
    "answer",
    [pytest.param(DEEP_NESTING, id="deep nesting"), pytest.param(HUGE_INTEGER, id="huge integer")],
)
def test_an_answer_the_decoder_cannot_read_is_withheld_and_repaired(answer: str) -> None:
    provider = ScriptedProvider([completion(answer), completion(_answer(_decision("c-1")))])

    record = _verify(provider)

    assert [(decision.candidate_id, decision.status) for decision in record.decisions] == [
        ("c-1", "supported")
    ]
    assert provider.kinds() == ["primary", "repair"]
    assert _stored_contents(provider)[0] == _withheld(answer)
    assert verifier_answer_without_reasons(answer) == _withheld(answer)


@pytest.mark.parametrize(
    "content",
    [pytest.param({"text": SENTINEL}, id="an object"), pytest.param(7, id="a number")],
)
def test_a_choice_whose_content_is_not_text_stores_no_content(content: object) -> None:
    assert _stored_body(_body(_choice(content))) == _body(
        {"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": None}}
    )
