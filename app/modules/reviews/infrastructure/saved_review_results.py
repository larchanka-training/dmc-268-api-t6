"""Batch finalized result reads, with bounded metadata-only legacy diagnostics."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import cast as type_cast
from uuid import UUID

from sqlalchemy import String, and_, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.reviews.application.get_run_diff import DiffSnapshot
from app.modules.reviews.application.prompt_builder import FileStatus
from app.modules.reviews.application.saved_review_result import (
    SavedReviewResult,
    fallback_review_result,
    read_saved_review_result,
)
from app.modules.reviews.infrastructure.models import (
    CodeChangeDiff,
    RunAction,
    RunActionResponseBody,
)


async def load_review_results(
    session: AsyncSession,
    heads: Mapping[UUID, str],
    severities: Mapping[UUID, Sequence[str]],
) -> dict[UUID, SavedReviewResult]:
    if not heads:
        return {}
    rows = (
        await session.execute(
            select(RunAction.run_id, RunAction.response, RunActionResponseBody.body)
            .outerjoin(
                RunActionResponseBody,
                and_(
                    RunActionResponseBody.run_id == RunAction.run_id,
                    cast(RunActionResponseBody.id, String) == RunAction.response_ref,
                ),
            )
            .where(RunAction.run_id.in_(heads), RunAction.tool == "review.postprocess")
            .order_by(RunAction.run_id, RunAction.index.desc())
            .distinct(RunAction.run_id)
        )
    ).all()
    results = {}
    for run_id, inline, body in rows:
        result = read_saved_review_result(
            body if body is not None else inline,
            head_sha=heads[run_id],
            run_id=run_id,
            severities=severities.get(run_id, ()),
        )
        if result is not None:
            results[run_id] = result
    missing = [run_id for run_id in heads if run_id not in results]
    if not missing:
        return results
    # Neither a legacy list page nor a check-run needs entire patch bodies just to
    # establish unknown coverage. Window counts retain totals beyond the 20 diagnostics.
    metadata = (
        select(
            CodeChangeDiff.run_id,
            CodeChangeDiff.filename,
            CodeChangeDiff.status,
            CodeChangeDiff.previous_filename,
            CodeChangeDiff.omission_reason,
            CodeChangeDiff.summary_only,
            func.count().over(partition_by=CodeChangeDiff.run_id).label("total"),
            func.row_number()
            .over(partition_by=CodeChangeDiff.run_id, order_by=CodeChangeDiff.filename)
            .label("position"),
        )
        .where(CodeChangeDiff.run_id.in_(missing))
        .subquery()
    )
    snapshots: dict[UUID, list[DiffSnapshot]] = {}
    totals: dict[UUID, int] = {}
    for (
        run_id,
        filename,
        status,
        previous,
        omission,
        summary_only,
        total,
        _,
    ) in await session.execute(
        select(metadata)
        .where(metadata.c.position <= 20)
        .order_by(metadata.c.run_id, metadata.c.position)
    ):
        snapshots.setdefault(run_id, []).append(
            DiffSnapshot(
                filename,
                None,
                status=type_cast(FileStatus, status),
                previous_filename=previous,
                omission_reason=omission,
                summary_only=summary_only,
            )
        )
        totals[run_id] = total
    for run_id in missing:
        results[run_id] = fallback_review_result(
            heads[run_id],
            severities.get(run_id, ()),
            snapshots.get(run_id, []),
            total_files=totals.get(run_id, 0),
        )
    return results
