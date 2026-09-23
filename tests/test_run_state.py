from app.common.infrastructure.db.enums import RunState


def test_run_state_uses_completed_for_a_successful_run() -> None:
    assert RunState.COMPLETED.value == "completed"
    assert "succeeded" not in {state.value for state in RunState}
