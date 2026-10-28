"""The verifier model on top of the gateway (#130 PR E, R11).

``GatewayVerifierModel`` sends the verifier prompt through ``LlmGateway`` on the verifier
route: its own model pair, the ``verification`` ledger operation, bounded repair and the
strict ``review/schemas/verifier-output.schema.v1.json``. The candidate-ID and reference
checks of ``ReflectionPass`` run in the gateway on every schema-valid answer, against the
manifest of its call: their errors earn the same repair and fallback within the attempt's
slots (#130, tech lead 2026-10-28). No network: provider answers come from
``httpx.MockTransport``.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

import httpx
import pytest

import app.modules.reviews.infrastructure.llm.gateway as gateway_module
from app.modules.reviews.application.check_runs import RunProviderCheckpoint
from app.modules.reviews.application.llm import (
    LlmCallFailed,
    LlmErrorCode,
    LlmUsage,
    RunCallContext,
)
from app.modules.reviews.application.policy_snapshot import (
    CallManifest,
    PolicySnapshot,
)
from app.modules.reviews.application.prompt_budget import (
    EstimatedTokenCounter,
    LegacyTokenCounter,
    verify_provenance_pre_send_guard,
)
from app.modules.reviews.application.prompt_builder import (
    DEFAULT_BACKEND_V1_CHECKSUM,
    ChangedFile,
    DiffLine,
    PullRequestMeta,
    RepoConventions,
    ReviewContext,
    file_block_id,
)
from app.modules.reviews.application.reflection_pass import (
    CandidateFindingItem,
    ReflectionPass,
    VerificationInvalidOutputError,
    VerifierPromptBuilder,
    VerifierReply,
)
from app.modules.reviews.application.review_output import EvidenceRef, ReviewFindingV2
from app.modules.reviews.application.run_failures import (
    RETRYABLE_ERROR_CODES,
    RunFailure,
    classify_failure,
)
from app.modules.reviews.infrastructure.llm.answers import (
    InvalidAnswer,
    validate_verifier_answer,
)
from app.modules.reviews.infrastructure.llm.ecb_fx import FxQuote, FxQuoteResult
from app.modules.reviews.infrastructure.llm.gateway import LlmGateway, StructuredTask
from app.modules.reviews.infrastructure.llm.models import (
    VERIFICATION_OPERATION,
    GatewayVerifierModel,
)
from app.modules.reviews.infrastructure.llm.settings import LlmSettings, ModelPrice, ModelProfile
from app.modules.reviews.infrastructure.llm.transport import (
    ChatMessage,
    ChatRequest,
    ChatResponse,
    OpenAICompatibleTransport,
    ResponseSchema,
)
from tests.llm_fakes import FakeClock, ScriptedProvider, completion, context_checkpoint
from tests.policy_snapshot_fakes import (
    make_complete_execution_profile,
    make_complete_prompt_bindings,
    make_policy_document_missing,
    make_policy_document_present,
)

NOW = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)
RUN_ID = UUID("00000000-0000-0000-0000-000000013701")
RUN = RunCallContext(
    run_id=RUN_ID,
    workspace_id=UUID("00000000-0000-0000-0000-000000013702"),
    attempt=1,
    engine="fast",
    deadline=NOW + timedelta(seconds=480),
    # The worker's run carries the review prompt; a verifier call must never record it.
    prompt_version_id=UUID("00000000-0000-0000-0000-000000013708"),
)
VERIFIER_PROMPT_ID = UUID("00000000-0000-0000-0000-000000013703")
CALL_ID = UUID("00000000-0000-0000-0000-000000013704")
REVIEW = ModelProfile(
    provider="router",
    base_url="https://router.test/v1",
    model="review-model",
    context_window=100_000,
    max_output_tokens=1_000,
    price=ModelPrice(Decimal("1"), Decimal("2")),
    api_keys=("sk-test-1",),
)
REVIEW_FALLBACK = replace(REVIEW, model="review-fallback-model")
VERIFIER = replace(REVIEW, model="verifier-model")
VERIFIER_FALLBACK = replace(REVIEW, model="verifier-fallback-model")
SETTINGS = LlmSettings(
    primary=REVIEW,
    fallback=REVIEW_FALLBACK,
    verifier=VERIFIER,
    verifier_fallback=VERIFIER_FALLBACK,
)
QUERY_BLOCK = file_block_id("app/query.py")


def _snapshot(
    *, counter_version: str = "counter/v2", verifier_id: UUID | None = VERIFIER_PROMPT_ID
) -> PolicySnapshot:
    return PolicySnapshot(
        schema_version=1,
        snapshot_id=UUID("00000000-0000-0000-0000-000000013705"),
        run_id=RUN_ID,
        repository_id=UUID("00000000-0000-0000-0000-000000013706"),
        policy_base_sha="base_123",
        rules=make_policy_document_present(
            ".review/rules.md", "# Rules\n1. Check SQL injection.\n2. Handle errors.\n"
        ),
        agents=make_policy_document_missing("AGENTS.md"),
        ignore_patterns=(),
        ignore_digest="id_0",
        parser_profile="rules-markdown/v1",
        parser_version="4.0.0",
        matcher_version="review-ignore/v1",
        limits_version="policy-limits/v1",
        builtin_exclusions_version="builtin-exclusions/v1",
        priority_matcher_version="priority-matcher/v1",
        service_defaults_rule_version_id=UUID("00000000-0000-0000-0000-000000013707"),
        defaults_checksum=DEFAULT_BACKEND_V1_CHECKSUM,
        prompt_bindings=replace(make_complete_prompt_bindings(), verifier_id=verifier_id),
        execution_profile=make_complete_execution_profile(counter_version=counter_version),
        policy_digest="digest_123",
    )


def _context(snapshot: PolicySnapshot | None) -> ReviewContext:
    return ReviewContext(
        system="SYSTEM",
        rules=(),
        agents_md=None,
        conventions=RepoConventions((), ()),
        pr_meta=PullRequestMeta("T", None, "u", "b", "main", (), 1, 1, 0, False, False),
        changed_files=(
            ChangedFile("app/query.py", "modified", (DiffLine(10, "added", "query = f'SELECT'"),)),
        ),
        omitted_files=(),
        policy_snapshot=snapshot,
    )


def _candidate(candidate_id: str) -> CandidateFindingItem:
    return CandidateFindingItem(
        candidate_id=candidate_id,
        finding=ReviewFindingV2(
            path="app/query.py",
            line=15,
            start_line=10,
            severity="high",
            category="security",
            title="SQL injection",
            body="Raw query formatting",
            suggestion=None,
            confidence=0.9,
            rule_name=".review/rules.md",
            evidence_refs=[EvidenceRef(block_id=QUERY_BLOCK, start_line=10, end_line=15)],
        ),
    )


def _decision(candidate_id: str, status: str = "supported") -> dict[str, Any]:
    return {
        "candidate_id": candidate_id,
        "status": status,
        "reason": "Direct SQL injection visible on line 10.",
        "evidence_refs": [{"block_id": QUERY_BLOCK, "start_line": 10, "end_line": 10}],
        "requirement_refs": [
            {"block_id": "policy-rules", "start_line": 1, "end_line": 2, "check_id": None}
        ],
    }


def _answer(*decisions: dict[str, Any]) -> str:
    return json.dumps({"decisions": list(decisions)})


def _without_reason(candidate_id: str) -> dict[str, Any]:
    decision = _decision(candidate_id)
    del decision["reason"]
    return decision


class _Provider(ScriptedProvider):
    def run[T](
        self, call: Callable[[LlmGateway], Awaitable[T]], *, settings: LlmSettings = SETTINGS
    ) -> T:
        async def exercise() -> T:
            async with httpx.AsyncClient(transport=httpx.MockTransport(self.handle)) as client:
                return await call(self.gateway(settings, client, FakeClock(NOW)))

        return asyncio.run(exercise())

    def verify(
        self, *, settings: LlmSettings = SETTINGS, context: ReviewContext | None = None
    ) -> VerifierReply:
        context = _context(_snapshot()) if context is None else context
        prompt, manifest = VerifierPromptBuilder().build_verifier_prompt(
            candidates=(_candidate("c-1"),), context=context, call_id=CALL_ID
        )
        return self.run(
            lambda gateway: GatewayVerifierModel(gateway, RUN).verify(
                system_prompt=prompt.system,
                user_prompt=prompt.user,
                manifest=manifest,
                context=context,
            ),
            settings=settings,
        )


@dataclass
class _Guarded:
    """What the gateway hands the pre-send guard before each repair and fallback call."""

    manifests: list[CallManifest] = field(default_factory=list)
    contexts: list[Any] = field(default_factory=list)
    snapshots: list[PolicySnapshot | None] = field(default_factory=list)


@pytest.fixture
def guarded(monkeypatch: pytest.MonkeyPatch) -> _Guarded:
    seen = _Guarded()

    def spy(
        manifest: CallManifest,
        *,
        context: Any = None,
        snapshot: PolicySnapshot | None = None,
    ) -> None:
        seen.manifests.append(manifest)
        seen.contexts.append(context)
        seen.snapshots.append(snapshot)
        verify_provenance_pre_send_guard(manifest, context=context, snapshot=snapshot)

    monkeypatch.setattr(gateway_module, "verify_provenance_pre_send_guard", spy)
    return seen


# ---------- the validator: strict schema, then VerificationOutput ----------


def test_validator_returns_a_valid_answer_as_decoded() -> None:
    assert validate_verifier_answer(_answer(_decision("c-1"))) == {"decisions": [_decision("c-1")]}


def test_validator_reports_a_missing_reason_from_the_schema() -> None:
    with pytest.raises(InvalidAnswer) as caught:
        validate_verifier_answer(_answer(_without_reason("c-1")))

    assert caught.value.errors == ["decisions/0: 'reason' is a required property"]


def test_validator_reports_a_missing_check_id_the_model_cannot_omit() -> None:
    decision = _decision("c-1")
    del decision["requirement_refs"][0]["check_id"]

    with pytest.raises(InvalidAnswer) as caught:
        validate_verifier_answer(_answer(decision))

    assert caught.value.errors == [
        "decisions/0/requirement_refs/0: 'check_id' is a required property"
    ]


def test_validator_applies_verification_output_after_the_schema() -> None:
    # The schema allows an empty evidence list; VerificationOutput does not for "supported".
    decision = dict(_decision("c-1"), evidence_refs=[])

    with pytest.raises(InvalidAnswer) as caught:
        validate_verifier_answer(_answer(decision))

    assert caught.value.errors == [
        "decisions/0: Value error, status 'supported' requires at least 1 evidence_ref"
    ]


def test_validator_rejects_prose_around_the_json() -> None:
    with pytest.raises(InvalidAnswer) as caught:
        validate_verifier_answer("Here it is: " + _answer(_decision("c-1")))

    assert caught.value.errors == ["not valid JSON: Expecting value at line 1"]


# ---------- the adapter on the gateway ----------


def test_a_valid_answer_is_returned_with_the_verifier_model_and_operation() -> None:
    provider = _Provider([completion(_answer(_decision("c-1")))])

    reply = provider.verify()

    assert reply.output == {"decisions": [_decision("c-1")]}
    assert reply.accepted_call_id == CALL_ID
    assert provider.models() == ["verifier-model"]
    assert provider.kinds() == ["primary"]
    assert [usage.operation for _, usage in provider.ledger.events] == ["verification"]
    assert VERIFICATION_OPERATION == "verification"
    assert [record.prompt_version_id for _, record in provider.trace.records] == [
        VERIFIER_PROMPT_ID
    ]
    response_format = provider.bodies()[0]["response_format"]
    assert response_format["json_schema"]["name"] == "VerificationOutput"
    schema = response_format["json_schema"]["schema"]
    assert "$schema" not in schema and "$id" not in schema
    assert schema["required"] == ["decisions"]
    assert schema["$defs"]["VerificationDecision"]["required"] == [
        "candidate_id",
        "status",
        "reason",
        "evidence_refs",
        "requirement_refs",
    ]


def test_a_missing_reason_is_repaired_with_bounded_data(guarded: _Guarded) -> None:
    provider = _Provider(
        [
            completion(_answer(_without_reason("c-1"))),
            completion(_answer(_decision("c-1"))),
        ]
    )

    reply = provider.verify()

    assert provider.kinds() == ["primary", "repair"]
    assert provider.models() == ["verifier-model", "verifier-model"]
    assert [manifest.recovery_kind for manifest in guarded.manifests] == ["repair"]
    assert reply.accepted_call_id == guarded.manifests[0].call_id
    assert reply.accepted_call_id != CALL_ID
    assert [block.block_id for block in guarded.manifests[0].blocks][-1] == "previous-output"
    # bounded repair: the previous answer is quoted in a user turn, never an assistant turn
    repair_roles = [message["role"] for message in provider.bodies()[1]["messages"]]
    assert repair_roles == ["system", "user", "user"]


def test_two_invalid_answers_go_to_the_verifier_fallback(guarded: _Guarded) -> None:
    provider = _Provider(
        [
            completion(_answer(_without_reason("c-1"))),
            completion(_answer(_without_reason("c-1"))),
            completion(_answer(_decision("c-1"))),
        ]
    )

    reply = provider.verify()

    assert provider.kinds() == ["primary", "repair", "fallback"]
    assert provider.models() == ["verifier-model", "verifier-model", "verifier-fallback-model"]
    assert [manifest.recovery_kind for manifest in guarded.manifests] == ["repair", "fallback"]
    assert all(manifest.stage == "verification" for manifest in guarded.manifests)
    assert reply.accepted_call_id == guarded.manifests[1].call_id
    assert [usage.operation for _, usage in provider.ledger.events] == ["verification"] * 3


def _repaired_once() -> _Provider:
    return _Provider(
        [
            completion(_answer(_without_reason("c-1"))),
            completion(_answer(_decision("c-1"))),
        ]
    )


def test_repair_and_fallback_guards_get_the_review_context_and_its_snapshot(
    guarded: _Guarded,
) -> None:
    context = _context(_snapshot())
    provider = _Provider(
        [
            completion(_answer(_without_reason("c-1"))),
            completion(_answer(_without_reason("c-1"))),
            completion(_answer(_decision("c-1"))),
        ]
    )

    provider.verify(context=context)

    assert [manifest.recovery_kind for manifest in guarded.manifests] == ["repair", "fallback"]
    assert [item is context for item in guarded.contexts] == [True, True]
    assert [item is context.policy_snapshot for item in guarded.snapshots] == [True, True]
    assert [record.prompt_version_id for _, record in provider.trace.records] == [
        VERIFIER_PROMPT_ID
    ] * 3


def test_a_counter_v1_snapshot_estimates_every_call_with_counter_v1() -> None:
    provider = _repaired_once()

    provider.verify(context=_context(_snapshot(counter_version="counter/v1")))

    def counted(counter: LegacyTokenCounter | EstimatedTokenCounter) -> list[int]:
        return [
            sum(counter.count(message["content"]) for message in body["messages"])
            for body in provider.bodies()
        ]

    assert [record.input_tokens_estimate for _, record in provider.trace.records] == counted(
        LegacyTokenCounter()
    )
    # counter/v2 equals the verifier profile's heuristic: only counter/v1 shows the counter.
    assert counted(LegacyTokenCounter()) != counted(EstimatedTokenCounter())


def test_a_snapshot_without_a_verifier_prompt_records_no_prompt_version(
    guarded: _Guarded,
) -> None:
    # Decided by the coordinator: None, never the review prompt the run carries.
    context = _context(_snapshot(verifier_id=None))
    provider = _repaired_once()

    provider.verify(context=context)

    assert RUN.prompt_version_id is not None
    assert [record.prompt_version_id for _, record in provider.trace.records] == [None, None]
    assert [item is context for item in guarded.contexts] == [True]
    assert [item is context.policy_snapshot for item in guarded.snapshots] == [True]


def test_without_a_snapshot_the_verifier_records_no_prompt_version(guarded: _Guarded) -> None:
    context = _context(None)
    provider = _repaired_once()

    reply = provider.verify(context=context)

    assert reply.output == {"decisions": [_decision("c-1")]}
    assert provider.models() == ["verifier-model", "verifier-model"]
    assert [record.prompt_version_id for _, record in provider.trace.records] == [None, None]
    assert [item is context for item in guarded.contexts] == [True]
    assert guarded.snapshots == [None]


def test_the_verifier_price_is_checked_against_the_run_cost_limit() -> None:
    # 1 000 reserved output tokens at $250/M cost $0.25; with $0.30 spent that is over $0.50.
    costly = replace(VERIFIER, price=ModelPrice(Decimal("0"), Decimal("250")))
    spent = LlmUsage(
        provider="router",
        model="review-model",
        operation="review",
        tokens_in=10,
        tokens_out=5,
        cache_read_tokens=0,
        cost_usd=Decimal("0.30"),
    )
    provider = _Provider([])
    provider.ledger.events.append((RUN, spent))

    with pytest.raises(LlmCallFailed) as caught:
        provider.verify(settings=replace(SETTINGS, verifier=costly))

    assert caught.value.error_code is LlmErrorCode.BUDGET_EXCEEDED
    assert str(caught.value) == (
        "budget_exceeded: run spent $0.30 and the next call may cost $0.250000, limit $0.50"
    )
    assert provider.requests == []


def test_the_review_price_passes_the_same_cost_check() -> None:
    # Control for the test above: without a verifier pair the cheap review model answers.
    spent = LlmUsage(
        provider="router",
        model="review-model",
        operation="review",
        tokens_in=10,
        tokens_out=5,
        cache_read_tokens=0,
        cost_usd=Decimal("0.30"),
    )
    provider = _Provider([completion(_answer(_decision("c-1")))])
    provider.ledger.events.append((RUN, spent))

    provider.verify(settings=LlmSettings(primary=REVIEW, fallback=REVIEW_FALLBACK))

    assert provider.models() == ["review-model"]


def test_reflection_pass_verifies_two_candidates_through_the_gateway() -> None:
    provider = _Provider(
        [completion(_answer(_decision("c-1"), _decision("c-2", status="contradicted")))]
    )

    async def verify(gateway: LlmGateway) -> list[tuple[str, str]]:
        reflection = ReflectionPass(GatewayVerifierModel(gateway, RUN))
        decisions = await reflection.verify(
            candidates=(_candidate("c-1"), _candidate("c-2")), context=_context(_snapshot())
        )
        return [(decision.candidate_id, decision.status) for decision in decisions]

    assert provider.run(verify) == [("c-1", "supported"), ("c-2", "contradicted")]
    assert provider.models() == ["verifier-model"]
    assert [usage.operation for _, usage in provider.ledger.events] == ["verification"]


# ---------- reference errors earn the bounded recovery (#130 hotfix, tech lead 28.10) ----------


def _unshown(candidate_id: str) -> dict[str, Any]:
    """A decision citing lines the diff block never showed (it shows line 10 only)."""
    return dict(
        _decision(candidate_id),
        evidence_refs=[{"block_id": QUERY_BLOCK, "start_line": 22, "end_line": 24}],
    )


def _verify_one(context: ReviewContext | None = None) -> Callable[[LlmGateway], Awaitable[Any]]:
    async def verify(gateway: LlmGateway) -> Any:
        return await ReflectionPass(GatewayVerifierModel(gateway, RUN)).verify_recorded(
            candidates=(_candidate("c-1"),),
            context=_context(_snapshot()) if context is None else context,
        )

    return verify


def test_reference_errors_earn_the_repair_then_the_fallback(guarded: _Guarded) -> None:
    # PIPELINE_SPEC :632, TEST_PLAN :139: a range the manifest does not show, then a decision
    # for an id that was never sent; the fallback's answer is accepted.
    provider = _Provider(
        [
            completion(_answer(_unshown("c-1"))),
            completion(_answer(_decision("c-9"))),
            completion(_answer(_decision("c-1"))),
        ]
    )

    record = provider.run(_verify_one())

    assert provider.kinds() == ["primary", "repair", "fallback"]
    assert provider.models() == ["verifier-model", "verifier-model", "verifier-fallback-model"]
    assert [usage.operation for _, usage in provider.ledger.events] == ["verification"] * 3
    assert [manifest.recovery_kind for manifest in guarded.manifests] == ["repair", "fallback"]
    assert record.accepted_call_id == guarded.manifests[1].call_id
    assert [(decision.candidate_id, decision.status) for decision in record.decisions] == [
        ("c-1", "supported")
    ]
    # The repair turn quotes the reference error in the escaped, bounded validation errors.
    repair_turn = provider.bodies()[1]["messages"][-1]["content"]
    assert (
        "<validation_errors>\n"
        f"- Decision c-1 evidence_ref {QUERY_BLOCK}:22-24 is not shown in verifier manifest\n"
        "</validation_errors>"
    ) in repair_turn


def test_the_repair_turn_names_the_missing_and_unknown_candidate_ids() -> None:
    provider = _Provider(
        [completion(_answer(_decision("c-9"))), completion(_answer(_decision("c-1")))]
    )

    record = provider.run(_verify_one())

    assert provider.kinds() == ["primary", "repair"]
    assert [decision.candidate_id for decision in record.decisions] == ["c-1"]
    assert (
        "<validation_errors>\n"
        "- Verifier returned decisions for 1 items, expected 1: missing c-1; unknown c-9\n"
        "</validation_errors>"
    ) in provider.bodies()[1]["messages"][-1]["content"]


def test_a_rule_finding_without_its_requirement_earns_the_repair() -> None:
    # RULES_FORMAT_SPEC §8.2: a supported or contradicted finding with rule_name cites its rule.
    contradicted = dict(_decision("c-1", status="contradicted"), requirement_refs=[])
    provider = _Provider([completion(_answer(contradicted)), completion(_answer(_decision("c-1")))])

    record = provider.run(_verify_one())

    assert provider.kinds() == ["primary", "repair"]
    assert [decision.status for decision in record.decisions] == ["supported"]
    assert (
        "- Decision c-1 for candidate with rule_name '.review/rules.md' requires at least one "
        "matching requirement_ref for this rule\n"
    ) in provider.bodies()[1]["messages"][-1]["content"]


def test_three_invalid_references_fail_with_verification_invalid_output() -> None:
    provider = _Provider([completion(_answer(_unshown("c-1"))) for _ in range(3)])

    with pytest.raises(VerificationInvalidOutputError) as caught:
        provider.run(_verify_one())

    assert provider.kinds() == ["primary", "repair", "fallback"]
    # Terminal: no run retry.
    failure = classify_failure(caught.value)
    assert failure.error_code == "verification_invalid_output"
    assert failure.error_code not in RETRYABLE_ERROR_CODES


def test_reference_errors_are_logged_without_the_answer_text(
    caplog: pytest.LogCaptureFixture,
) -> None:
    provider = _Provider(
        [completion(_answer(_unshown("c-1"))), completion(_answer(_decision("c-1")))]
    )

    with caplog.at_level(logging.INFO, logger=gateway_module.__name__):
        provider.run(_verify_one())

    [record] = [
        record
        for record in caplog.records
        if record.getMessage().startswith("llm answer failed the manifest check")
    ]
    errors = [f"Decision c-1 evidence_ref {QUERY_BLOCK}:22-24 is not shown in verifier manifest"]
    assert record.levelno == logging.INFO
    assert {key: record.__dict__[key] for key in ("run_id", "operation", "errors")} == {
        "run_id": str(RUN_ID),
        "operation": "verification",
        "errors": errors,
    }
    # The default formatter drops extras: the message itself carries the errors.
    assert json.dumps(errors) in record.getMessage()
    # Ids and line numbers only: the answer's reason is in no log record.
    reason = _decision("c-1")["reason"]
    assert reason not in record.getMessage()
    assert not any(reason in str(vars(record)) for record in caplog.records)


def test_a_reference_error_without_a_free_slot_fails_without_recovery() -> None:
    # C1 -> R1 -> R2 fallback -> V1: the verifier's primary is the attempt's fourth and last
    # call (max_calls_per_attempt=4), so its invalid answer has no repair and no fallback.
    spent = StructuredTask(
        operation="review",
        messages=(ChatMessage("user", "spend one slot"),),
        schema=ResponseSchema("Spent", {"type": "object"}),
        validate=lambda content: dict(json.loads(content)),
    )
    provider = _Provider(
        [*(completion("{}") for _ in range(3)), completion(_answer(_unshown("c-1")))]
    )

    async def verify(gateway: LlmGateway) -> Any:
        for _ in range(3):
            await gateway.generate(spent, RUN)
        return await _verify_one()(gateway)

    with pytest.raises(VerificationInvalidOutputError) as caught:
        provider.run(verify)

    assert provider.kinds() == ["primary"] * 4
    assert [usage.operation for _, usage in provider.ledger.events] == [
        "review",
        "review",
        "review",
        "verification",
    ]
    assert classify_failure(caught.value).error_code == "verification_invalid_output"


def test_a_repair_answer_is_checked_against_the_repair_manifest() -> None:
    # CVS §5.2: refs of a repair resolve against its own manifest, where the previous answer
    # is a previous_output block and never code evidence. Without a verifier fallback the
    # repair's own error ends the verification.
    citing_previous = dict(
        _decision("c-1"),
        evidence_refs=[{"block_id": "previous-output", "start_line": 1, "end_line": 1}],
    )
    provider = _Provider(
        [completion(_answer(_unshown("c-1"))), completion(_answer(citing_previous))]
    )
    settings = LlmSettings(primary=REVIEW, fallback=None, verifier=VERIFIER)

    with pytest.raises(VerificationInvalidOutputError) as caught:
        provider.run(_verify_one(), settings=settings)

    assert provider.kinds() == ["primary", "repair"]
    assert isinstance(caught.value.__cause__, LlmCallFailed)
    assert str(caught.value.__cause__) == (
        "llm_invalid_output: Decision c-1 evidence_ref previous-output has non-code role "
        "'previous_output'"
    )


def _paid(content: str, *, completion_tokens: int) -> httpx.Response:
    """An answer whose usage reports ``completion_tokens`` output tokens."""
    return httpx.Response(
        200,
        json={
            "model": "answering-model",
            "choices": [{"message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": completion_tokens},
        },
    )


def test_a_reference_error_without_time_for_recovery_fails_as_invalid_output() -> None:
    # CVS §7.3: when the verifier cannot recover, its last error keeps its class. The primary
    # call takes 390 s of the 480 s attempt: 90 s are left, under the 90 s call and the 10 s
    # of local completion, so no repair or fallback is sent.
    clock = FakeClock(NOW)
    provider = _Provider([completion(_answer(_unshown("c-1")))])

    def slow(request: httpx.Request) -> httpx.Response:
        clock.current += timedelta(seconds=390)
        return provider.handle(request)

    async def exercise() -> Any:
        async with httpx.AsyncClient(transport=httpx.MockTransport(slow)) as client:
            return await _verify_one()(provider.gateway(SETTINGS, client, clock))

    with pytest.raises(VerificationInvalidOutputError) as caught:
        asyncio.run(exercise())

    assert provider.kinds() == ["primary"]
    assert classify_failure(caught.value).error_code == "verification_invalid_output"


def test_a_repair_sent_on_a_rejected_key_keeps_its_deadline_class() -> None:
    # CVS §7.3: the run keeps the class of the last call that was made. The repair's first
    # key answers 429 after 390 s of the 480 s attempt; its request went out, so the refusal
    # before the second key is the repair's own verification_deadline_exceeded.
    clock = FakeClock(NOW)
    rejected = httpx.Response(429, json={"error": {"message": "rate limited"}})
    provider = _Provider([completion(_answer(_unshown("c-1"))), rejected])
    settings = replace(SETTINGS, verifier=replace(VERIFIER, api_keys=("sk-a", "sk-b")))

    def slow(request: httpx.Request) -> httpx.Response:
        response = provider.handle(request)
        if response is rejected:
            clock.current += timedelta(seconds=390)
        return response

    async def exercise() -> Any:
        async with httpx.AsyncClient(transport=httpx.MockTransport(slow)) as client:
            return await _verify_one()(provider.gateway(settings, client, clock))

    with pytest.raises(RunFailure) as caught:
        asyncio.run(exercise())

    # The primary and the repair were both sent on the first key; the second never was.
    assert [request.headers["Authorization"] for request in provider.requests] == [
        "Bearer sk-a",
        "Bearer sk-a",
    ]
    assert classify_failure(caught.value).error_code == "verification_deadline_exceeded"


def test_a_repair_refused_before_its_first_key_keeps_the_invalid_answer_class() -> None:
    # The same refusal at the transport's first checkpoint: nothing of the repair was sent.
    clock = FakeClock(NOW)
    provider = _Provider([completion(_answer(_unshown("c-1")))])
    completes = 0

    class _LateTransport(OpenAICompatibleTransport):
        """Spends 390 s between the gateway's pre-send checks and the repair's first key."""

        async def complete(
            self, request: ChatRequest, *, checkpoint: RunProviderCheckpoint
        ) -> ChatResponse:
            nonlocal completes
            completes += 1
            if completes == 2:
                clock.current += timedelta(seconds=390)
            return await super().complete(request, checkpoint=checkpoint)

    async def exercise() -> Any:
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider.handle)) as client:
            gateway = LlmGateway(
                SETTINGS,
                _LateTransport(client),
                provider.ledger,
                provider.trace,
                checkpoint=context_checkpoint,
                clock=clock,
                sleep=clock.sleep,
                jitter=lambda: 0.0,
                monotonic=clock.monotonic,
            )
            return await _verify_one()(gateway)

    with pytest.raises(VerificationInvalidOutputError) as caught:
        asyncio.run(exercise())

    assert (completes, len(provider.requests)) == (2, 1)
    assert classify_failure(caught.value).error_code == "verification_invalid_output"


def test_a_reference_error_without_budget_for_recovery_fails_as_invalid_output() -> None:
    # 1 000 reserved output tokens at $250/M cost $0.25: $0.01 spent and the primary's $0.25
    # leave no room for a repair under the $0.50 fast limit.
    costly = replace(VERIFIER, price=ModelPrice(Decimal("0"), Decimal("250")))
    spent = LlmUsage(
        provider="router",
        model="review-model",
        operation="review",
        tokens_in=10,
        tokens_out=5,
        cost_usd=Decimal("0.01"),
        cache_read_tokens=0,
    )
    provider = _Provider([_paid(_answer(_unshown("c-1")), completion_tokens=1_000)])
    provider.ledger.events.append((RUN, spent))

    with pytest.raises(VerificationInvalidOutputError) as caught:
        provider.run(_verify_one(), settings=replace(SETTINGS, verifier=costly))

    assert provider.kinds() == ["primary"]
    assert classify_failure(caught.value).error_code == "verification_invalid_output"


class _QuoteOnce:
    """The ECB quote of the primary call; no quote for any call after it."""

    def __init__(self) -> None:
        self.quotes = 0

    async def get_quote(self) -> FxQuoteResult:
        self.quotes += 1
        if self.quotes > 1:
            return FxQuoteResult(None, stale_cache=False)
        quote = FxQuote(Decimal("1.1"), NOW.date(), "EXR.D.USD.EUR.SP00.A", NOW)
        return FxQuoteResult(quote, stale_cache=False)


def test_a_reference_error_without_an_ecb_quote_for_recovery_fails_as_invalid_output() -> None:
    # An EUR-priced verifier route: the repair cannot be priced, so it is never sent, and the
    # invalid answer keeps its class (CVS §7.3), not a retryable verification_unavailable.
    eur = replace(VERIFIER, base_url="https://api.eurouter.ai/api/v1")
    settings = replace(
        SETTINGS, verifier=eur, verifier_fallback=replace(eur, model="verifier-fallback-model")
    )
    provider = _Provider([completion(_answer(_unshown("c-1")))])
    fx = _QuoteOnce()

    async def exercise() -> Any:
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
                fx_provider=fx,
            )
            return await _verify_one()(gateway)

    with pytest.raises(VerificationInvalidOutputError) as caught:
        asyncio.run(exercise())

    assert provider.kinds() == ["primary"]
    assert fx.quotes == 2
    assert classify_failure(caught.value).error_code == "verification_invalid_output"


def test_a_schema_error_without_time_for_recovery_fails_as_invalid_output() -> None:
    # PIPELINE_SPEC §4.5, §5.1, CVS §7.3: the same for a schema error; before, a repair refused
    # for lack of time failed with verification_deadline_exceeded.
    clock = FakeClock(NOW)
    provider = _Provider([completion(_answer(_without_reason("c-1")))])

    def slow(request: httpx.Request) -> httpx.Response:
        # 390 s of the 480 s attempt: under the 90 s call and the 10 s of local completion.
        clock.current += timedelta(seconds=390)
        return provider.handle(request)

    async def exercise() -> Any:
        async with httpx.AsyncClient(transport=httpx.MockTransport(slow)) as client:
            return await _verify_one()(provider.gateway(SETTINGS, client, clock))

    with pytest.raises(VerificationInvalidOutputError) as caught:
        asyncio.run(exercise())

    assert provider.kinds() == ["primary"]
    assert classify_failure(caught.value).error_code == "verification_invalid_output"
