"""The immutable publication plan is validated at its JSON boundary."""

from __future__ import annotations

from copy import deepcopy
from uuid import UUID

import pytest
from pydantic import ValidationError

from app.modules.reviews.infrastructure.publication_plan import SavedPublicationPlan

FINDING_ID = UUID("00000000-0000-0000-0000-000000000435")


def _saved_json() -> dict[str, object]:
    return {
        "head_sha": "a" * 40,
        "idempotency_key": "key-435",
        "review_event": "REQUEST_CHANGES",
        "findings_hash": "b" * 64,
        "review_body": "Review body",
        "finding_ids": [str(FINDING_ID)],
        "findings": [
            {
                "path": "src/app.py",
                "line": 7,
                "start_line": None,
                "severity": "high",
                "category": "correctness",
                "title": "Bug",
                "body": "Fix this",
                "suggestion": None,
                "confidence": 0.9,
                "rule_name": None,
                "finding_id": str(FINDING_ID),
            }
        ],
    }


def test_saved_plan_round_trips_and_types_nested_findings() -> None:
    plan = SavedPublicationPlan.from_json(_saved_json())

    assert plan.finding_ids == (FINDING_ID,)
    assert plan.findings[0].finding_id == FINDING_ID
    assert SavedPublicationPlan.from_json(plan.to_json()) == plan


@pytest.mark.parametrize(
    "damage", ["missing_body", "bad_finding", "extra_key", "unknown_id", "bad_event"]
)
def test_saved_plan_rejects_malformed_json(damage: str) -> None:
    value = deepcopy(_saved_json())
    if damage == "missing_body":
        del value["review_body"]
    elif damage == "bad_finding":
        value["findings"][0]["line"] = "not-a-line"  # type: ignore[index]
    elif damage == "extra_key":
        value["unexpected"] = True
    elif damage == "bad_event":
        value["review_event"] = "DELETE"
    else:
        value["finding_ids"] = [str(UUID(int=123))]

    with pytest.raises(ValidationError):
        SavedPublicationPlan.from_json(value)
