"""A masked file and a lock stay excluded after a real summary-only publication (#313).

The stage uses the worker's built-in exclusions and ``TransactionalRunTrace``. Coverage
is the reload inside ``save_summary_only_stop``, then the publication input and the
stored review result. Legacy ``review.system`` v2 and snapshot-path v5 both count the
lock as ``ignored``: the worker withholds it before the generated-omission exclusion.
A proven empty file stays unmarked, so the stop does not cancel that proof.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from uuid import UUID

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.modules.auth.application.scope import AuthScope
from app.modules.reviews.application.get_run_diff import DiffSnapshot, StoreDiffSnapshot
from app.modules.reviews.application.prepare_policy_snapshot import PreparePolicySnapshot
from app.modules.reviews.application.review_context import BuildReviewContext, SummaryOnlyReview
from app.modules.reviews.application.review_context_stage import (
    EstimatedTokenCounter,
    ReviewContextStage,
)
from app.modules.reviews.application.run_trace import TransactionalRunTrace
from app.modules.reviews.application.store_review_output import StoreReviewOutput
from app.modules.reviews.application.vcs_diff import GIT_EMPTY_BLOB_SHA, is_generated_path
from app.modules.reviews.infrastructure.models import CodeChangeDiff, RunAction
from app.modules.reviews.infrastructure.policy_snapshot_unit_of_work import (
    SqlAlchemyPolicySnapshotUnitOfWork,
)
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
from tests.clean_without_text_fakes import answer
from tests.policy_snapshot_fakes import make_complete_execution_profile
from tests.portal_postgres import PR_NEW, WS_A, Env, api, portal_schema
from tests.test_clean_without_text_acceptance_postgres import RevisionSource, checkpoint
from tests.test_review_context_stage import Parser, Policy, Vcs
from tests.test_run_prompt_pins_postgres import _packaged, _seed
from tests.test_run_publication_settings_postgres import _insert, _ready
from tests.test_severity_threshold_postgres import RecordingQueue

_LINE = "@@ -1 +1 @@\n-old\n+new"
# The worker withholds the lock, so both paths publish it as ignored, with the mask.
# The proven empty file is an exclusion and is not a summary-only row.
_EXCLUSIONS = {"ignored": 2, "empty_file": 1}


def _code(lines: int) -> DiffSnapshot:
    if lines == 1:
        return DiffSnapshot("src/app.py", _LINE, "d" * 40, "modified", None, 1, 1, 2)
    body = "".join(f"+x{number}\n" for number in range(lines))
    return DiffSnapshot(
        "src/app.py",
        f"@@ -0,0 +1,{lines} @@\n{body}",
        "d" * 40,
        "added",
        None,
        lines,
        0,
        lines,
    )


def _mask() -> DiffSnapshot:
    return DiffSnapshot("generated/notes.py", _LINE, "a" * 40, "modified", None, 1, 1, 2)


def _lock() -> DiffSnapshot:
    return DiffSnapshot(
        "package-lock.json",
        _LINE,
        "b" * 40,
        "modified",
        None,
        1,
        1,
        2,
        omission_reason="generated",
    )


def _missing() -> DiffSnapshot:
    return DiffSnapshot(
        "src/note.txt",
        None,
        "c" * 40,
        "modified",
        None,
        3,
        0,
        3,
        omission_reason="missing_patch",
    )


def _empty() -> DiffSnapshot:
    return DiffSnapshot(
        "pkg/__init__.py",
        None,
        GIT_EMPTY_BLOB_SHA,
        "added",
        omission_reason="empty_file",
    )


_CASES = (
    ("diff_too_large", (_code(3_001), _mask(), _lock(), _empty()), 4, {"summary_only": 1}),
    (
        "diff_inventory_incomplete",
        (_code(1), _mask(), _lock(), _empty()),
        5,
        {"diff_inventory_incomplete": 1, "summary_only": 1},
    ),
    (
        "no_reviewable_files",
        (_missing(), _mask(), _lock(), _empty()),
        4,
        {"missing_patch": 1},
    ),
)


@pytest.fixture
def env() -> Iterator[Env]:
    url = os.environ.get("TEST_DATABASE_URL")
    if url is None:
        pytest.skip("set TEST_DATABASE_URL to test summary-only publication")
    with portal_schema(url, None) as schema:
        yield schema


@pytest.mark.integration
@pytest.mark.parametrize("version", [2, 5])
def test_masked_file_and_lock_keep_published_coverage(env: Env, version: int) -> None:
    async def exercise() -> list[UUID]:
        engine = env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        scope = AuthScope(42, (WS_A,))
        published: list[UUID] = []
        try:
            await _ready(factory)
            await _seed(factory, _packaged())
            for index, (reason, files, files_changed, reasons) in enumerate(_CASES):
                head = f"{index + 1:040x}"
                async with factory.begin() as session:
                    await session.execute(
                        text("UPDATE code_changes SET head_sha=:head WHERE id=:pr"),
                        {"head": head, "pr": PR_NEW},
                    )
                    run_id = await _insert(session)
                    await session.execute(
                        text(
                            "UPDATE runs SET state='running', worker_id='worker', attempt=1, "
                            "prompt_version_id=(SELECT id FROM prompt_versions "
                            "WHERE key='review.system' AND version=:version) WHERE id=:id"
                        ),
                        {"version": version, "id": run_id},
                    )
                async with SqlAlchemyRunProcessingUnitOfWork(factory) as processing:
                    await StoreDiffSnapshot(processing.repository).execute(
                        run_id=run_id,
                        code_change_id=PR_NEW,
                        head_sha=head,
                        files=list(files),
                        excluded=lambda item: (
                            item.filename.startswith("generated/")
                            or is_generated_path(item.filename)
                        ),
                    )
                    await processing.commit()
                trace = TransactionalRunTrace(lambda: SqlAlchemyRunTraceUnitOfWork(factory))
                stage = ReviewContextStage(
                    runs=SqlAlchemyRunRepository(factory, scope=scope),
                    checkpoint=checkpoint,
                    vcs=Vcs(head_sha=head, files_changed=files_changed),
                    merge_base=Vcs(head_sha=head),
                    prepare_policy=PreparePolicySnapshot(
                        uow_factory=lambda: SqlAlchemyPolicySnapshotUnitOfWork(
                            factory, execution_profile=make_complete_execution_profile()
                        ),
                        source_factory=lambda _pins: Policy(),
                    ),
                    context_builder=lambda _run: BuildReviewContext(
                        source=RevisionSource(), parser=Parser(), counter=EstimatedTokenCounter()
                    ),
                    trace=trace,
                    payloads=trace,
                    builtin_excluded=is_generated_path,
                )
                with pytest.raises(SummaryOnlyReview):
                    await stage.run(run_id)
                async with factory() as session:
                    rows = (
                        await session.scalars(
                            select(CodeChangeDiff).where(CodeChangeDiff.run_id == run_id)
                        )
                    ).all()
                    build = await session.scalar(
                        select(RunAction.response).where(
                            RunAction.run_id == run_id, RunAction.tool == "context.build"
                        )
                    )
                checked = "src/note.txt" if reason == "no_reviewable_files" else "src/app.py"
                flags = {row.filename: (row.summary_only, row.review_patch is None) for row in rows}
                assert flags == {
                    checked: (True, True),
                    "package-lock.json": (True, True),
                    "generated/notes.py": (False, True),
                    "pkg/__init__.py": (False, True),
                }
                assert isinstance(build, dict)
                assert build["status"] == "summary_only"
                assert build["reason"] == reason
                assert build["coverage"]["status"] == "partial"
                assert build["coverage"]["reviewed_files"] == 0
                assert build["coverage"]["reason_counts"] == reasons
                assert build["coverage"]["exclusion_counts"] == _EXCLUSIONS
                assert "classification_unavailable" not in build["coverage"]["reason_counts"]
                async with SqlAlchemyReviewOutputUnitOfWork(factory) as uow:
                    post_input = await uow.reviews.get_post_processing_input(run_id)
                assert post_input is not None and post_input.coverage is not None
                assert post_input.coverage.reason_counts == reasons
                assert post_input.coverage.exclusion_counts == _EXCLUSIONS
                assert await StoreReviewOutput(
                    lambda: SqlAlchemyReviewOutputUnitOfWork(factory),
                    RecordingQueue(),
                    worker_id="worker",
                    expected_attempt=1,
                    run_url=lambda _: None,
                ).execute(run_id, answer())
                async with factory() as session:
                    saved = await session.scalar(
                        select(RunAction.response).where(
                            RunAction.run_id == run_id, RunAction.tool == "review.finalize"
                        )
                    )
                assert isinstance(saved, dict)
                assert saved["coverage"]["reason_counts"] == reasons
                assert saved["coverage"]["exclusion_counts"] == _EXCLUSIONS
                async with SqlAlchemyReviewOutputUnitOfWork(factory) as uow:
                    await uow.reviews.mark_review_published(run_id)
                    await uow.commit()
                async with factory() as session:
                    report = await SqlAlchemyRunLifecycleStore(session).check_run_report(run_id)
                assert report is not None
                assert report.summary_only is True
                assert report.summary_reason == reason
                assert report.coverage_status == "partial"
                published.append(run_id)
        finally:
            await engine.dispose()
        return published

    published = asyncio.run(exercise())
    with api(env, AuthScope(42, (WS_A,))) as (client, _factory):
        for run_id in published:
            detail = client.get(f"/api/runs/{run_id}")
            assert detail.status_code == 200
            body = detail.json()
            assert body["status"] == "succeeded"
            assert body["summaryOnly"] is True
            assert body["verdict"] is None
