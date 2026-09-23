from typing import cast

from sqlalchemy import CheckConstraint, Table
from sqlalchemy.dialects.postgresql import JSONB

from app.common.infrastructure.db.enums import (
    FindingCategory,
    FindingSeverity,
    FindingSide,
    RunState,
)
from app.modules.reviews.infrastructure.models import RunAction


def test_run_state_uses_completed_for_a_successful_run() -> None:
    assert RunState.COMPLETED.value == "completed"
    assert "succeeded" not in {state.value for state in RunState}


def test_finding_enums_match_the_frontend_contract() -> None:
    assert [severity.value for severity in FindingSeverity] == [
        "critical",
        "high",
        "medium",
        "low",
        "info",
    ]
    assert [category.value for category in FindingCategory] == [
        "security",
        "correctness",
        "performance",
        "readability",
    ]
    assert [side.value for side in FindingSide] == ["LEFT", "RIGHT"]


def test_run_action_supports_one_response_location() -> None:
    table = cast(Table, RunAction.__table__)
    response = table.c.response

    assert isinstance(response.type, JSONB)
    assert "ck_run_actions_response_location" in {
        constraint.name
        for constraint in table.constraints
        if isinstance(constraint, CheckConstraint)
    }
