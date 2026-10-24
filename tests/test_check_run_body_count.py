"""Check-run ``body_count`` aligns with stored review body (D-3, #170)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

from app.common.infrastructure.db.enums import RunState
from app.modules.reviews.application.findings_post_processor import (
    body_only_accounted_in_review_body,
)
from app.modules.reviews.infrastructure.run_lifecycle_store import SqlAlchemyRunLifecycleStore


def test_check_run_report_body_count_uses_accounted_review_body() -> None:
    review_body = "\n".join(
        (
            "## Review summary",
            "",
            "**Verdict:** attention",
            "",
            "## Additional findings",
            "",
            "- **Listed**",
            "",
            "**Ещё 2 находок**",
        )
    )
    expected = body_only_accounted_in_review_body(review_body)
    run_id = uuid4()
    run = SimpleNamespace(
        id=run_id,
        state=RunState.SUCCEEDED,
        attempt=1,
        head_sha="a" * 40,
        error_code=None,
        error_message=None,
        review_body=review_body,
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
    store._published_findings = AsyncMock(return_value=())  # type: ignore[method-assign]

    report = asyncio.run(store.check_run_report(run_id))

    assert report is not None
    assert report.body_count == expected == 3
