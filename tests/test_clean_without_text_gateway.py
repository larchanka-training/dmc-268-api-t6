"""The real gateway's legacy fit reaches deterministic result finalization."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from typing import Any
from uuid import UUID

import httpx
import pytest

from app.modules.reviews.application.execute_review import ExecuteReviewRun, ReviewPromptInput
from app.modules.reviews.application.prompt_builder import (
    ChangedFile,
    DiffLine,
    PullRequestMeta,
    RepoConventions,
    file_block_id,
)
from app.modules.reviews.application.reflection_pass import ReflectionPass
from app.modules.reviews.application.review_output import VerifiedReviewOutput
from app.modules.reviews.application.store_review_output import StoreReviewOutput
from app.modules.reviews.infrastructure.llm.models import GatewayReviewModel
from tests.test_clean_without_text_result import Queue, Repository, Uow, answer
from tests.test_clean_without_text_sent_context import finding
from tests.test_execute_review import FakeVerifierModel, conventions
from tests.test_gateway_review_model import RUN, Transport, _gateway, _snapshot
from tests.test_llm_gateway import PRIMARY, Harness, completion
from tests.test_run_coverage import code_snapshot, coverage


def test_gateway_hidden_fit_marks_legacy_unverified_output_and_persisted_result_partial() -> None:
    async def exercise() -> None:
        harness = Harness(
            [completion(json.dumps(answer()))],
            primary=replace(PRIMARY, context_window=4000),
            fallback=None,
        )
        repository = Repository(coverage([code_snapshot()]))
        committed: list[str] = []
        queue = Queue(committed)
        store = StoreReviewOutput(
            lambda: Uow(repository, committed), queue, worker_id="worker", run_url=lambda _: None
        )
        lines = tuple(DiffLine(i + 1, "added", f"line_{i} = '{'x' * 70}'") for i in range(400))

        class Processor:
            async def prepare(self, run_id: UUID) -> Any:
                return conventions()

        class Prompts:
            async def get_review_prompt_input(self, run_id: UUID, saved: Any) -> ReviewPromptInput:
                return ReviewPromptInput(
                    "Review carefully.",
                    (),
                    None,
                    RepoConventions((), ()),
                    (ChangedFile("src/app.py", "added", lines),),
                    (),
                )

        class Meta:
            async def get_pull_request_meta(self, run_id: UUID) -> PullRequestMeta:
                return PullRequestMeta("PR", None, "u", "b", "m", (), 1, 400, 0, False, False)

        class Publisher:
            async def execute(self, run_id: UUID, raw_output: Any) -> bool:
                assert isinstance(raw_output, VerifiedReviewOutput)
                assert raw_output.incomplete_paths == ("src/app.py",)
                assert raw_output.severity_threshold is None
                assert raw_output.allowed_v2_rules is None
                assert raw_output.dropped_findings == ()
                return await store.execute(run_id, raw_output)

        async with httpx.AsyncClient(transport=httpx.MockTransport(harness._handle)) as client:
            model = GatewayReviewModel(harness.gateway(client), harness.run, Meta())
            assert await ExecuteReviewRun(Processor(), Prompts(), model, Publisher()).execute(
                UUID(int=213)
            )
            assert model.last_result is not None and model.last_result.output == answer()
            assert model.last_result.accepted_call_id is not None
            assert model.last_result.usage and model.last_result.calls == 1
        assert len(harness.requests) == 1
        sent = json.loads(harness.requests[0].content)["messages"][1]["content"]
        assert "line_399" not in sent and "line_0" in sent
        assert repository.response["coverage"]["status"] == "partial"
        assert repository.response["coverage"]["reviewed_files"] == 0
        assert repository.response["coverage"]["reason_counts"] == {"review_input_incomplete": 1}
        assert repository.response["verdict"] is None
        assert "**Verdict:** clean" not in repository.body

    asyncio.run(exercise())


@pytest.mark.parametrize("fit", [False, True])
@pytest.mark.parametrize("candidates", ["empty", "supported", "unknown_path"])
def test_snapshot_reply_fit_and_reflection_keep_the_final_coverage(
    fit: bool,
    candidates: str,
) -> None:
    """The sole adapter fit and both reflection returns reach durable publication."""
    lines = tuple(DiffLine(i, "added", f"line_{i} = '{'x' * 70}'") for i in range(1, 401))
    files = (ChangedFile("src/app.py", "added", lines if fit else lines[:1]),)
    saved = code_snapshot()
    repository = Repository(coverage([saved]))
    committed: list[str] = []
    queue = Queue(committed)
    store = StoreReviewOutput(
        lambda: Uow(repository, committed), queue, worker_id="worker", run_url=lambda _: None
    )
    output = answer()
    if candidates != "empty":
        path = "private/unknown.py" if candidates == "unknown_path" else "src/app.py"
        item = finding(path)
        item["evidence_refs"] = [
            {"block_id": file_block_id("src/app.py"), "start_line": 1, "end_line": 1}
        ]
        output["findings"] = [item]
    transport = Transport(json.dumps(output))
    verifier = FakeVerifierModel(
        {
            "decisions": [
                {
                    "candidate_id": "replaced-by-the-fake",
                    "status": "supported",
                    "reason": "Supported by the changed line.",
                    "evidence_refs": [
                        {"block_id": file_block_id("src/app.py"), "start_line": 1, "end_line": 1}
                    ],
                    "requirement_refs": [],
                }
            ]
        }
    )

    class Inputs:
        async def prepare(self, run_id: UUID) -> Any:
            return conventions()

        async def get_review_prompt_input(self, run_id: UUID, saved: Any) -> ReviewPromptInput:
            return ReviewPromptInput(
                "Review carefully.",
                (),
                None,
                RepoConventions((), ()),
                files,
                (),
                policy_snapshot=_snapshot(max_input_tokens=12_000),
            )

        async def get_pull_request_meta(self, run_id: UUID) -> PullRequestMeta:
            return PullRequestMeta(
                "PR", None, "u", "b", "m", (), 1, len(files[0].lines), 0, False, False
            )

    inputs = Inputs()
    model = GatewayReviewModel(_gateway(transport), RUN, inputs)
    assert asyncio.run(
        ExecuteReviewRun(
            inputs, inputs, model, store, reflection_pass=ReflectionPass(verifier)
        ).execute(UUID(int=213))
    )
    [request] = transport.requests
    shown = request.messages[1].content.count('type="added"')
    assert (0 < shown < 400) if fit else shown == 1
    assert model.last_result is not None and model.last_result.output == output
    assert set(model.last_result.output) == {"findings", "summary"}
    assert verifier.call_count == int(candidates == "supported")
    reasons = {}
    if fit:
        reasons["review_input_incomplete"] = 1
    if candidates == "unknown_path":
        reasons["unknown_path"] = 1
    partial = bool(reasons)
    assert repository.response["coverage"]["status"] == ("partial" if partial else "complete")
    assert repository.response["coverage"]["reviewed_files"] == (0 if fit else 1)
    assert repository.response["coverage"]["reason_counts"] == reasons
    expected_verdict = "blocking" if candidates == "supported" else None if partial else "clean"
    assert repository.response["verdict"] == expected_verdict
    assert "private/unknown.py" not in repository.body
    assert ("**Verdict:** clean" in repository.body) == (expected_verdict == "clean")
