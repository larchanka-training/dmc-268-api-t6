"""The ``llm.verify`` trace of the Reflection Pass (#130 PR E, R13 narrowed).

``ExecuteReviewRun`` hands the verifier's candidate ids, the accepted call id and the
decisions without ``reason`` to ``StoreReviewOutput`` on ``VerifiedReviewOutput``; the store
passes them to ``enter_publishing``, which writes the ``llm.verify`` run action
(``docs/PIPELINE_SPEC.md:177``; ``manifest_id`` stays null until manifests are persisted).
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import re
from dataclasses import replace
from types import SimpleNamespace, TracebackType
from typing import Any, cast
from uuid import UUID

import pytest

import app.modules.reviews.application.execute_review as execute_review_module
from app.modules.reviews.application.conventions import CachedConventions, GeneratedConventions
from app.modules.reviews.application.execute_review import ExecuteReviewRun, ReviewPromptInput
from app.modules.reviews.application.findings_post_processor import ProcessedReviewOutput
from app.modules.reviews.application.policy_snapshot import (
    POLICY_SNAPSHOT_SCHEMA_VERSION,
    PolicySnapshot,
    make_complete_execution_profile,
    make_complete_prompt_bindings,
    make_policy_document_missing,
    make_policy_document_present,
)
from app.modules.reviews.application.prompt_builder import (
    ChangedFile,
    DiffLine,
    PullRequestMeta,
    RepoConventions,
    ReviewContext,
    file_block_id,
)
from app.modules.reviews.application.reflection_pass import ReflectionPass, VerifierReply
from app.modules.reviews.application.review_output import (
    FindingPostProcessingInput,
    ReviewOutput,
    ReviewPublication,
    VerifiedReviewOutput,
)
from app.modules.reviews.application.store_review_output import (
    PublishingGuard,
    StoreReviewOutput,
)
from tests.test_diff_anchor import _diff_map

RUN_ID = UUID("00000000-0000-0000-0000-000000013901")
ACCEPTED_CALL_ID = UUID("00000000-0000-0000-0000-000000013902")
MAIN_BLOCK_ID = file_block_id("app/main.py")
SUMMARY = {"problem": "A bug.", "done_well": "Small change.", "effort": "small"}


def _snapshot() -> PolicySnapshot:
    profile = make_complete_execution_profile(
        "fast", review_model="model-a", verifier_model="model-b"
    )
    return PolicySnapshot(
        schema_version=POLICY_SNAPSHOT_SCHEMA_VERSION,
        snapshot_id=UUID("00000000-0000-0000-0000-000000013903"),
        run_id=RUN_ID,
        repository_id=UUID("00000000-0000-0000-0000-000000013904"),
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
        limits_version="policy-limits/v1",
        builtin_exclusions_version="builtin-exclusions/v1",
        priority_matcher_version="priority-matcher/v1",
        service_defaults_rule_version_id=UUID("00000000-0000-0000-0000-000000013905"),
        defaults_checksum="282d61c2da897152046db742fbd9bfda061ba1c71ce32759d47e042c06b0ff52",
        prompt_bindings=make_complete_prompt_bindings(),
        execution_profile=replace(profile, max_input_tokens=60_000, max_policy_tokens=12_000),
        policy_digest="3" * 64,
    )


def _finding(line: int, title: str) -> dict[str, Any]:
    return {
        "path": "app/main.py",
        "line": line,
        "start_line": None,
        "severity": "critical",
        "category": "security",
        "title": title,
        "body": "Fix it.",
        "suggestion": None,
        "confidence": 0.9,
        "rule_name": None,
        "evidence_refs": [{"block_id": MAIN_BLOCK_ID, "start_line": line, "end_line": line}],
    }


class _Processor:
    async def prepare(self, run_id: UUID) -> GeneratedConventions:
        return GeneratedConventions(
            CachedConventions(
                repository_id=RUN_ID,
                agents_md_sha=None,
                prompt_version_id=RUN_ID,
                key_patterns=("Use services.",),
                recommendations=("Test paths.",),
                languages={},
            ),
            None,
            True,
        )


class _Repository:
    async def get_review_prompt_input(
        self, run_id: UUID, saved: GeneratedConventions
    ) -> ReviewPromptInput:
        return ReviewPromptInput(
            system="Review carefully.",
            rules=(),
            agents_md=None,
            conventions=RepoConventions((), ()),
            changed_files=(
                ChangedFile(
                    "app/main.py",
                    "modified",
                    (DiffLine(10, "added", "x = 1"), DiffLine(11, "added", "y = 2")),
                ),
            ),
            omitted_files=(),
            policy_snapshot=_snapshot(),
        )


class _Model:
    def __init__(self, findings: list[dict[str, Any]]) -> None:
        self._findings = findings

    async def get_pull_request_meta(self, run_id: UUID) -> PullRequestMeta:
        return PullRequestMeta("Title", None, "u", "b", "m", (), 1, 1, 0, False, False)

    async def draft_review(self, *, context: ReviewContext) -> dict[str, Any]:
        return {"findings": self._findings, "summary": SUMMARY}


class _Publisher:
    def __init__(self) -> None:
        self.outputs: list[Any] = []

    async def execute(self, run_id: UUID, raw_output: Any) -> bool:
        self.outputs.append(raw_output)
        return True


class _ReplyVerifier:
    """Supports the first candidate, contradicts the rest, names an accepted call."""

    def __init__(self) -> None:
        self.candidate_ids: list[str] = []

    async def verify(self, *, system_prompt: str, user_prompt: str, **_: object) -> VerifierReply:
        self.candidate_ids = re.findall(r'candidate candidate_id="([^"]+)"', user_prompt)
        decisions = [
            {
                "candidate_id": candidate_id,
                "status": "supported" if index == 0 else "contradicted",
                "reason": "Confirmed in the diff.",
                "evidence_refs": [{"block_id": MAIN_BLOCK_ID, "start_line": 10, "end_line": 10}],
                "requirement_refs": [],
            }
            for index, candidate_id in enumerate(self.candidate_ids)
        ]
        return VerifierReply(output={"decisions": decisions}, accepted_call_id=ACCEPTED_CALL_ID)


def _execute(findings: list[dict[str, Any]], verifier: _ReplyVerifier) -> VerifiedReviewOutput:
    publisher = _Publisher()
    pipeline = ExecuteReviewRun(
        _Processor(),
        _Repository(),
        _Model(findings),
        publisher,
        reflection_pass=ReflectionPass(verifier),
    )
    assert asyncio.run(pipeline.execute(RUN_ID)) is True
    assert len(publisher.outputs) == 1
    output = publisher.outputs[0]
    assert isinstance(output, VerifiedReviewOutput)
    return output


# ---------- ExecuteReviewRun builds the trace ----------


def test_verified_output_carries_the_llm_verify_trace_without_reason(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Each id this module draws sorts before the previous one, so prompt order is not sorted.
    countdown = itertools.count()
    monkeypatch.setattr(
        execute_review_module,
        "uuid",
        SimpleNamespace(uuid4=lambda: UUID(int=2**64 - next(countdown))),
    )
    verifier = _ReplyVerifier()

    with caplog.at_level(logging.INFO):
        output = _execute([_finding(10, "SQL Injection"), _finding(11, "Path traversal")], verifier)

    assert len(verifier.candidate_ids) == 2
    first, second = verifier.candidate_ids
    assert first > second
    assert output.verify_request == {
        "candidate_ids": [first, second],
        "accepted_call_id": "00000000-0000-0000-0000-000000013902",
        "manifest_id": None,
    }
    assert output.verify_response == {
        "decisions": [
            {
                "candidate_id": first,
                "status": "supported",
                "evidence_refs": [{"block_id": MAIN_BLOCK_ID, "start_line": 10, "end_line": 10}],
                "requirement_refs": [],
            },
            {
                "candidate_id": second,
                "status": "contradicted",
                "evidence_refs": [{"block_id": MAIN_BLOCK_ID, "start_line": 10, "end_line": 10}],
                "requirement_refs": [],
            },
        ]
    }
    logs = [r.message for r in caplog.records if "Reflection pass completed" in r.message]
    assert len(logs) == 1
    assert "verifier call 00000000-0000-0000-0000-000000013902" in logs[0]


def test_the_zero_candidate_bypass_writes_no_trace() -> None:
    verifier = _ReplyVerifier()

    output = _execute([], verifier)

    assert verifier.candidate_ids == []
    assert output.verify_request is None
    assert output.verify_response is None


# ---------- StoreReviewOutput passes it to enter_publishing ----------


class _PublishingRepository:
    def __init__(self) -> None:
        self.entered: list[dict[str, Any]] = []

    async def get_post_processing_input(self, run_id: UUID) -> FindingPostProcessingInput:
        return FindingPostProcessingInput(
            diff_map=_diff_map(), rule_names=frozenset(), allowed_v2_rules=frozenset()
        )

    async def lock_for_publishing(self, run_id: UUID) -> PublishingGuard:
        return PublishingGuard(
            state="running",
            worker_id="test-worker",
            cancel_requested=False,
            head_sha="head_sha_123",
            head_current=True,
            repository_review_event="APPROVE",
        )

    async def store_review_output(
        self,
        run_id: UUID,
        answer: dict[str, Any],
        parsed: ReviewOutput,
        processed: ProcessedReviewOutput,
    ) -> ReviewPublication:
        return ReviewPublication(head_sha="head_sha_123", findings=(), review_body="body")

    async def enter_publishing(self, run_id: UUID, **kwargs: Any) -> None:
        self.entered.append(kwargs)


class _UnitOfWork:
    def __init__(self, reviews: _PublishingRepository) -> None:
        self.reviews = reviews

    async def __aenter__(self) -> _UnitOfWork:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        pass

    async def commit(self) -> None:
        pass

    async def rollback(self) -> None:
        pass


class _Queue:
    async def publish_review(self, pointer: Any) -> None:
        pass


def _store(raw_output: Any) -> list[dict[str, Any]]:
    reviews = _PublishingRepository()
    store = StoreReviewOutput(
        cast(Any, lambda: _UnitOfWork(reviews)), _Queue(), worker_id="test-worker"
    )
    assert asyncio.run(store.execute(RUN_ID, raw_output)) is True
    return reviews.entered


def test_store_passes_the_trace_to_enter_publishing() -> None:
    request = {
        "candidate_ids": ["c-1"],
        "accepted_call_id": "00000000-0000-0000-0000-000000013902",
        "manifest_id": None,
    }
    response = {"decisions": [{"candidate_id": "c-1", "status": "contradicted"}]}

    entered = _store(
        VerifiedReviewOutput(
            {"findings": [], "summary": SUMMARY},
            verify_request=request,
            verify_response=response,
        )
    )

    assert len(entered) == 1
    assert entered[0]["verify_request"] == request
    assert entered[0]["verify_response"] == response


def test_a_plain_answer_passes_no_trace() -> None:
    entered = _store({"findings": [], "summary": SUMMARY})

    assert len(entered) == 1
    assert entered[0]["verify_request"] is None
    assert entered[0]["verify_response"] is None
