"""The verifier model on top of the gateway (#130 PR E, R11).

``GatewayVerifierModel`` sends the verifier prompt through ``LlmGateway`` on the verifier
route: its own model pair, the ``verification`` ledger operation, bounded repair and the
strict ``review/schemas/verifier-output.schema.v1.json``. The candidate-ID and reference
checks stay in ``ReflectionPass``. No network: provider answers come from
``httpx.MockTransport``.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

import httpx
import pytest

import app.modules.reviews.infrastructure.llm.gateway as gateway_module
from app.modules.reviews.application.llm import (
    LlmCallFailed,
    LlmErrorCode,
    LlmUsage,
    RunCallContext,
)
from app.modules.reviews.application.policy_snapshot import (
    CallManifest,
    PolicySnapshot,
    make_complete_execution_profile,
    make_complete_prompt_bindings,
    make_policy_document_missing,
    make_policy_document_present,
)
from app.modules.reviews.application.prompt_budget import verify_provenance_pre_send_guard
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
    VerifierPromptBuilder,
    VerifierReply,
)
from app.modules.reviews.application.review_output import EvidenceRef, ReviewFindingV2
from app.modules.reviews.infrastructure.llm.answers import (
    InvalidAnswer,
    validate_verifier_answer,
)
from app.modules.reviews.infrastructure.llm.gateway import LlmGateway
from app.modules.reviews.infrastructure.llm.memory import InMemoryLlmCallTrace, InMemoryUsageLedger
from app.modules.reviews.infrastructure.llm.models import (
    VERIFICATION_OPERATION,
    GatewayVerifierModel,
)
from app.modules.reviews.infrastructure.llm.settings import LlmSettings, ModelPrice, ModelProfile
from app.modules.reviews.infrastructure.llm.transport import OpenAICompatibleTransport
from tests.llm_fakes import context_checkpoint

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


def _snapshot(*, verifier_id: UUID | None = VERIFIER_PROMPT_ID) -> PolicySnapshot:
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
        execution_profile=make_complete_execution_profile(),
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


def _completion(content: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "model": "answering-model",
            "choices": [{"message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        },
    )


class _Clock:
    def __init__(self) -> None:
        self.current = NOW

    def now(self) -> datetime:
        return self.current

    def monotonic(self) -> float:
        return (self.current - NOW).total_seconds()

    async def sleep(self, seconds: float) -> None:
        self.current += timedelta(seconds=seconds)


@dataclass
class _Provider:
    replies: list[httpx.Response]
    requests: list[httpx.Request] = field(default_factory=list)
    ledger: InMemoryUsageLedger = field(default_factory=InMemoryUsageLedger)
    trace: InMemoryLlmCallTrace = field(default_factory=InMemoryLlmCallTrace)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if not self.replies:
            raise AssertionError("unexpected provider call")
        return self.replies.pop(0)

    def run[T](
        self, call: Callable[[LlmGateway], Awaitable[T]], *, settings: LlmSettings = SETTINGS
    ) -> T:
        async def exercise() -> T:
            clock = _Clock()
            async with httpx.AsyncClient(transport=httpx.MockTransport(self._handle)) as client:
                gateway = LlmGateway(
                    settings,
                    OpenAICompatibleTransport(client),
                    self.ledger,
                    self.trace,
                    checkpoint=context_checkpoint,
                    clock=clock,
                    sleep=clock.sleep,
                    jitter=lambda: 0.0,
                    monotonic=clock.monotonic,
                )
                return await call(gateway)

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

    def bodies(self) -> list[dict[str, Any]]:
        return [json.loads(request.content) for request in self.requests]

    def models(self) -> list[str]:
        return [body["model"] for body in self.bodies()]

    def kinds(self) -> list[str]:
        return [record.kind.value for _, record in self.trace.records]


@pytest.fixture
def manifests(monkeypatch: pytest.MonkeyPatch) -> list[CallManifest]:
    """Every repair and fallback manifest the gateway checks before sending."""
    seen: list[CallManifest] = []

    def spy(
        manifest: CallManifest,
        *,
        context: Any = None,
        snapshot: PolicySnapshot | None = None,
    ) -> None:
        seen.append(manifest)
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
    provider = _Provider([_completion(_answer(_decision("c-1")))])

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


def test_a_missing_reason_is_repaired_with_bounded_data(manifests: list[CallManifest]) -> None:
    provider = _Provider(
        [
            _completion(_answer(_without_reason("c-1"))),
            _completion(_answer(_decision("c-1"))),
        ]
    )

    reply = provider.verify()

    assert provider.kinds() == ["primary", "repair"]
    assert provider.models() == ["verifier-model", "verifier-model"]
    assert [manifest.recovery_kind for manifest in manifests] == ["repair"]
    assert reply.accepted_call_id == manifests[0].call_id
    assert reply.accepted_call_id != CALL_ID
    assert [block.block_id for block in manifests[0].blocks][-1] == "previous-output"
    # bounded repair: the previous answer is quoted in a user turn, never an assistant turn
    repair_roles = [message["role"] for message in provider.bodies()[1]["messages"]]
    assert repair_roles == ["system", "user", "user"]


def test_two_invalid_answers_go_to_the_verifier_fallback(manifests: list[CallManifest]) -> None:
    provider = _Provider(
        [
            _completion(_answer(_without_reason("c-1"))),
            _completion(_answer(_without_reason("c-1"))),
            _completion(_answer(_decision("c-1"))),
        ]
    )

    reply = provider.verify()

    assert provider.kinds() == ["primary", "repair", "fallback"]
    assert provider.models() == ["verifier-model", "verifier-model", "verifier-fallback-model"]
    assert [manifest.recovery_kind for manifest in manifests] == ["repair", "fallback"]
    assert all(manifest.stage == "verification" for manifest in manifests)
    assert reply.accepted_call_id == manifests[1].call_id
    assert [usage.operation for _, usage in provider.ledger.events] == ["verification"] * 3


def _repaired_once() -> _Provider:
    return _Provider(
        [
            _completion(_answer(_without_reason("c-1"))),
            _completion(_answer(_decision("c-1"))),
        ]
    )


def test_a_snapshot_without_a_verifier_prompt_records_no_prompt_version() -> None:
    # Decided by the coordinator: None, never the review prompt the run carries.
    context = _context(_snapshot(verifier_id=None))
    provider = _repaired_once()

    provider.verify(context=context)

    assert RUN.prompt_version_id is not None
    assert [record.prompt_version_id for _, record in provider.trace.records] == [None, None]


def test_without_a_snapshot_the_verifier_records_no_prompt_version() -> None:
    context = _context(None)
    provider = _repaired_once()

    reply = provider.verify(context=context)

    assert reply.output == {"decisions": [_decision("c-1")]}
    assert provider.models() == ["verifier-model", "verifier-model"]
    assert [record.prompt_version_id for _, record in provider.trace.records] == [None, None]


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
    provider = _Provider([_completion(_answer(_decision("c-1")))])
    provider.ledger.events.append((RUN, spent))

    provider.verify(settings=LlmSettings(primary=REVIEW, fallback=REVIEW_FALLBACK))

    assert provider.models() == ["review-model"]


def test_reflection_pass_verifies_two_candidates_through_the_gateway() -> None:
    provider = _Provider(
        [_completion(_answer(_decision("c-1"), _decision("c-2", status="contradicted")))]
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
