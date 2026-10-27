"""Every production composition of the context stage keeps the run's payload row (#168 part 6).

The stage writes ``context_payloads`` only through its ``payloads`` store: a composition that
builds a ``ReviewContextStage`` without it records ``context.build`` and writes no row.
"""

from __future__ import annotations

import ast
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_APP = _ROOT / "app"


def _stage_calls(path: Path) -> list[ast.Call]:
    calls: list[ast.Call] = []
    for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
        if name == "ReviewContextStage":
            calls.append(node)
    return calls


def _passes_a_store(keyword: ast.keyword) -> bool:
    # ``payloads=None`` is the default spelt out: no row either.
    value = keyword.value
    return keyword.arg == "payloads" and not (
        isinstance(value, ast.Constant) and value.value is None
    )


def test_every_review_context_stage_in_app_passes_its_payload_store() -> None:
    found: dict[str, list[bool]] = {}
    for path in sorted(_APP.rglob("*.py")):
        for call in _stage_calls(path):
            found.setdefault(path.relative_to(_ROOT).as_posix(), []).append(
                any(_passes_a_store(keyword) for keyword in call.keywords)
            )

    # Not vacuous: the GitHub factory and the GitLab bundle are found.
    assert set(found) >= {"app/worker.py", "app/bootstrap/gitlab_reviews.py"}
    missing = sorted(path for path, calls in found.items() if not all(calls))
    assert missing == []
