"""SQL form of ``is_summary_only_stop`` for a ``context_payloads.summary`` document."""

from __future__ import annotations

from typing import Any

from sqlalchemy import ColumnElement, not_


def summary_only_stop_conditions(summary: Any) -> tuple[ColumnElement[bool], ColumnElement[bool]]:
    """Match the stage stop: ``status == summary_only`` and no ``reviewable_lines``.

    The same rule as ``app.modules.reviews.application.check_runs.is_summary_only_stop``.
    A builder payload that continues to the model always has ``reviewable_lines``.
    """
    return (
        summary["status"].astext == "summary_only",
        not_(summary.has_key("reviewable_lines")),
    )
