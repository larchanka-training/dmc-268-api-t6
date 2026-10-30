"""An incomplete list of only withheld files still publishes summaryOnly (#313, R2-01)."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.modules.auth.application.scope import AuthScope
from app.modules.reviews.application.check_runs import check_run_view, is_summary_only_stop
from app.modules.reviews.application.get_run_diff import DiffSnapshot, StoreDiffSnapshot
from app.modules.reviews.application.prepare_policy_snapshot import PreparePolicySnapshot
from app.modules.reviews.application.review_context import BuildReviewContext, SummaryOnlyReview
from app.modules.reviews.application.review_context_stage import (
    EstimatedTokenCounter,
    ReviewContextStage,
)
from app.modules.reviews.application.run_trace import TransactionalRunTrace
from app.modules.reviews.application.store_review_output import StoreReviewOutput
from app.modules.reviews.infrastructure.models import CodeChangeDiff, ContextPayload
from app.modules.reviews.infrastructure.policy_snapshot_unit_of_work import (
    SqlAlchemyPolicySnapshotUnitOfWork,
)
from app.modules.reviews.infrastructure.pull_request_queries import SqlAlchemyPullRequestQueries
from app.modules.reviews.infrastructure.review_output_unit_of_work import (
    SqlAlchemyReviewOutputUnitOfWork,
)
from app.modules.reviews.infrastructure.run_lifecycle_store import (
    SqlAlchemyRunLifecycleStore,
    SqlAlchemyRunTraceUnitOfWork,
)
from app.modules.reviews.infrastructure.run_processing_unit_of_work import (
    SqlAlchemyRunProcessingUnitOfWork,
)
from app.modules.reviews.infrastructure.run_repository import SqlAlchemyRunRepository
from tests.portal_postgres import HEAD, PR_NEW, REPO_A, RUN_ATTEMPTED, WS_A, Env, api
from tests.test_clean_without_text_acceptance_postgres import RevisionSource, checkpoint
from tests.test_clean_without_text_result import answer
from tests.test_clean_without_text_result_postgres import result_env as result_env
from tests.test_review_context_stage import Parser, Policy, Vcs
from tests.test_run_coverage import empty_snapshot
from tests.test_severity_threshold_postgres import RecordingQueue

_PATCH = "@@ -1 +1 @@\n-old\n+new"


def _masked() -> DiffSnapshot:
    return DiffSnapshot(
        "generated/vendor.js",
        _PATCH,
        "a" * 40,
        additions=1,
        deletions=1,
        changes=2,
    )


def test_a_builder_payload_with_reviewable_lines_is_not_a_summary_only_stop() -> None:
    stop = {
        "status": "summary_only",
        "reason": "diff_inventory_incomplete",
        "blocks": [],
    }
    builder = {
        "status": "summary_only",
        "reason": "no_reviewable_files",
        "reviewable_lines": 0,
        "blocks": [],
    }

    assert is_summary_only_stop(stop) is True
    assert is_summary_only_stop(builder) is False
    assert is_summary_only_stop({"status": "review", "reason": None}) is False
    assert is_summary_only_stop(None) is False


@pytest.mark.integration
@pytest.mark.parametrize(
    "files",
    [
        pytest.param((_masked(),), id="mask"),
        pytest.param((empty_snapshot(),), id="empty"),
        pytest.param((_masked(), empty_snapshot()), id="mask-and-empty"),
    ],
)
def test_incomplete_withheld_files_publish_summary_only(
    result_env: Env, files: tuple[DiffSnapshot, ...]
) -> None:
    async def exercise() -> None:
        engine = result_env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        scope = AuthScope(42, (WS_A,))
        try:
            async with factory.begin() as session:
                await session.execute(
                    text("UPDATE runs SET state='running', worker_id='worker' WHERE id=:run"),
                    {"run": RUN_ATTEMPTED},
                )
            async with SqlAlchemyRunProcessingUnitOfWork(factory) as processing:
                await StoreDiffSnapshot(processing.repository).execute(
                    run_id=RUN_ATTEMPTED,
                    code_change_id=PR_NEW,
                    head_sha=HEAD,
                    files=list(files),
                    excluded=lambda item: item.filename.startswith("generated/"),
                )
                await processing.commit()
            trace = TransactionalRunTrace(lambda: SqlAlchemyRunTraceUnitOfWork(factory))
            stage = ReviewContextStage(
                runs=SqlAlchemyRunRepository(factory, scope=scope),
                checkpoint=checkpoint,
                vcs=Vcs(head_sha=HEAD, files_changed=len(files) + 1),
                merge_base=Vcs(head_sha=HEAD),
                prepare_policy=PreparePolicySnapshot(
                    uow_factory=lambda: SqlAlchemyPolicySnapshotUnitOfWork(factory),
                    source_factory=lambda _pins: Policy(),
                ),
                context_builder=lambda _run: BuildReviewContext(
                    source=RevisionSource(), parser=Parser(), counter=EstimatedTokenCounter()
                ),
                trace=trace,
                payloads=trace,
            )
            with pytest.raises(SummaryOnlyReview):
                await stage.run(RUN_ATTEMPTED)
            async with factory() as session:
                rows = (
                    await session.scalars(
                        select(CodeChangeDiff).where(CodeChangeDiff.run_id == RUN_ATTEMPTED)
                    )
                ).all()
                stored = await session.scalar(
                    select(ContextPayload.summary).where(ContextPayload.run_id == RUN_ATTEMPTED)
                )
            assert rows
            assert all(row.summary_only is False for row in rows)
            assert isinstance(stored, dict)
            assert stored["status"] == "summary_only"
            assert stored["reason"] == "diff_inventory_incomplete"
            assert "reviewable_lines" not in stored
            queue = RecordingQueue()
            assert await StoreReviewOutput(
                lambda: SqlAlchemyReviewOutputUnitOfWork(factory),
                queue,
                worker_id="worker",
                run_url=lambda _: None,
            ).execute(RUN_ATTEMPTED, answer())
            async with SqlAlchemyReviewOutputUnitOfWork(factory) as uow:
                await uow.reviews.mark_review_published(RUN_ATTEMPTED)
                await uow.commit()
            pulls = await SqlAlchemyPullRequestQueries(factory, scope=scope).list_pulls(
                REPO_A, state="open", cursor=None, limit=20
            )
            async with factory() as session:
                report = await SqlAlchemyRunLifecycleStore(session).check_run_report(RUN_ATTEMPTED)
            assert pulls is not None and report is not None
            latest = next(item.latest_run for item in pulls if item.number == 3)
            assert latest is not None
            assert latest.summary_only is True
            assert report.summary_only is True
            assert report.summary_reason == "diff_inventory_incomplete"
            assert report.verdict is None
            view = check_run_view(report)
            assert view.title == "AI-ревью: только сводка"
            assert view.conclusion == "neutral"
            assert "список изменённых файлов неполон" in view.summary
        finally:
            await engine.dispose()

    asyncio.run(exercise())
    with api(result_env, AuthScope(42, (WS_A,))) as (client, _factory):
        detail = client.get(f"/api/runs/{RUN_ATTEMPTED}")
    assert detail.status_code == 200
    body = detail.json()
    assert body["status"] == "succeeded"
    assert body["summaryOnly"] is True
    assert body["verdict"] is None
