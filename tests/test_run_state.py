from app.common.infrastructure.db.enums import FindingCategory, FindingSeverity, RunState


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
