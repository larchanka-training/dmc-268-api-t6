"""The gateway review adapter fits, guards and manifests a snapshot-path review once (#130 F1-5).

R10: ``GatewayReviewModel`` is the only fit point of a review. On the snapshot path it fits to
``send_budget_for(profile, review route window)`` with the snapshot's counter, runs the pre-send
and provenance guards before any provider call, and answers with ``ReviewReply``: the accepted
answer, the context it was sent with and the manifest of the accepted call (base, repair or
fallback, T-a). ``ExecuteReviewRun`` attributes against that reply; a model that answers with a
plain mapping (the fakes) still works. The six fit and guard cases that used to sit in
``tests/test_execute_review.py`` prove the same behaviours here, at the new fit point.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest

from app.modules.reviews.application import execute_review as execute_review_module
from app.modules.reviews.application.check_runs import RunProviderCheckpoint
from app.modules.reviews.application.context_provenance import SourceRole
from app.modules.reviews.application.conventions import CachedConventions, GeneratedConventions
from app.modules.reviews.application.execute_review import (
    ExecuteReviewRun,
    ReviewPromptInput,
    ReviewReply,
)
from app.modules.reviews.application.llm import LlmCallFailed, LlmErrorCode, RunCallContext
from app.modules.reviews.application.policy_errors import PolicyError, PolicyErrorCode
from app.modules.reviews.application.policy_snapshot import (
    POLICY_SNAPSHOT_SCHEMA_VERSION,
    PolicySnapshot,
)
from app.modules.reviews.application.prompt_budget import send_budget_for
from app.modules.reviews.application.prompt_builder import (
    ChangedFile,
    DiffLine,
    PromptBuilder,
    PullRequestMeta,
    RepoConventions,
    ReviewContext,
    file_block_id,
)
from app.modules.reviews.application.reflection_pass import ReflectionPass
from app.modules.reviews.application.review_context import (
    ContextBlock,
    ContextLevel,
    ReviewContextPayload,
)
from app.modules.reviews.application.run_failures import RunFailure
from app.modules.reviews.infrastructure.llm import models as models_module
from app.modules.reviews.infrastructure.llm.gateway import LlmGateway
from app.modules.reviews.infrastructure.llm.memory import InMemoryLlmCallTrace, InMemoryUsageLedger
from app.modules.reviews.infrastructure.llm.models import GatewayReviewModel
from app.modules.reviews.infrastructure.llm.settings import LlmSettings, ModelPrice, ModelProfile
from app.modules.reviews.infrastructure.llm.transport import ChatRequest, ChatResponse
from tests.llm_fakes import context_checkpoint
from tests.policy_snapshot_fakes import (
    make_complete_execution_profile,
    make_complete_prompt_bindings,
    make_policy_document_missing,
    make_policy_document_present,
)
from tests.test_review_context_blocks import HEAD as PINNED_HEAD
from tests.test_review_context_blocks import (
    L2_WINDOW,
    L3_FULL,
    L4_SIGNATURE,
    _block,
    _bounded_l4_id,
    _payload,
)

RUN_ID = UUID("00000000-0000-0000-0000-000000000500")
MAIN = "app/main.py"
SUMMARY = {"problem": "One issue was found.", "done_well": "The change is small.", "effort": "none"}
EMPTY_ANSWER = {"findings": [], "summary": SUMMARY}
NO_ISSUES = "No verified issues were found in the evaluated changes."


def _profile(model: str = "review-model", *, context_window: int = 100_000) -> ModelProfile:
    # The fast engine caps the window at 60 000: max_prompt_tokens = 60 000 - 8 000 = 52 000.
    return ModelProfile(
        provider="router",
        base_url="https://router.test/v1",
        model=model,
        context_window=context_window,
        max_output_tokens=8_000,
        price=ModelPrice(Decimal("0.000001"), Decimal("0.000001")),
        api_keys=("sk-test-1",),
    )


class Transport:
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


def _gateway(
    transport: Transport,
    *,
    primary: ModelProfile | None = None,
    fallback: ModelProfile | None = None,
) -> LlmGateway:
    return LlmGateway(
        LlmSettings(primary=primary or _profile(), fallback=fallback),
        transport,
        InMemoryUsageLedger(),
        InMemoryLlmCallTrace(),
        checkpoint=context_checkpoint,
    )


RUN = RunCallContext(
    run_id=RUN_ID,
    workspace_id=UUID("00000000-0000-0000-0000-000000000501"),
    attempt=1,
    engine="fast",
    deadline=datetime.now(UTC) + timedelta(hours=1),
)


def _snapshot(*, max_input_tokens: int = 60_000, max_policy_tokens: int = 12_000) -> PolicySnapshot:
    """A ready snapshot bound to review prompt v4: the review answers in ReviewOutput v2."""
    profile = replace(
        make_complete_execution_profile("fast", review_model="model-a", verifier_model="model-b"),
        max_input_tokens=max_input_tokens,
        max_policy_tokens=max_policy_tokens,
    )
    return PolicySnapshot(
        schema_version=POLICY_SNAPSHOT_SCHEMA_VERSION,
        snapshot_id=UUID("00000000-0000-0000-0000-000000000503"),
        run_id=RUN_ID,
        repository_id=UUID("00000000-0000-0000-0000-000000000504"),
        policy_base_sha="b" * 40,
        rules=make_policy_document_present(
            path=".review/rules.md", text="# Rules\nAlways sanitize input.\n", blob_oid="r" * 40
        ),
        agents=make_policy_document_missing("AGENTS.md"),
        ignore_patterns=(),
        ignore_digest="0" * 64,
        parser_profile="rules-markdown/v1",
        parser_version="4.0.0",
        matcher_version="review-ignore/v1",
        limits_version="limits/v1",
        builtin_exclusions_version="builtin-exclusions/v1",
        priority_matcher_version="priority/v1",
        service_defaults_rule_version_id=UUID("00000000-0000-0000-0000-000000000505"),
        defaults_checksum="default-backend.v1",
        prompt_bindings=replace(make_complete_prompt_bindings(), review_system_version="v4"),
        execution_profile=profile,
        policy_digest="d" * 64,
    )


def _meta(head_sha: str | None = "head_sha_123") -> PullRequestMeta:
    return PullRequestMeta("PR", None, "u", "b", "m", (), 1, 1, 0, False, False, head_sha=head_sha)


def _context(
    files: tuple[ChangedFile, ...],
    snapshot: PolicySnapshot | None,
    *,
    system: str = "Review carefully.",
) -> ReviewContext:
    return ReviewContext(
        system=system,
        rules=(),
        agents_md=None,
        conventions=RepoConventions((), ()),
        pr_meta=_meta(),
        changed_files=files,
        omitted_files=(),
        policy_snapshot=snapshot,
    )


def _one_line(path: str = MAIN) -> tuple[ChangedFile, ...]:
    return (ChangedFile(path, "modified", (DiffLine(1, "added", "x = 1"),)),)


def _finding(line: int, *, title: str = "An issue") -> dict[str, Any]:
    return {
        "path": MAIN,
        "line": line,
        "start_line": None,
        "severity": "critical",
        "category": "security",
        "title": title,
        "body": "Issue on the line.",
        "suggestion": None,
        "confidence": 0.95,
        "rule_name": None,
        "evidence_refs": [{"block_id": file_block_id(MAIN), "start_line": line, "end_line": line}],
    }


def _draft(gateway: LlmGateway, context: ReviewContext) -> object:
    return asyncio.run(GatewayReviewModel(gateway, RUN, _NoMeta()).draft_review(context=context))


class _NoMeta:
    async def get_pull_request_meta(self, run_id: UUID) -> PullRequestMeta | None:
        return None


class _Pipeline:
    """Processor, prompt source and publisher around a real or fake review model."""

    def __init__(self, files: tuple[ChangedFile, ...], snapshot: PolicySnapshot | None) -> None:
        self.files = files
        self.snapshot = snapshot
        self.published: list[Any] = []

    async def prepare(self, run_id: UUID) -> GeneratedConventions:
        cached = CachedConventions(UUID(int=1), None, RUN_ID, (), (), {})
        return GeneratedConventions(cached, None, True)

    async def get_review_prompt_input(
        self, run_id: UUID, saved: GeneratedConventions
    ) -> ReviewPromptInput:
        return ReviewPromptInput(
            system="Review carefully.",
            rules=(),
            agents_md=None,
            conventions=RepoConventions(("Use services.",), ("Test paths.",)),
            changed_files=self.files,
            omitted_files=(),
            policy_snapshot=self.snapshot,
        )

    async def get_pull_request_meta(self, run_id: UUID) -> PullRequestMeta:
        return _meta()

    async def execute(self, run_id: UUID, raw_output: object) -> bool:
        self.published.append(raw_output)
        return True


class _Verifier:
    """Supports every candidate it is shown, citing ``line`` of the file, and records ids."""

    def __init__(self, line: int = 1) -> None:
        self.line = line
        self.candidates: list[str] = []

    async def verify(self, *, system_prompt: str, user_prompt: str, **_: object) -> dict[str, Any]:
        ids = re.findall(r'candidate_id="([^"]+)"', user_prompt)
        self.candidates.extend(ids)
        return {
            "decisions": [
                {
                    "candidate_id": candidate,
                    "status": "supported",
                    "reason": "OK",
                    "evidence_refs": [
                        {
                            "block_id": file_block_id(MAIN),
                            "start_line": self.line,
                            "end_line": self.line,
                        }
                    ],
                    "requirement_refs": [],
                }
                for candidate in ids
            ]
        }


class _GatewayModel:
    """``ReviewModel`` over the real adapter; the meta comes from the pipeline."""

    def __init__(self, gateway: LlmGateway, pipeline: _Pipeline) -> None:
        self.review = GatewayReviewModel(gateway, RUN, pipeline)
        self.get_pull_request_meta = pipeline.get_pull_request_meta

    async def draft_review(self, *, context: ReviewContext) -> object:
        return await self.review.draft_review(context=context)


def _execute(pipeline: _Pipeline, model: object, *, line: int = 1) -> tuple[bool, _Verifier]:
    verifier = _Verifier(line)
    run = ExecuteReviewRun(
        pipeline,
        pipeline,
        model,  # type: ignore[arg-type]
        pipeline,
        reflection_pass=ReflectionPass(verifier),
    )
    return asyncio.run(run.execute(RUN_ID)), verifier


# The six fit and guard cases, moved from tests/test_execute_review.py to the new fit point.


def test_policy_over_its_budget_fails_closed_before_any_provider_call() -> None:
    transport = Transport(json.dumps(EMPTY_ANSWER))

    with pytest.raises(PolicyError) as raised:
        _draft(_gateway(transport), _context(_one_line(), _snapshot(max_policy_tokens=1)))

    assert raised.value.code == PolicyErrorCode.PROMPT_BUDGET_EXCEEDED
    assert raised.value.limit == 1
    assert raised.value.actual is not None and raised.value.actual > 1
    assert "допустимо 1" in str(raised.value)
    assert transport.requests == []


def test_prompt_that_cannot_fit_fails_with_context_overflow_before_any_call() -> None:
    # send budget = 10 000 - 8 000 = 2 000 tokens; the system prompt alone is ~2 100.
    transport = Transport(json.dumps(EMPTY_ANSWER))
    context = _context(
        _one_line(), _snapshot(max_input_tokens=10_000), system="Review carefully. " * 350
    )

    with pytest.raises(RunFailure) as raised:
        _draft(_gateway(transport), context)

    assert raised.value.error_code == "llm_context_overflow"
    assert not isinstance(raised.value, PolicyError)
    assert transport.requests == []


def test_a_2500_line_diff_is_fitted_first_and_never_overflows_prematurely() -> None:
    # #219 part 2: the guard runs after the fit, so a large diff is cut, not refused.
    line = "x = 100 + 200 # line calculation " + "y" * 55
    files = (
        ChangedFile(
            "app/large.py", "added", tuple(DiffLine(i, "added", line) for i in range(1, 2501))
        ),
    )
    pipeline = _Pipeline(files, _snapshot())
    transport = Transport(json.dumps(EMPTY_ANSWER))

    success, _ = _execute(pipeline, _GatewayModel(_gateway(transport), pipeline))

    assert success is True
    [request] = transport.requests
    shown = request.messages[1].content.count('type="added"')
    assert 0 < shown < 2500
    [published] = pipeline.published
    assert published["findings"] == []
    assert published["summary"]["problem"] == NO_ISSUES


def test_the_provenance_guard_blocks_a_disallowed_source_before_any_call() -> None:
    pipeline = _Pipeline(_one_line("../secret.py"), _snapshot())
    transport = Transport(json.dumps(EMPTY_ANSWER))

    with pytest.raises(RunFailure) as raised:
        _execute(pipeline, _GatewayModel(_gateway(transport), pipeline))

    assert raised.value.error_code == "policy_context_violation"
    assert transport.requests == []
    assert pipeline.published == []


def test_the_send_budget_is_capped_by_the_review_route_window() -> None:
    # The snapshot allows 60 000 - 8 000 = 52 000 tokens; the route window allows
    # min(24 000, 60 000) - 8 000 = 16 000. ~16 667 tokens of system prompt overflow the
    # route window only: the adapter refuses before the gateway's own window check.
    gateway = _gateway(Transport(), primary=_profile(context_window=24_000))
    context = _context(_one_line(), _snapshot(max_input_tokens=60_000), system="S" * 50_000)

    with pytest.raises(RunFailure) as raised:
        _draft(gateway, context)

    assert raised.value.error_code == "llm_context_overflow"
    assert "limit is 16000" in str(raised.value)


def test_coordinates_are_checked_against_the_context_the_adapter_fitted() -> None:
    # send budget = 12 000 - 8 000 = 4 000 tokens: the 400-line file is cut by the adapter.
    files = (
        ChangedFile(
            MAIN,
            "modified",
            tuple(DiffLine(5000 + i, "added", f"line_{i} = {i}") for i in range(400)),
        ),
    )
    pipeline = _Pipeline(files, _snapshot(max_input_tokens=12_000))
    answer = {
        "findings": [
            _finding(5005, title="Visible issue"),
            _finding(5300, title="Truncated issue"),
            _finding(3, title="Absent issue"),
        ],
        "summary": SUMMARY,
    }
    transport = Transport(json.dumps(answer))

    success, verifier = _execute(pipeline, _GatewayModel(_gateway(transport), pipeline), line=5005)

    assert success is True
    assert '<line n="5300"' not in transport.requests[0].messages[1].content
    assert len(verifier.candidates) == 1
    [published] = pipeline.published
    assert [finding["line"] for finding in published["findings"]] == [5005]
    dropped = {item.finding.line: item.drop_reason for item in published.dropped_findings}
    assert dropped == {5300: "anchor_not_visible", 3: "line_absent"}


# R10 and T-a: one fit, the reply's context and the accepted call's manifest.


def test_a_snapshot_review_is_fitted_and_manifested_once(monkeypatch: pytest.MonkeyPatch) -> None:
    fits: list[int] = []
    manifests: list[str] = []
    build_manifest = PromptBuilder.build_manifest

    def counting(fit: Callable[..., ReviewContext]) -> Callable[..., ReviewContext]:
        def wrapper(context: ReviewContext, **kwargs: Any) -> ReviewContext:
            fits.append(len(context.changed_files))
            return fit(context, **kwargs)

        return wrapper

    for module in (execute_review_module, models_module):
        if hasattr(module, "fit_review_context"):
            monkeypatch.setattr(module, "fit_review_context", counting(module.fit_review_context))

    def recording(self: PromptBuilder, context: ReviewContext, **kwargs: Any) -> Any:
        manifests.append(str(kwargs.get("stage", "review")))
        return build_manifest(self, context, **kwargs)

    monkeypatch.setattr(PromptBuilder, "build_manifest", recording)
    pipeline = _Pipeline(
        (ChangedFile(MAIN, "modified", (DiffLine(5, "added", "x = 1"),)),), _snapshot()
    )
    transport = Transport(json.dumps({"findings": [_finding(5)], "summary": SUMMARY}))

    success, verifier = _execute(pipeline, _GatewayModel(_gateway(transport), pipeline), line=5)

    assert success is True
    assert fits == [1]
    # One review manifest; the verifier builds its own for its own call.
    assert manifests.count("review") == 1
    assert len(verifier.candidates) == 1


def test_a_repair_answer_comes_with_the_repair_manifest() -> None:
    transport = Transport("{not json", json.dumps({"findings": [_finding(1)], "summary": SUMMARY}))
    model = GatewayReviewModel(_gateway(transport), RUN, _NoMeta())
    context = _context(_one_line(), _snapshot())

    reply = asyncio.run(model.draft_review(context=context))

    assert isinstance(reply, ReviewReply)
    assert model.last_result is not None
    assert reply.manifest.recovery_kind == "repair"
    assert reply.manifest.call_id == model.last_result.accepted_call_id
    assert [block.block_id for block in reply.manifest.blocks] == [
        "policy-rules",
        file_block_id(MAIN),
        "previous-output",
    ]
    assert reply.context.changed_files == context.changed_files
    assert reply.output["findings"] == [_finding(1)]
    assert len(transport.requests) == 2


def test_a_fallback_answer_comes_with_the_fallback_manifest() -> None:
    transport = Transport(
        "{not json", "{still not json", json.dumps({"findings": [], "summary": SUMMARY})
    )
    gateway = _gateway(transport, fallback=_profile("fallback-model"))
    model = GatewayReviewModel(gateway, RUN, _NoMeta())

    reply = asyncio.run(model.draft_review(context=_context(_one_line(), _snapshot())))

    assert isinstance(reply, ReviewReply)
    assert model.last_result is not None and model.last_result.model == "fallback-model"
    assert reply.manifest.recovery_kind == "fallback"
    assert reply.manifest.call_id == model.last_result.accepted_call_id
    assert [block.block_id for block in reply.manifest.blocks] == [
        "policy-rules",
        file_block_id(MAIN),
    ]
    assert [request.profile.model for request in transport.requests] == [
        "review-model",
        "review-model",
        "fallback-model",
    ]


def test_a_legacy_prompt_that_cannot_fit_still_fails_at_the_gateway_window() -> None:
    # Legacy keeps its behaviour: no adapter guard, the gateway's own check refuses the call.
    transport = Transport(json.dumps(EMPTY_ANSWER))

    with pytest.raises(LlmCallFailed) as raised:
        _draft(_gateway(transport), _context(_one_line(), None, system="S" * 200_000))

    assert raised.value.error_code is LlmErrorCode.CONTEXT_OVERFLOW
    assert transport.requests == []


def test_a_legacy_review_answers_with_the_accepted_mapping_itself() -> None:
    answer = {
        "findings": [],
        "summary": {"problem": "Nothing found.", "done_well": "Small.", "effort": "none"},
    }
    transport = Transport(json.dumps(answer))

    reply = _draft(_gateway(transport), _context(_one_line(), None))

    assert reply == answer


class _ReplyModel:
    """A model that answers with a prepared ``ReviewReply``."""

    def __init__(self, reply: Callable[[ReviewContext], ReviewReply]) -> None:
        self._reply = reply
        self.seen: list[ReviewContext] = []

    async def get_pull_request_meta(self, run_id: UUID) -> PullRequestMeta:
        return _meta()

    async def draft_review(self, *, context: ReviewContext) -> ReviewReply:
        self.seen.append(context)
        return self._reply(context)


TEN_LINES = (
    ChangedFile(MAIN, "modified", tuple(DiffLine(n, "added", f"v{n} = {n}") for n in range(1, 11))),
)


def _cut(context: ReviewContext) -> ReviewContext:
    # The adapter's fit: the first five lines of ten.
    [file] = context.changed_files
    return replace(context, changed_files=(replace(file, lines=file.lines[:5], total_lines=10),))


def test_execute_review_checks_coordinates_against_the_reply_context() -> None:
    def reply(context: ReviewContext) -> ReviewReply:
        fitted = _cut(context)
        manifest = PromptBuilder().build_manifest(fitted, call_id=UUID(int=7))
        output = {"findings": [_finding(2), _finding(8)], "summary": SUMMARY}
        return ReviewReply(output, fitted, manifest)

    pipeline = _Pipeline(TEN_LINES, _snapshot())

    success, verifier = _execute(pipeline, _ReplyModel(reply), line=2)

    assert success is True
    [published] = pipeline.published
    assert [finding["line"] for finding in published["findings"]] == [2]
    # Line 8 is in the stored diff but not in what the model was sent.
    assert {item.finding.line: item.drop_reason for item in published.dropped_findings} == {
        8: "anchor_not_visible"
    }
    assert len(verifier.candidates) == 1


def test_execute_review_attributes_against_the_manifest_of_the_accepted_call() -> None:
    def reply(context: ReviewContext) -> ReviewReply:
        # An accepted call whose manifest did not show the file block: its evidence is not
        # attributable, even though a manifest rebuilt from the context would show it.
        manifest = replace(
            PromptBuilder().build_manifest(context, call_id=UUID(int=8)),
            recovery_kind="fallback",
            blocks=(),
        )
        return ReviewReply({"findings": [_finding(2)], "summary": SUMMARY}, context, manifest)

    pipeline = _Pipeline(TEN_LINES, _snapshot())

    success, verifier = _execute(pipeline, _ReplyModel(reply), line=2)

    assert success is True
    [published] = pipeline.published
    assert published["findings"] == []
    assert [item.drop_reason for item in published.dropped_findings] == ["evidence_not_shown"]
    assert verifier.candidates == []


class _MappingModel:
    """A test fake that answers with the mapping itself, as no production adapter does."""

    async def get_pull_request_meta(self, run_id: UUID) -> PullRequestMeta:
        return _meta()

    async def draft_review(self, *, context: ReviewContext) -> Mapping[str, object]:
        return {"findings": [_finding(2)], "summary": SUMMARY}


def test_execute_review_still_takes_a_plain_mapping_on_the_snapshot_path(
    caplog: pytest.LogCaptureFixture,
) -> None:
    pipeline = _Pipeline(TEN_LINES, _snapshot())

    with caplog.at_level(logging.WARNING, logger=execute_review_module.__name__):
        success, verifier = _execute(pipeline, _MappingModel(), line=2)

    assert success is True
    [published] = pipeline.published
    assert [finding["line"] for finding in published["findings"]] == [2]
    assert len(verifier.candidates) == 1
    # It skipped the adapter's fit and guards: said, never silent.
    [warning] = [r for r in caplog.records if r.name == execute_review_module.__name__]
    assert warning.levelno == logging.WARNING
    assert warning.getMessage() == (
        "Snapshot-path review answered without a ReviewReply: the fit and the pre-send "
        f"guards of the review adapter did not run run_id={RUN_ID}"
    )


def test_a_plain_mapping_on_the_legacy_path_logs_nothing(caplog: pytest.LogCaptureFixture) -> None:
    pipeline = _Pipeline(TEN_LINES, None)

    with caplog.at_level(logging.WARNING, logger=execute_review_module.__name__):
        success, _ = _execute(pipeline, _MappingModel(), line=2)

    assert success is True
    assert [r for r in caplog.records if r.name == execute_review_module.__name__] == []


# The L2-L4 excerpts of the payload through the real adapter (#130 F1-4 with F1-5).


def _with_excerpts(*blocks: ContextBlock, head_sha: str = PINNED_HEAD) -> ReviewContext:
    return replace(
        _context(_one_line(), _snapshot()), pr_meta=_meta(head_sha), context_blocks=blocks
    )


def test_the_adapter_fits_guards_manifests_and_sends_the_l2_l4_excerpts() -> None:
    transport = Transport(json.dumps(EMPTY_ANSWER))
    context = _with_excerpts(L4_SIGNATURE, L2_WINDOW, L3_FULL)

    reply = _draft(_gateway(transport), context)

    assert isinstance(reply, ReviewReply)
    assert reply.context.context_blocks == (L4_SIGNATURE, L2_WINDOW, L3_FULL)
    assert [(block.block_id, block.source_role) for block in reply.manifest.blocks] == [
        ("policy-rules", SourceRole.POLICY_DOCUMENT),
        (file_block_id(MAIN), SourceRole.DIFF),
        ("L4:sym-1", SourceRole.AST_EXCERPT),
        ("L2:f1:3-4", SourceRole.SOURCE_EXCERPT),
        ("L3:f2:1-3", SourceRole.SOURCE_EXCERPT),
    ]
    assert reply.manifest.is_range_shown("L4:sym-1", 5, 6)
    [request] = transport.requests
    user = request.messages[1].content
    for block_id in ("L4:sym-1", "L2:f1:3-4", "L3:f2:1-3"):
        assert f'<excerpt block_id="{block_id}"' in user
    assert '<line n="6">        ...</line>' in user


@pytest.mark.parametrize(
    "text", ["def f():", "def f():\n    return 1\n    # one more"], ids=["shorter", "longer"]
)
def test_an_excerpt_whose_text_does_not_match_its_ranges_fails_before_any_call(text: str) -> None:
    # Two source lines (3-4); the stage always cuts one text line per source line.
    broken = _block("L2:f1:3-4", ContextLevel.L2, "app/util.py", ((3, 4),), text)
    transport = Transport(json.dumps(EMPTY_ANSWER))

    with pytest.raises(PolicyError) as raised:
        _draft(_gateway(transport), _with_excerpts(broken))

    assert raised.value.code is PolicyErrorCode.CONTEXT_VIOLATION
    assert "Excerpt 'L2:f1:3-4' has" in str(raised.value)
    assert transport.requests == []


# A push after the stage built the diff map: the snapshot path keeps the attempt's head.


class _PushedPipeline(_Pipeline):
    """The live PR meta names a head pushed after the stage pinned the attempt's diff map."""

    async def get_pull_request_meta(self, run_id: UUID) -> PullRequestMeta:
        return _meta("f" * 40)


class _ExcerptStage:
    """The context stage as execute_review sees it, with the attempt's diff map head."""

    diff_old_sha: str | None = None
    head_sha: str | None = PINNED_HEAD

    def __init__(self, snapshot: PolicySnapshot | None, payload: ReviewContextPayload) -> None:
        self.snapshot_for_llm = snapshot
        self.payload = payload

    def excluded(self, path: str) -> bool:
        return False


def _pushed(snapshot: PolicySnapshot | None) -> tuple[GatewayReviewModel, Transport]:
    pipeline = _PushedPipeline(_one_line(), None)
    transport = Transport(json.dumps(EMPTY_ANSWER))
    model = _GatewayModel(_gateway(transport), pipeline)
    run = ExecuteReviewRun(
        pipeline,
        pipeline,
        model,  # type: ignore[arg-type]
        pipeline,
        context_filter=_ExcerptStage(snapshot, _payload()),
        reflection_pass=ReflectionPass(_Verifier()),
    )
    assert asyncio.run(run.execute(RUN_ID)) is True
    return model.review, transport


def test_a_push_after_the_diff_map_keeps_the_pinned_head_on_the_snapshot_path() -> None:
    review, transport = _pushed(_snapshot())

    assert review.last_result is not None and review.last_result.accepted_manifest is not None
    revisions = {
        block.block_id: block.revision for block in review.last_result.accepted_manifest.blocks
    }
    # The diff block and the excerpts name the head the stored diff and the excerpts were
    # read at, not the pushed one.
    assert revisions == {
        "policy-rules": "b" * 40,
        file_block_id(MAIN): PINNED_HEAD,
        _bounded_l4_id(L4_SIGNATURE): PINNED_HEAD,
        "L2:f1:3-4": PINNED_HEAD,
    }
    [request] = transport.requests
    assert f"<head_sha>{PINNED_HEAD}</head_sha>" in request.messages[1].content


def test_a_legacy_review_keeps_the_live_head_of_the_meta() -> None:
    _, transport = _pushed(None)

    [request] = transport.requests
    assert f"<head_sha>{'f' * 40}</head_sha>" in request.messages[1].content
    assert "<context_excerpts>" not in request.messages[1].content


# E-9: the send budget never reads the environment; the caller passes the route window.


def test_send_budget_ignores_the_window_variables_of_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = _snapshot(max_input_tokens=60_000).execution_profile
    monkeypatch.setenv("LLM_CONTEXT_WINDOW", "20000")
    monkeypatch.setenv("LLM_MAX_OUTPUT_TOKENS", "1000")

    assert send_budget_for(profile, stage="review") == 52_000
    assert send_budget_for(profile, stage="review", model_max_prompt_tokens=16_000) == 16_000
    assert send_budget_for(profile, stage="verification") == 52_000
