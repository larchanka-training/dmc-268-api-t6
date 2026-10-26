"""Every ReviewOutput constraint rejected on the gateway's parse path (docs/PIPELINE_SPEC.md §9).

The answer string goes through ``review/schemas/review-output.schema.json`` and then
``parse_review_output``, as in the gateway. The corpus of ``tests/fixtures/review_output``
is shared with the parity test; no fourth copy of the schema exists. A snapshot whose review
prompt is ``v4`` binds ``review-output.schema.v2.json`` instead (#130 PR E, R9).
"""

from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import pytest

from app.modules.reviews.application.check_runs import RunProviderCheckpoint
from app.modules.reviews.application.llm import RunCallContext
from app.modules.reviews.application.policy_errors import PolicyError, PolicyErrorCode
from app.modules.reviews.application.policy_snapshot import (
    ExecutionProfile,
    PolicySnapshot,
    PromptBindings,
)
from app.modules.reviews.application.prompt_builder import (
    ChangedFile,
    DiffLine,
    PullRequestMeta,
    RepoConventions,
    ReviewContext,
)
from app.modules.reviews.application.review_output import (
    ReviewOutput,
    ReviewOutputV2,
    parse_review_output_v2,
)
from app.modules.reviews.infrastructure.llm.answers import (
    REVIEW_OUTPUT_SCHEMA_PATH,
    InvalidAnswer,
    parse_review_answer,
    provider_schema,
    review_output_schema,
    validate_review_answer,
    validate_review_answer_v2,
)
from app.modules.reviews.infrastructure.llm.ecb_fx import FxQuote, FxQuoteResult
from app.modules.reviews.infrastructure.llm.gateway import LlmGateway, StructuredTask
from app.modules.reviews.infrastructure.llm.memory import InMemoryLlmCallTrace, InMemoryUsageLedger
from app.modules.reviews.infrastructure.llm.models import review_with_gateway
from app.modules.reviews.infrastructure.llm.settings import LlmSettings, ModelPrice, ModelProfile
from app.modules.reviews.infrastructure.llm.transport import (
    ChatMessage,
    ChatRequest,
    ChatResponse,
    ResponseSchema,
)
from tests.llm_fakes import context_checkpoint
from tests.policy_snapshot_fakes import make_policy_document_missing

CORPUS = Path(__file__).parent / "fixtures" / "review_output"
VALID = sorted((CORPUS / "valid").glob("*.json"))
NORMALIZABLE = sorted((CORPUS / "normalizable").glob("*.json"))
# The body prefix <-> rule_name binding is repaired by the post-processor at runtime
# (review/postprocess/lint-filter.md, step 7), so the gateway accepts it (§9 table).
REPAIRED_LATER = {"rule-name-without-prefix.json"}
INVALID = sorted(
    path for path in (CORPUS / "invalid").glob("*.json") if path.name not in REPAIRED_LATER
)
SAMPLE: dict[str, Any] = json.loads((CORPUS / "valid" / "single-line-anchor.json").read_text())
REVIEW_OUTPUT_V2_FILE = (
    Path(__file__).parents[1] / "review" / "schemas" / "review-output.schema.v2.json"
)
EVIDENCE_REFS = [{"block_id": "file-charge", "start_line": 30, "end_line": 34}]
SAMPLE_V2: dict[str, Any] = dict(
    SAMPLE, findings=[dict(SAMPLE["findings"][0], evidence_refs=EVIDENCE_REFS)]
)


@pytest.mark.parametrize("path", VALID, ids=lambda path: path.name)
def test_valid_corpus_is_accepted_as_a_typed_review_output(path: Path) -> None:
    output = parse_review_answer(path.read_text(encoding="utf-8"))

    assert isinstance(output, ReviewOutput)


@pytest.mark.parametrize("path", NORMALIZABLE, ids=lambda path: path.name)
def test_normalizable_corpus_is_corrected_before_typed_parsing(path: Path) -> None:
    raw = path.read_text(encoding="utf-8")
    original = json.loads(raw)

    accepted = validate_review_answer(raw)

    if path.name == "start-line-equals-line.json":
        expected = {
            **original,
            "findings": [{**original["findings"][0], "start_line": None}],
        }
    else:
        assert path.name == "wrong-order.json"
        expected = {**original, "findings": [original["findings"][1], original["findings"][0]]}
    assert accepted == expected
    assert parse_review_answer(raw) == ReviewOutput.model_validate(expected)


@pytest.mark.parametrize("path", INVALID, ids=lambda path: path.name)
def test_every_invalid_corpus_case_is_rejected_with_messages(path: Path) -> None:
    with pytest.raises(InvalidAnswer) as caught:
        validate_review_answer(path.read_text(encoding="utf-8"))

    assert caught.value.errors
    assert all(isinstance(message, str) and message for message in caught.value.errors)


def test_rule_name_prefix_binding_is_left_to_the_post_processor() -> None:
    raw = (CORPUS / "invalid" / "rule-name-without-prefix.json").read_text(encoding="utf-8")

    assert isinstance(parse_review_answer(raw), ReviewOutput)


def _mutated(mutate: Any) -> str:
    value = json.loads(json.dumps(SAMPLE))
    mutate(value)
    return json.dumps(value)


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ('{"findings": [', "not valid JSON"),
        ("", "not valid JSON"),
        ("```json\n" + json.dumps(SAMPLE) + "\n```", "not valid JSON"),
        ("Here is the review:\n" + json.dumps(SAMPLE), "not valid JSON"),
        ("[]", "one JSON object"),
        ('"text"', "one JSON object"),
        (_mutated(lambda v: v.update(verdict="clean")), "'verdict' was unexpected"),
        (_mutated(lambda v: v["findings"][0].update(side="RIGHT")), "'side' was unexpected"),
        (_mutated(lambda v: v["summary"].update(score=3)), "'score' was unexpected"),
        (_mutated(lambda v: v["findings"][0].pop("suggestion")), "'suggestion' is a required"),
        (_mutated(lambda v: v["findings"][0].update(severity="blocker")), "is not one of"),
        (_mutated(lambda v: v["findings"][0].update(category="style")), "is not one of"),
        (_mutated(lambda v: v["summary"].update(effort="huge")), "is not one of"),
        (_mutated(lambda v: v["findings"][0].update(confidence=1.5)), "maximum of 1"),
        (_mutated(lambda v: v["findings"][0].update(line=0)), "minimum of 1"),
        (_mutated(lambda v: v["findings"][0].update(title="x" * 81)), "is too long"),
        (_mutated(lambda v: v["findings"][0].update(body="x" * 1201)), "is too long"),
        (_mutated(lambda v: v["findings"][0].update(path="")), "should be non-empty"),
        (_mutated(lambda v: v["findings"][0].update(title="")), "title: '' should be non-empty"),
        (_mutated(lambda v: v["findings"][0].update(body="")), "body: '' should be non-empty"),
        (_mutated(lambda v: v["summary"].update(problem="")), "problem: '' should be non-empty"),
        (
            _mutated(lambda v: v["summary"].update(done_well="")),
            "done_well: '' should be non-empty",
        ),
        (_mutated(lambda v: v["findings"][0].update(start_line=0)), "less than the minimum of 1"),
        (_mutated(lambda v: v["findings"][0].update(confidence=-0.1)), "minimum of 0"),
        (
            _mutated(lambda v: v["findings"][0].update(start_line=v["findings"][0]["line"] + 1)),
            "start_line must be strictly earlier than line",
        ),
        (
            _mutated(lambda v: v["summary"].update(problem="No terminal punctuation")),
            "exactly one sentence",
        ),
        (_mutated(lambda v: v.update(findings=v["findings"] * 11)), "is too long"),
        (_mutated(lambda v: v["findings"][0].update(line=1.0)), "valid integer"),
        (_mutated(lambda v: v["findings"][0].update(title="Two.\nlines")), "title"),
        (_mutated(lambda v: v["findings"][0].update(start_line=1.0, line=1)), "start_line"),
        (_mutated(lambda v: v["summary"].update(problem="One. Two.")), "exactly one sentence"),
        (_mutated(lambda v: v["summary"].update(done_well="A. B. C.")), "one or two sentences"),
    ],
)
def test_malformed_answers_are_rejected_with_the_validator_message(raw: str, message: str) -> None:
    with pytest.raises(InvalidAnswer) as caught:
        validate_review_answer(raw)

    assert message in str(caught.value)


def test_order_by_severity_then_confidence_is_normalized() -> None:
    first = dict(SAMPLE["findings"][0], severity="low", confidence=0.9)
    second = dict(SAMPLE["findings"][0], severity="critical", confidence=0.9)
    raw = json.dumps(dict(SAMPLE, findings=[first, second]))

    assert validate_review_answer(raw)["findings"] == [second, first]


def test_equal_severity_is_normalized_by_descending_confidence() -> None:
    first = dict(SAMPLE["findings"][0], severity="high", confidence=0.4)
    second = dict(SAMPLE["findings"][0], severity="high", confidence=0.9)

    assert validate_review_answer(json.dumps(dict(SAMPLE, findings=[first, second])))[
        "findings"
    ] == [second, first]


def test_equal_severity_and_confidence_keep_provider_order() -> None:
    first = dict(SAMPLE["findings"][0], path="first.py", severity="high", confidence=0.9)
    second = dict(SAMPLE["findings"][0], path="second.py", severity="low", confidence=0.9)
    third = dict(SAMPLE["findings"][0], path="third.py", severity="high", confidence=0.9)

    accepted = validate_review_answer(json.dumps(dict(SAMPLE, findings=[first, second, third])))

    assert accepted["findings"] == [first, third, second]


def test_semantic_error_uses_the_raw_finding_index_before_sorting() -> None:
    first = dict(SAMPLE["findings"][0], severity="low", title="Invalid title.")
    second = dict(SAMPLE["findings"][0], severity="critical", title="Valid title")

    with pytest.raises(InvalidAnswer) as caught:
        validate_review_answer(json.dumps(dict(SAMPLE, findings=[first, second])))

    assert caught.value.errors == ["findings/0: Value error, title must not end with a period"]


def test_gateway_returns_normalized_output_and_keeps_raw_provider_trace() -> None:
    payload = json.loads((CORPUS / "normalizable" / "wrong-order.json").read_text())
    payload["findings"][1]["start_line"] = 33
    raw_answer = json.dumps(payload)
    raw_response = {"choices": [{"message": {"content": raw_answer}}]}
    expected_raw_response = deepcopy(raw_response)

    class OneAnswerTransport:
        async def complete(
            self, request: ChatRequest, *, checkpoint: RunProviderCheckpoint
        ) -> ChatResponse:
            await checkpoint()
            return ChatResponse(
                raw=raw_response,
                content=raw_answer,
                finish_reason="stop",
                model=request.profile.model,
                prompt_tokens=10,
                completion_tokens=20,
                cached_tokens=0,
                cost=None,
                cost_currency=None,
            )

    settings = LlmSettings.from_env({"LLM_MODEL": "mistral-small-4", "LLM_API_KEYS": "sk-test"})
    trace = InMemoryLlmCallTrace()

    class FixedQuote:
        async def get_quote(self) -> FxQuoteResult:
            return FxQuoteResult(
                FxQuote(
                    Decimal("1.20"),
                    datetime.now(UTC).date(),
                    "EXR.D.USD.EUR.SP00.A",
                    datetime.now(UTC),
                ),
                stale_cache=False,
            )

    gateway = LlmGateway(
        settings,
        OneAnswerTransport(),
        InMemoryUsageLedger(),
        trace,
        checkpoint=context_checkpoint,
        fx_provider=FixedQuote(),
    )
    task = StructuredTask(
        operation="review",
        messages=(ChatMessage("user", "Review this diff"),),
        schema=ResponseSchema("ReviewOutput", provider_schema(review_output_schema())),
        validate=validate_review_answer,
    )
    context = RunCallContext(
        run_id=UUID("00000000-0000-0000-0000-000000000066"),
        workspace_id=UUID("00000000-0000-0000-0000-000000000067"),
        attempt=1,
        engine="fast",
        deadline=datetime.now(UTC) + timedelta(minutes=5),
    )

    result = asyncio.run(gateway.generate(task, context))

    findings = cast(list[dict[str, object]], result.output["findings"])
    assert [finding["path"] for finding in findings] == [
        "app/modules/billing/infrastructure/repository.py",
        "app/modules/billing/application/charge.py",
    ]
    assert findings[0]["start_line"] is None
    # accepted as it came: a lossless deviation costs no repair call
    assert [record.kind.value for _, record in trace.records] == ["primary"]
    assert isinstance(ReviewOutput.model_validate(result.output), ReviewOutput)
    assert raw_response == expected_raw_response
    assert trace.records[0][1].response == expected_raw_response
    assert trace.records[0][1].response["choices"][0]["message"]["content"] == raw_answer


@pytest.mark.parametrize(
    ("problem", "done_well"),
    [("Is this safe?", "It is! Mostly."), ("It breaks!", "Tests pass. Names are clear?")],
)
def test_exclamation_and_question_marks_end_a_sentence(problem: str, done_well: str) -> None:
    summary = dict(SAMPLE["summary"], problem=problem, done_well=done_well)

    validate_review_answer(json.dumps(dict(SAMPLE, summary=summary)))


def test_provider_schema_is_the_schema_file_without_any_annotation() -> None:
    on_disk = json.loads(REVIEW_OUTPUT_SCHEMA_PATH.read_text(encoding="utf-8"))
    sent = provider_schema(review_output_schema())

    assert "$comment" in json.dumps(on_disk["$defs"])
    assert "$comment" not in json.dumps(sent)
    assert "$schema" not in sent and "$id" not in sent
    assert (
        sent["$defs"]["ReviewFinding"]["required"] == on_disk["$defs"]["ReviewFinding"]["required"]
    )
    assert sent["properties"]["findings"]["items"] == {"$ref": "#/$defs/ReviewFinding"}
    assert sent["required"] == ["findings", "summary"]


def test_a_long_echoed_value_keeps_the_message_short_and_its_verdict() -> None:
    value = json.loads(json.dumps(SAMPLE))
    value["findings"][0]["body"] = "x" * 20_000

    with pytest.raises(InvalidAnswer) as caught:
        validate_review_answer(json.dumps(value))

    (message,) = caught.value.errors
    assert len(message) <= 300
    assert message.startswith("findings/0/body: ")
    assert message.endswith("is too long")


def test_a_v2_answer_with_evidence_refs_is_accepted() -> None:
    accepted = validate_review_answer_v2(json.dumps(SAMPLE_V2))

    assert accepted == SAMPLE_V2
    assert accepted["findings"][0]["evidence_refs"] == [  # type: ignore[index]
        {"block_id": "file-charge", "start_line": 30, "end_line": 34}
    ]
    assert isinstance(parse_review_output_v2(accepted), ReviewOutputV2)


def test_a_v2_start_line_equal_to_line_is_normalized_to_null() -> None:
    finding = dict(SAMPLE_V2["findings"][0], line=34, start_line=34)

    accepted = validate_review_answer_v2(json.dumps(dict(SAMPLE_V2, findings=[finding])))

    assert accepted["findings"] == [dict(finding, start_line=None)]


def test_v2_findings_out_of_order_are_sorted() -> None:
    first = dict(SAMPLE_V2["findings"][0], severity="low", confidence=0.9)
    second = dict(SAMPLE_V2["findings"][0], severity="critical", confidence=0.9)

    accepted = validate_review_answer_v2(json.dumps(dict(SAMPLE_V2, findings=[first, second])))

    assert accepted["findings"] == [second, first]


def test_a_v2_semantic_error_uses_the_raw_finding_index() -> None:
    first = dict(SAMPLE_V2["findings"][0], severity="low", line=30, start_line=31)
    second = dict(SAMPLE_V2["findings"][0], severity="critical")

    with pytest.raises(InvalidAnswer) as caught:
        validate_review_answer_v2(json.dumps(dict(SAMPLE_V2, findings=[first, second])))

    assert caught.value.errors == [
        "findings/0: Value error, start_line must be strictly earlier than line"
    ]


def test_a_v2_summary_is_checked_by_the_typed_parse() -> None:
    summary = dict(SAMPLE_V2["summary"], problem="One. Two.")

    with pytest.raises(InvalidAnswer) as caught:
        validate_review_answer_v2(json.dumps(dict(SAMPLE_V2, summary=summary)))

    assert caught.value.errors == [
        "summary: Value error, problem must contain exactly one sentence"
    ]


def test_a_v1_answer_under_v2_is_rejected_naming_evidence_refs() -> None:
    with pytest.raises(InvalidAnswer) as caught:
        validate_review_answer_v2(json.dumps(SAMPLE))

    assert caught.value.errors == ["findings/0: 'evidence_refs' is a required property"]


def test_a_v2_answer_under_v1_is_rejected() -> None:
    # Live pin: the review without a v4 prompt keeps the v1 contract.
    with pytest.raises(InvalidAnswer) as caught:
        validate_review_answer(json.dumps(SAMPLE_V2))

    assert caught.value.errors == [
        "findings/0: Additional properties are not allowed ('evidence_refs' was unexpected)"
    ]


class _RecordingTransport:
    """Answers in order and keeps every request the gateway sent."""

    def __init__(self, *answers: str) -> None:
        self.answers = list(answers)
        self.requests: list[ChatRequest] = []

    async def complete(
        self, request: ChatRequest, *, checkpoint: RunProviderCheckpoint
    ) -> ChatResponse:
        await checkpoint()
        self.requests.append(request)
        if not self.answers:
            raise AssertionError("unexpected provider call")
        content = self.answers.pop(0)
        return ChatResponse(
            raw={"choices": [{"message": {"content": content}}]},
            content=content,
            finish_reason="stop",
            model=request.profile.model,
            prompt_tokens=10,
            completion_tokens=20,
            cached_tokens=0,
            cost=None,
            cost_currency=None,
        )


_REVIEW_PROFILE = ModelProfile(
    provider="router",
    base_url="https://router.test/v1",
    model="review-model",
    context_window=100_000,
    max_output_tokens=1_000,
    price=ModelPrice(Decimal("1"), Decimal("2")),
    api_keys=("sk-test-1",),
)
_RUN_ID = UUID("00000000-0000-0000-0000-000000013901")


def _review_context(review_system_version: str | None) -> ReviewContext:
    snapshot = (
        None
        if review_system_version is None
        else PolicySnapshot(
            schema_version=1,
            snapshot_id=UUID("00000000-0000-0000-0000-000000013902"),
            run_id=_RUN_ID,
            repository_id=UUID("00000000-0000-0000-0000-000000013903"),
            policy_base_sha="sha_base",
            rules=make_policy_document_missing(".review/rules.md"),
            agents=make_policy_document_missing("AGENTS.md"),
            ignore_patterns=(),
            ignore_digest="id_0",
            parser_profile="rules-markdown/v1",
            parser_version="4.0.0",
            matcher_version="review-ignore/v1",
            limits_version="policy-limits/v1",
            builtin_exclusions_version="builtin-exclusions/v1",
            priority_matcher_version="priority-matcher/v1",
            service_defaults_rule_version_id=UUID("00000000-0000-0000-0000-000000013904"),
            defaults_checksum="282d61c2da897152046db742fbd9bfda061ba1c71ce32759d47e042c06b0ff52",
            prompt_bindings=PromptBindings(
                "cs1", "cs2", "cs3", review_system_version=review_system_version
            ),
            execution_profile=ExecutionProfile.fast("m1", "m2"),
            policy_digest="digest_base",
        )
    )
    return ReviewContext(
        system="SYSTEM PROMPT",
        rules=(),
        agents_md=None,
        conventions=RepoConventions((), ()),
        pr_meta=PullRequestMeta(
            "Title", None, "octo", "feature", "main", (), 1, 1, 0, False, False
        ),
        changed_files=(
            ChangedFile(
                "app/service.py",
                "modified",
                (DiffLine(3, "context", "try:"), DiffLine(4, "added", "except Exception: pass")),
            ),
        ),
        omitted_files=(),
        policy_snapshot=snapshot,
    )


def _review(
    transport: _RecordingTransport, review_system_version: str | None
) -> tuple[dict[str, object], list[str]]:
    trace = InMemoryLlmCallTrace()
    gateway = LlmGateway(
        LlmSettings(primary=_REVIEW_PROFILE, fallback=None),
        transport,
        InMemoryUsageLedger(),
        trace,
        checkpoint=context_checkpoint,
    )
    run = RunCallContext(
        run_id=_RUN_ID,
        workspace_id=UUID("00000000-0000-0000-0000-000000013905"),
        attempt=1,
        engine="fast",
        deadline=datetime.now(UTC) + timedelta(minutes=5),
    )
    result = asyncio.run(review_with_gateway(gateway, _review_context(review_system_version), run))
    return result.output, [record.kind.value for _, record in trace.records]


@pytest.mark.parametrize("version", [None, "v1", "v2", "v3"], ids=str)
def test_the_review_sends_the_v1_schema_up_to_review_prompt_v3(version: str | None) -> None:
    transport = _RecordingTransport(json.dumps(SAMPLE))

    output, kinds = _review(transport, version)

    (request,) = transport.requests
    assert request.response_schema is not None
    assert request.response_schema.name == "ReviewOutput"
    assert request.response_schema.schema == provider_schema(review_output_schema())
    assert output == SAMPLE
    assert kinds == ["primary"]


def test_review_prompt_v4_sends_the_v2_schema_and_accepts_a_v2_answer() -> None:
    on_disk = json.loads(REVIEW_OUTPUT_V2_FILE.read_text(encoding="utf-8"))
    # The v2 file carries annotations only at the top level, so this is the whole strip.
    assert "$comment" not in json.dumps(on_disk)
    transport = _RecordingTransport(json.dumps(SAMPLE_V2))

    output, kinds = _review(transport, "v4")

    (request,) = transport.requests
    assert request.response_schema is not None
    assert request.response_schema.name == "ReviewOutputV2"
    assert request.response_schema.schema == {
        key: value for key, value in on_disk.items() if key not in {"$schema", "$id"}
    }
    assert output == SAMPLE_V2
    assert kinds == ["primary"]


def test_review_prompt_v4_repairs_a_v1_answer() -> None:
    transport = _RecordingTransport(json.dumps(SAMPLE), json.dumps(SAMPLE_V2))

    output, kinds = _review(transport, "v4")

    assert kinds == ["primary", "repair"]
    assert output == SAMPLE_V2


def test_review_prompt_v3_repairs_a_v2_answer() -> None:
    transport = _RecordingTransport(json.dumps(SAMPLE_V2), json.dumps(SAMPLE))

    output, kinds = _review(transport, "v3")

    assert kinds == ["primary", "repair"]
    assert output == SAMPLE


def test_an_unknown_review_prompt_version_fails_before_any_call() -> None:
    transport = _RecordingTransport(json.dumps(SAMPLE))

    with pytest.raises(PolicyError) as caught:
        _review(transport, "v9")

    assert caught.value.code == PolicyErrorCode.SNAPSHOT_INTEGRITY_ERROR
    assert "'v9'" in str(caught.value)
    assert transport.requests == []
