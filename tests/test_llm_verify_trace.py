"""The ``llm.verify`` trace of the Reflection Pass (#130 PR E, R13 narrowed).

``ExecuteReviewRun`` hands the verifier's candidate ids, the accepted call id and the
decisions without ``reason`` to ``StoreReviewOutput`` on ``VerifiedReviewOutput``; the store
passes them to ``finalize_review`` before T8, which writes the ``llm.verify`` run action
(``docs/PIPELINE_SPEC.md:177``; ``manifest_id`` names the stored manifest of the accepted
verifier call, null when no manifest store is wired).
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import re
from datetime import datetime
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID

import pytest

import app.modules.reviews.application.execute_review as execute_review_module
from app.modules.reviews.application.execute_review import ExecuteReviewRun
from app.modules.reviews.application.findings_post_processor import ProcessedReviewOutput
from app.modules.reviews.application.reflection_pass import ReflectionPass, VerifierReply
from app.modules.reviews.application.review_output import (
    FindingPostProcessingInput,
    VerifiedReviewOutput,
)
from app.modules.reviews.application.store_review_output import (
    PublishingGuard,
    StoreReviewOutput,
)
from tests.execute_review_fakes import _AcceptingCheckpointPublisher
from tests.review_output_fakes import InMemoryFinalization
from tests.test_diff_anchor import _diff_map
from tests.verifier_pipeline_fakes import (
    MAIN_BLOCK_ID,
    RUN_ID,
    SUMMARY,
    FakeModel,
    FakeProcessor,
    FakePublisher,
    FakeQueue,
    FakeRepository,
    FakeUnitOfWork,
)

ACCEPTED_CALL_ID = UUID("00000000-0000-0000-0000-000000013902")
ACCEPTED_MANIFEST_ID = UUID("00000000-0000-0000-0000-000000013903")


class _Publisher(_AcceptingCheckpointPublisher, FakePublisher):
    pass


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


class _ReplyVerifier:
    """Supports the first candidate, contradicts the rest, names an accepted call."""

    def __init__(self, manifest_id: UUID | None = None) -> None:
        self.candidate_ids: list[str] = []
        self.manifest_id = manifest_id

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
        return VerifierReply(
            output={"decisions": decisions},
            accepted_call_id=ACCEPTED_CALL_ID,
            manifest_id=self.manifest_id,
        )


def _execute(findings: list[dict[str, Any]], verifier: _ReplyVerifier) -> VerifiedReviewOutput:
    publisher = _Publisher()
    pipeline = ExecuteReviewRun(
        FakeProcessor(),
        FakeRepository(),
        FakeModel(findings),
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


def test_llm_verify_names_the_stored_manifest_of_the_accepted_verifier_call() -> None:
    output = _execute([_finding(10, "SQL Injection")], _ReplyVerifier(ACCEPTED_MANIFEST_ID))

    assert output.verify_request is not None
    assert output.verify_request["accepted_call_id"] == "00000000-0000-0000-0000-000000013902"
    assert output.verify_request["manifest_id"] == "00000000-0000-0000-0000-000000013903"


def test_the_zero_candidate_bypass_writes_no_trace() -> None:
    verifier = _ReplyVerifier()

    output = _execute([], verifier)

    assert verifier.candidate_ids == []
    assert output.verify_request is None
    assert output.verify_response is None


# ---------- StoreReviewOutput saves the trace before T8 ----------


class _PublishingRepository(InMemoryFinalization):
    def __init__(self) -> None:
        self.finalized_trace: list[tuple[dict[str, Any] | None, dict[str, Any] | None]] = []
        self.phases: list[str] = []

    async def get_post_processing_input(self, run_id: UUID) -> FindingPostProcessingInput:
        return FindingPostProcessingInput(
            diff_map=_diff_map(),
            checked_paths=frozenset(),
            rule_names=frozenset(),
            allowed_v2_rules=frozenset(),
        )

    async def lock_for_publishing(self, run_id: UUID) -> PublishingGuard:
        return PublishingGuard(
            state="running",
            worker_id="test-worker",
            cancel_requested=False,
            head_sha="head_sha_123",
            head_current=True,
            repository_review_event="APPROVE",
            attempt=1,
        )

    async def finalize_review(
        self,
        run_id: UUID,
        *,
        model_output: dict[str, object] | None,
        processed: ProcessedReviewOutput,
        result: dict[str, Any],
        review_event: str,
        findings_hash: str,
        verify_request: dict[str, Any] | None,
        verify_response: dict[str, Any] | None,
        started_at: datetime,
        duration_ms: int,
    ) -> None:
        await super().finalize_review(
            run_id,
            model_output=model_output,
            processed=processed,
            result=result,
            review_event=review_event,
            findings_hash=findings_hash,
            verify_request=verify_request,
            verify_response=verify_response,
            started_at=started_at,
            duration_ms=duration_ms,
        )
        self.finalized_trace.append((verify_request, verify_response))
        self.phases.append("finalize")

    async def enter_publishing_from_final(self, run_id: UUID, *, lease_until: datetime) -> bool:
        assert self.phases == ["finalize"]
        self.phases.append("t8")
        return await super().enter_publishing_from_final(run_id, lease_until=lease_until)


def _store(raw_output: Any) -> _PublishingRepository:
    reviews = _PublishingRepository()
    store = StoreReviewOutput(
        cast(Any, lambda: FakeUnitOfWork(reviews)),
        FakeQueue(),
        worker_id="test-worker",
        expected_attempt=1,
        run_url=lambda _: None,
    )
    assert asyncio.run(store.execute(RUN_ID, raw_output)) is True
    return reviews


def test_store_saves_the_trace_before_t8() -> None:
    request = {
        "candidate_ids": ["c-1"],
        "accepted_call_id": "00000000-0000-0000-0000-000000013902",
        "manifest_id": None,
    }
    response = {"decisions": [{"candidate_id": "c-1", "status": "contradicted"}]}

    reviews = _store(
        VerifiedReviewOutput(
            {"findings": [], "summary": SUMMARY},
            verify_request=request,
            verify_response=response,
        )
    )

    assert reviews.phases == ["finalize", "t8"]
    assert reviews.finalized_trace == [(request, response)]


def test_a_plain_answer_passes_no_trace() -> None:
    reviews = _store({"findings": [], "summary": SUMMARY})

    assert reviews.phases == ["finalize", "t8"]
    assert reviews.finalized_trace == [(None, None)]
