from __future__ import annotations


def render_run_label(run_id: int, state: str) -> str:
    return f"Run #{run_id}: {state}"
