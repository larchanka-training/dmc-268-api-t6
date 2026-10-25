"""Check-run ``body_count`` counts body-only findings, not markdown parsing (D-3, #170)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

from app.common.infrastructure.db.enums import FindingSeverity, RunState
from app.modules.reviews.infrastructure.run_lifecycle_store import SqlAlchemyRunLifecycleStore


def _finding(*, inline: bool) -> SimpleNamespace:
    return SimpleNamespace(inline_comment=inline, severity=FindingSeverity.MEDIUM)


def test_check_run_report_body_count_is_non_inline_findings() -> None:
    run_id = uuid4()
    run = SimpleNamespace(
        id=run_id,
        state=RunState.SUCCEEDED,
        attempt=1,
        head_sha="a" * 40,
        error_code=None,
        error_message=None,
        review_body="legacy body without ## Additional findings section",
    )
    repository = SimpleNamespace(full_name="org/repo")
    installation = SimpleNamespace(external_id=99)
    session = MagicMock()
    session.execute = AsyncMock(
        return_value=MagicMock(
            one_or_none=MagicMock(return_value=(run, None, repository, installation))
        )
    )
    session.scalar = AsyncMock(side_effect=[False, 0])
    store = SqlAlchemyRunLifecycleStore(session)
    store._published_findings = AsyncMock(  # type: ignore[method-assign]
        return_value=(
            _finding(inline=True),
            _finding(inline=False),
            _finding(inline=False),
        )
    )

    report = asyncio.run(store.check_run_report(run_id))

    assert report is not None
    assert report.inline_count == 1
    assert report.body_count == 2


def test_check_run_report_body_count_includes_findings_moved_from_inline() -> None:
    """After coordinate fallback, inline_comment is false but findings remain published."""
    run_id = uuid4()
    run = SimpleNamespace(
        id=run_id,
        state=RunState.SUCCEEDED,
        attempt=1,
        head_sha="a" * 40,
        error_code=None,
        error_message=None,
        review_body="## Review summary\n\n**Verdict:** attention\n\n(no Additional findings)",
    )
    repository = SimpleNamespace(full_name="org/repo")
    installation = SimpleNamespace(external_id=99)
    session = MagicMock()
    session.execute = AsyncMock(
        return_value=MagicMock(
            one_or_none=MagicMock(return_value=(run, None, repository, installation))
        )
    )
    session.scalar = AsyncMock(side_effect=[False, 0])
    store = SqlAlchemyRunLifecycleStore(session)
    store._published_findings = AsyncMock(  # type: ignore[method-assign]
        return_value=tuple(_finding(inline=False) for _ in range(3))
    )

    report = asyncio.run(store.check_run_report(run_id))

    assert report is not None
    assert report.inline_count == 0
    assert report.body_count == 3
