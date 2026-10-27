"""Actual budgeted execution survives durable reread, recovery and HTTP views."""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.modules.auth.application.scope import AuthScope
from app.modules.reviews.application.check_runs import check_run_view
from app.modules.reviews.application.execute_review import (
    ExecuteReviewRun,
    ReviewPromptInput,
    ReviewReply,
)
from app.modules.reviews.application.get_run import GetRunDetail
from app.modules.reviews.application.prompt_builder import (
    PullRequestMeta,
    ReviewContext,
    file_block_id,
)
from app.modules.reviews.application.reflection_pass import ReflectionPass, VerifierReply
from app.modules.reviews.application.review_output import VerifiedReviewOutput
from app.modules.reviews.application.store_review_output import StoreReviewOutput
from app.modules.reviews.infrastructure.llm.models import GatewayReviewModel
from app.modules.reviews.infrastructure.models import Run, RunAction
from app.modules.reviews.infrastructure.review_output_unit_of_work import (
    SqlAlchemyReviewOutputUnitOfWork,
)
from app.modules.reviews.infrastructure.review_prompt_repository import (
    SqlAlchemyReviewPromptRepository,
)
from app.modules.reviews.infrastructure.run_lifecycle_store import SqlAlchemyRunLifecycleStore
from app.modules.reviews.infrastructure.run_repository import SqlAlchemyRunRepository
from tests.portal_postgres import HEAD, REPO_A, RUN_ATTEMPTED, WS_A, Env, api
from tests.test_clean_without_text_result import answer
from tests.test_clean_without_text_result_postgres import prepare_result
from tests.test_clean_without_text_result_postgres import result_env as result_env
from tests.test_clean_without_text_sent_context import finding
from tests.test_execute_review import _make_test_snapshot, conventions
from tests.test_gateway_review_model import RUN, Transport, _gateway
from tests.test_review_prompt_pins_postgres import _bind_v4
from tests.test_run_coverage import code_snapshot
from tests.test_severity_threshold_postgres import RecordingQueue


@pytest.mark.integration
@pytest.mark.parametrize(
    "case", ["budget_empty", "budget_info", "unknown_path", "model_fit_empty", "model_fit_info"]
)
def test_actual_execution_partial_result_survives_postgres_http_checkrun_and_recovery(
    result_env: Env,
    case: str,
) -> None:
    async def exercise() -> dict[str, Any]:
        engine = result_env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        scope = AuthScope(42, (WS_A,))
        try:
            patch = "@@ -0,0 +1,400 @@\n" + "\n".join(f"+line_{i} = {i}" for i in range(400))
            snapshot = replace(
                code_snapshot(),
                patch=patch,
                review_patch=patch,
                status="added",
                additions=400,
                deletions=0,
                changes=400,
            )
            await prepare_result(factory, [snapshot], referenced=True, prompt_trace=True)
            # F1 chooses the path from the actual pinned prompt. The fixture v4 row is
            # inactive: this test never activates or changes the production seed.
            review_prompt_id = await _bind_v4(factory, RUN_ATTEMPTED)
            async with SqlAlchemyReviewOutputUnitOfWork(factory) as uow:
                before = await uow.reviews.get_post_processing_input(RUN_ATTEMPTED)
            assert before is not None and before.coverage is not None
            assert (
                before.coverage.status.value == "complete" and before.coverage.reviewed_files == 1
            )
            prompts = SqlAlchemyReviewPromptRepository(factory)

            class Processor:
                async def prepare(self, run_id: UUID) -> Any:
                    return conventions()

            class Prompts:
                async def get_review_prompt_input(
                    self, run_id: UUID, saved: Any
                ) -> ReviewPromptInput:
                    value = await prompts.get_review_prompt_input(run_id, saved)
                    assert value is not None and len(value.changed_files[0].lines) == 400
                    assert value.review_binding is not None
                    assert value.review_binding.prompt_version_id == review_prompt_id
                    assert value.review_binding.review_system_version == "v4"
                    # The packaged v4 needs fixed-prompt room: 18k minus the 8k output
                    # reserve leaves 10k input tokens, still below this full 400-line patch.
                    policy = _make_test_snapshot(
                        max_input_tokens=60_000
                        if case in {"unknown_path", "model_fit_empty", "model_fit_info"}
                        else 18_000
                    )
                    policy = replace(
                        policy,
                        run_id=RUN_ATTEMPTED,
                        repository_id=REPO_A,
                        prompt_bindings=replace(
                            policy.prompt_bindings,
                            review_system_id=review_prompt_id,
                            review_system_key=value.review_binding.key,
                            review_system_version=value.review_binding.review_system_version,
                            review_system_checksum=sha256(value.system.encode()).hexdigest(),
                        ),
                    )
                    return replace(value, policy_snapshot=policy)

            class Model:
                async def get_pull_request_meta(self, run_id: UUID) -> PullRequestMeta:
                    return PullRequestMeta(
                        "PR", None, "u", "b", "m", (), 1, 400, 0, False, False, head_sha=HEAD
                    )

                async def draft_review(
                    self, *, context: ReviewContext
                ) -> dict[str, object] | VerifiedReviewOutput | ReviewReply:
                    # ExecuteReviewRun no longer fits: the gateway adapter is the sole
                    # fit point. All cases begin with the full durable 400-line patch.
                    assert len(context.changed_files[0].lines) == 400
                    value = answer()
                    if case in {"budget_info", "unknown_path", "model_fit_info"}:
                        path = "@private/hidden.py" if case == "unknown_path" else "src/app.py"
                        item = finding(path, "info")
                        item["evidence_refs"] = [
                            {"block_id": file_block_id(path), "start_line": 1, "end_line": 1}
                        ]
                        value["findings"] = [item]
                    if case.startswith("budget"):
                        transport = Transport(json.dumps(value))
                        reply = await GatewayReviewModel(
                            _gateway(transport), replace(RUN, run_id=RUN_ATTEMPTED), self
                        ).draft_review(context=context)
                        assert isinstance(reply, ReviewReply)
                        assert 0 < len(reply.context.changed_files[0].lines) < 400
                        [request] = transport.requests
                        wire = request.messages[1].content
                        assert "line_0" in wire and "line_399" not in wire
                        return reply
                    if case.startswith("model_fit"):
                        return VerifiedReviewOutput(value, incomplete_paths=("src/app.py",))
                    return value

            class Verifier:
                async def verify(
                    self, *, system_prompt: str, user_prompt: str, **_: object
                ) -> VerifierReply:
                    ids = re.findall(r'candidate_id="([^"]+)"', user_prompt)
                    return VerifierReply(
                        {
                            "decisions": [
                                {
                                    "candidate_id": cid,
                                    "status": "supported",
                                    "reason": "OK",
                                    "evidence_refs": [
                                        {
                                            "block_id": file_block_id("src/app.py"),
                                            "start_line": 1,
                                            "end_line": 1,
                                        }
                                    ],
                                    "requirement_refs": [],
                                }
                                for cid in ids
                            ]
                        },
                        accepted_call_id=UUID(int=130213),
                    )

            queue = RecordingQueue()
            assert await ExecuteReviewRun(
                Processor(),
                Prompts(),
                Model(),
                StoreReviewOutput(
                    lambda: SqlAlchemyReviewOutputUnitOfWork(factory),
                    queue,
                    worker_id="worker",
                    run_url=lambda _: None,
                ),
                reflection_pass=ReflectionPass(Verifier()),
            ).execute(RUN_ATTEMPTED)
            reason = "unknown_path" if case == "unknown_path" else "review_input_incomplete"
            async with factory.begin() as session:
                action = await session.scalar(
                    select(RunAction).where(
                        RunAction.run_id == RUN_ATTEMPTED, RunAction.tool == "review.postprocess"
                    )
                )
                run = await session.get(Run, RUN_ATTEMPTED)
                assert action is not None and isinstance(action.response, dict) and run is not None
                actions = list(
                    (
                        await session.scalars(
                            select(RunAction)
                            .where(RunAction.run_id == RUN_ATTEMPTED)
                            .order_by(RunAction.index)
                        )
                    ).all()
                )
                verifies = [item for item in actions if item.tool == "llm.verify"]
                if case in {"budget_info", "model_fit_info"}:
                    assert len(verifies) == 1
                    verified = verifies[0]
                    assert verified.index < action.index
                    assert isinstance(verified.request, dict)
                    assert verified.request["accepted_call_id"] == str(UUID(int=130213))
                    assert verified.request["manifest_id"] is None
                    assert len(verified.request["candidate_ids"]) == 1
                    assert isinstance(verified.response, dict)
                    assert verified.response["decisions"][0]["status"] == "supported"
                    assert "reason" not in verified.response["decisions"][0]
                else:
                    assert verifies == []
                result = action.response
                assert result["coverage"]["status"] == "partial"
                assert result["coverage"]["reviewed_files"] == (1 if case == "unknown_path" else 0)
                assert result["coverage"]["reason_counts"] == {reason: 1}
                assert result["verdict"] is None
                assert (
                    run.review_body is not None and result["summary"]["problem"] in run.review_body
                )
                assert (
                    "**Verdict:** clean" not in run.review_body
                    and "@private" not in run.review_body
                )
                run.lease_until = datetime.now(UTC) - timedelta(seconds=1)
            await engine.dispose()
            async with factory() as session:
                assert await SqlAlchemyRunLifecycleStore(session).expired_publishing(
                    datetime.now(UTC), 10
                ) == tuple(queue.pointers)
            async with SqlAlchemyReviewOutputUnitOfWork(factory) as uow:
                await uow.reviews.mark_review_published(RUN_ATTEMPTED)
                await uow.commit()
            detail = await GetRunDetail(SqlAlchemyRunRepository(factory, scope=scope)).execute(
                RUN_ATTEMPTED
            )
            async with factory() as session:
                report = await SqlAlchemyRunLifecycleStore(session).check_run_report(RUN_ATTEMPTED)
            assert detail is not None and report is not None
            assert detail.verdict is report.verdict is None
            assert detail.review.summary == report.saved_summary == result["summary"]
            assert check_run_view(report).title == "AI-ревью: проверка неполная"
            assert result["summary"]["problem"] in check_run_view(report).summary
            return result
        finally:
            await engine.dispose()

    result = asyncio.run(exercise())
    with api(result_env, AuthScope(42, (WS_A,))) as (client, _):
        detail = client.get(f"/api/runs/{RUN_ATTEMPTED}")
        pulls = client.get(f"/api/repos/{REPO_A}/pulls")
        assert detail.status_code == pulls.status_code == 200
        latest = next(item["latestRun"] for item in pulls.json()["items"] if item["number"] == 3)
        assert detail.json()["verdict"] is latest["verdict"] is None
        assert detail.json()["summary"]["problem"] == result["summary"]["problem"]
