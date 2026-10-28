"""Actual context stage through durable finalization, HTTP and recovery (#213)."""

from __future__ import annotations

import asyncio
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.modules.auth.application.scope import AuthScope
from app.modules.reviews.application.ast_resolution import RevisionTree
from app.modules.reviews.application.check_runs import RunProviderCheckpoint, check_run_view
from app.modules.reviews.application.conventions import CachedConventions, GeneratedConventions
from app.modules.reviews.application.get_run import GetRunDetail
from app.modules.reviews.application.get_run_diff import StoreDiffSnapshot
from app.modules.reviews.application.list_pulls import ListRepositoryPulls
from app.modules.reviews.application.prepare_policy_snapshot import PreparePolicySnapshot
from app.modules.reviews.application.review_context import (
    BuildReviewContext,
    ContextStatus,
    SummaryOnlyReview,
)
from app.modules.reviews.application.review_context_stage import (
    EstimatedTokenCounter,
    ReviewContextStage,
)
from app.modules.reviews.application.run_trace import TransactionalRunTrace
from app.modules.reviews.application.store_review_output import StoreReviewOutput
from app.modules.reviews.application.vcs_diff import PullRequestLocator, VcsPullRequest
from app.modules.reviews.infrastructure.models import Run, RunAction
from app.modules.reviews.infrastructure.policy_snapshot_unit_of_work import (
    SqlAlchemyPolicySnapshotUnitOfWork,
)
from app.modules.reviews.infrastructure.pull_request_queries import SqlAlchemyPullRequestQueries
from app.modules.reviews.infrastructure.review_output_unit_of_work import (
    SqlAlchemyReviewOutputUnitOfWork,
)
from app.modules.reviews.infrastructure.review_prompt_repository import (
    SqlAlchemyReviewPromptRepository,
)
from app.modules.reviews.infrastructure.run_lifecycle_store import (
    SqlAlchemyRunLifecycleStore,
    SqlAlchemyRunTraceUnitOfWork,
)
from app.modules.reviews.infrastructure.run_processing_unit_of_work import (
    SqlAlchemyRunProcessingUnitOfWork,
)
from app.modules.reviews.infrastructure.run_repository import SqlAlchemyRunRepository
from tests.portal_postgres import HEAD, PR_NEW, PROMPT, REPO_A, RUN_ATTEMPTED, WS_A, Env, api
from tests.test_clean_without_text_result import answer
from tests.test_clean_without_text_result_postgres import result_env as result_env
from tests.test_review_context_stage import Parser, Policy, Vcs
from tests.test_run_coverage import code_snapshot, empty_snapshot
from tests.test_severity_threshold_postgres import RecordingQueue


class RevisionSource:
    async def tree(self, revision: str) -> RevisionTree:
        return RevisionTree(
            revision, {"src/app.py": "d" * 40 if revision == HEAD else "c" * 40}, complete=True
        )

    async def read_blob(self, blob_sha: str) -> bytes:
        assert blob_sha in {"c" * 40, "d" * 40}
        return b"new\n" if blob_sha == "d" * 40 else b"old\n"


async def checkpoint() -> None:
    return None


@pytest.mark.integration
@pytest.mark.parametrize(
    ("marker", "inventory_complete", "expected"),
    [("empty_file", True, "clean"), ("missing_patch", True, None), ("empty_file", False, None)],
)
@pytest.mark.parametrize("live_base_sha", ["b" * 40, "9" * 40], ids=["pinned_base", "moved_base"])
def test_actual_stage_persisted_empty_proof_controls_http_and_recovery_result(
    result_env: Env,
    marker: str,
    inventory_complete: bool,
    expected: str | None,
    live_base_sha: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def exercise() -> dict[str, Any]:
        engine = result_env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        scope = AuthScope(42, (WS_A,))
        try:
            original = empty_snapshot()
            candidate = replace(original, omission_reason=marker)
            # The pair changes exactly one durable proof field, including the empty SHA.
            assert asdict(candidate) | {"omission_reason": "empty_file"} == asdict(original)
            async with factory.begin() as session:
                await session.execute(
                    text("UPDATE runs SET state='running', worker_id='worker' WHERE id=:run"),
                    {"run": RUN_ATTEMPTED},
                )
            async with SqlAlchemyRunProcessingUnitOfWork(factory) as processing_uow:
                await StoreDiffSnapshot(processing_uow.repository).execute(
                    run_id=RUN_ATTEMPTED,
                    code_change_id=PR_NEW,
                    head_sha=HEAD,
                    files=[code_snapshot(), candidate],
                )
                await processing_uow.commit()
            await engine.dispose()
            runs = SqlAlchemyRunRepository(factory, scope=scope)
            reloaded = await runs.get_run_snapshots(RUN_ATTEMPTED)
            assert reloaded is not None
            stored_empty = next(item for item in reloaded if item.filename == candidate.filename)
            assert stored_empty == candidate
            trace = TransactionalRunTrace(lambda: SqlAlchemyRunTraceUnitOfWork(factory))
            vcs = Vcs(head_sha=HEAD, files_changed=2 if inventory_complete else 3)
            original_pull = vcs.get_pull_request

            async def live_pull(
                locator: PullRequestLocator, *, checkpoint: RunProviderCheckpoint
            ) -> VcsPullRequest:
                return replace(
                    await original_pull(locator, checkpoint=checkpoint), base_sha=live_base_sha
                )

            monkeypatch.setattr(vcs, "get_pull_request", live_pull)
            stage = ReviewContextStage(
                runs=runs,
                checkpoint=checkpoint,
                vcs=vcs,
                merge_base=vcs,
                prepare_policy=PreparePolicySnapshot(
                    uow_factory=lambda: SqlAlchemyPolicySnapshotUnitOfWork(factory),
                    source_factory=lambda pins: Policy(rules=None),
                ),
                context_builder=lambda run: BuildReviewContext(
                    source=RevisionSource(), parser=Parser(), counter=EstimatedTokenCounter()
                ),
                trace=trace,
                payloads=trace,
            )
            try:
                payload = await stage.run(RUN_ATTEMPTED)
            except SummaryOnlyReview:
                # #313: an incomplete list is recorded and stops before the builder.
                payload = None
            if inventory_complete:
                assert payload is not None
                assert payload.status is ContextStatus.REVIEW
                assert payload.blocks and {block.source.path for block in payload.blocks} == {
                    "src/app.py"
                }
            else:
                assert payload is None
            async with factory() as session:
                context = await session.scalar(
                    select(RunAction).where(
                        RunAction.run_id == RUN_ATTEMPTED, RunAction.tool == "context.build"
                    )
                )
                assert context is not None and isinstance(context.response, dict)
                recorded = context.response
            classification = recorded["coverage"]
            assert classification["head_sha"] == HEAD
            if payload is not None:
                assert (
                    classification["diff_digest"]
                    == payload.diff_digest
                    == recorded["diff"]["digest"]
                )
            else:
                assert recorded["status"] == "summary_only"
                assert recorded["reason"] == "diff_inventory_incomplete"
                assert recorded["blocks"] == []
                assert classification["diff_digest"] == recorded["diff"]["digest"]
            assert len(classification["snapshot_digest"]) == 64
            assert classification["status"] == ("complete" if expected else "partial")
            assert classification["reviewed_files"] == 1
            expected_reasons = (
                {"diff_inventory_incomplete": 1}
                if not inventory_complete
                else {}
                if marker == "empty_file"
                else {"missing_patch": 1}
            )
            assert classification["file_list_complete"] is inventory_complete
            assert classification["exclusion_counts"] == (
                {"empty_file": 1} if marker == "empty_file" else {}
            )
            assert classification["reason_counts"] == expected_reasons
            # The real later prompt read records nothing (#168 part 6): the stage's
            # context.build stays the attempt's only one, and its response, coverage
            # included, is the run's context_payloads row.
            prompt = await SqlAlchemyReviewPromptRepository(factory).get_review_prompt_input(
                RUN_ATTEMPTED,
                GeneratedConventions(
                    CachedConventions(REPO_A, None, PROMPT, (), (), {}), None, True
                ),
            )
            assert prompt is not None
            if inventory_complete:
                assert [item.path for item in prompt.changed_files] == ["src/app.py"]
            else:
                # The stop clears review_patch and sets the durable flag, so nothing is model input.
                assert prompt.changed_files == ()
                assert set(prompt.omitted_files) == {"src/app.py", candidate.filename}
            async with factory() as session:
                contexts = (
                    await session.scalars(
                        select(RunAction)
                        .where(RunAction.run_id == RUN_ATTEMPTED, RunAction.tool == "context.build")
                        .order_by(RunAction.index)
                    )
                ).all()
                [(schema_version, stored)] = (
                    await session.execute(
                        text(
                            "SELECT schema_version, summary FROM context_payloads WHERE run_id=:id"
                        ),
                        {"id": RUN_ATTEMPTED},
                    )
                ).all()
            assert [item.response for item in contexts] == [recorded]
            assert (schema_version, stored) == (2, recorded)
            assert stored["coverage"] == classification
            async with SqlAlchemyReviewOutputUnitOfWork(factory) as uow:
                post_input = await uow.reviews.get_post_processing_input(RUN_ATTEMPTED)
            assert post_input is not None and post_input.coverage is not None
            assert post_input.coverage.reason_counts == expected_reasons
            assert post_input.diff_map.file_list_complete is inventory_complete
            assert post_input.coverage.file_list_complete is inventory_complete
            queue = RecordingQueue()
            assert await StoreReviewOutput(
                lambda: SqlAlchemyReviewOutputUnitOfWork(factory),
                queue,
                worker_id="worker",
                run_url=lambda _: None,
            ).execute(RUN_ATTEMPTED, answer())
            assert len(queue.pointers) == 1 and queue.pointers[0].review_event == "COMMENT"
            async with factory.begin() as session:
                run = await session.get(Run, RUN_ATTEMPTED)
                saved = await session.scalar(
                    select(RunAction).where(
                        RunAction.run_id == RUN_ATTEMPTED, RunAction.tool == "review.postprocess"
                    )
                )
                assert run is not None and run.state.value == "publishing"
                assert saved is not None and isinstance(saved.response, dict)
                result = saved.response
                assert result["coverage"] == classification and result["verdict"] == expected
                assert (
                    run.review_body is not None and result["summary"]["problem"] in run.review_body
                )
                assert ("**Verdict:** clean" in run.review_body) is bool(expected)
                if expected is None and inventory_complete:
                    assert "Проверка неполная" in run.review_body
                    assert "Нет подтверждения обычного пустого файла" in run.review_body
                if not inventory_complete:
                    assert "список изменённых файлов неполон" in run.review_body
                    assert result["coverage"]["file_list_complete"] is False
                    assert result["coverage"]["reason_counts"] == {"diff_inventory_incomplete": 1}
                    assert result["summary"]["problem"] == (
                        "Построчное AI-ревью не выполнено: список изменённых файлов неполон."
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
            pulls = await ListRepositoryPulls(
                SqlAlchemyPullRequestQueries(factory, scope=scope)
            ).execute(REPO_A)
            async with factory() as session:
                report = await SqlAlchemyRunLifecycleStore(session).check_run_report(RUN_ATTEMPTED)
            assert detail is not None and pulls is not None and report is not None
            latest = next(item.latest_run for item in pulls.items if item.number == 3)
            assert latest is not None
            assert detail.verdict == latest.verdict == report.verdict == expected
            assert detail.review.summary == report.saved_summary == result["summary"]
            if not inventory_complete:
                assert detail.review.saved_result is not None
                assert detail.review.saved_result.coverage.reason_counts == {
                    "diff_inventory_incomplete": 1
                }
                assert report.coverage_status == "partial"
            view = check_run_view(report)
            if not inventory_complete:
                assert view.title == "AI-ревью: только сводка"
                assert view.conclusion == "neutral"
            else:
                assert view.title == (
                    "AI-ревью: clean" if expected else "AI-ревью: проверка неполная"
                )
            assert result["summary"]["problem"] in view.summary
            return result
        finally:
            await engine.dispose()

    result = asyncio.run(exercise())
    # Actual authenticated HTTP mapping must preserve the same nullable result.
    with api(result_env, AuthScope(42, (WS_A,))) as (client, _):
        detail = client.get(f"/api/runs/{RUN_ATTEMPTED}")
        pulls = client.get(f"/api/repos/{REPO_A}/pulls")
        assert detail.status_code == pulls.status_code == 200
        body = detail.json()
        latest = next(item["latestRun"] for item in pulls.json()["items"] if item["number"] == 3)
        assert body["verdict"] == latest["verdict"] == expected
        assert body["summary"]["problem"] == result["summary"]["problem"]
        assert body["summary"]["doneWell"] == result["summary"]["done_well"]
        if not inventory_complete:
            assert body["summary"]["problem"] == (
                "Построчное AI-ревью не выполнено: список изменённых файлов неполон."
            )
