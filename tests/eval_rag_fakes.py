"""RAG eval fixtures shared by recorder, replay, and aggregate tests."""

from __future__ import annotations

import asyncio
import json
import shutil
from dataclasses import replace
from pathlib import Path
from typing import Any

import review.scripts.eval_live as eval_live_module
from app.modules.reviews.application.llm import EngineName
from app.modules.reviews.application.prompt_budget import (
    fit_review_context,
    prompt_tokens,
    token_counter_for,
)
from app.modules.reviews.infrastructure.llm.settings import LlmSettings
from review.scripts.eval_rag_context import build_arm_input
from review.scripts.rag_snapshot import INDEX_FILE, build_case_snapshot, render_snapshot
from tests.eval_live_fakes import (
    BACKEND_RULES,
    FRONTEND_RULES,
    RECORDED_AT,
    REPO_ROOT,
    VALID,
    FakeTransport,
    answer,
    settings,
)
from tests.rag_case_fakes import _rag_metadata

SYSTEM_V5 = REPO_ROOT / "review" / "prompts" / "review.system.v5.md"
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "rag_eval"
CASES = {"RAG-01": ("spike", "reverse_edge"), "RAG-02": ("clean", "clean")}


def rag_root(tmp_path: Path, cases: dict[str, tuple[str, str]] | None = None) -> Path:
    """A dataset root of RAG cases: copies of fixture cases, their indexes rebuilt."""
    root = tmp_path / "rag"
    for case_id, (fixture, kind) in (cases or CASES).items():
        case_dir = root / "cases" / case_id
        shutil.copytree(FIXTURES / fixture, case_dir)
        (case_dir / "case.json").write_text(
            json.dumps(_rag_metadata(kind, case_id)), encoding="utf-8"
        )
        # The indexed commit id derives from the directory name.
        (case_dir / INDEX_FILE).write_bytes(render_snapshot(build_case_snapshot(case_dir)))
    return root


class Clock:
    """A monotonic clock that advances 0.25 s per reading."""

    def __init__(self) -> None:
        self._now = 0.0

    def __call__(self) -> float:
        self._now += 0.25
        return self._now


def record_rag(
    root: Path,
    transport: FakeTransport,
    rag_arm: str | None,
    *,
    model_settings: LlmSettings | None = None,
    policy_arm: str = "none",
    system_prompt: Path = SYSTEM_V5,
    engine: EngineName = "fast",
    responses_dir: Path | None = None,
) -> dict[str, Any]:
    return asyncio.run(
        eval_live_module.record_live(
            root,
            model_settings or settings(),
            system_prompt=system_prompt,
            backend_rules=BACKEND_RULES,
            frontend_rules=FRONTEND_RULES,
            transport=transport,
            recorded_at=RECORDED_AT,
            engine=engine,
            policy_arm=policy_arm,
            rag_arm=rag_arm,
            responses_dir=responses_dir,
            clock=Clock(),
        )
    )


def valid_answers(count: int = len(CASES)) -> FakeTransport:
    return FakeTransport([answer("primary-model", VALID) for _ in range(count)])


def window_without_rag(root: Path, case_id: str) -> LlmSettings:
    """A model window the gateway fits arm B of ``case_id`` into only by dropping the RAG slot.

    Found with the production fitting itself: the largest budget at which no retrieved block
    is left (L3 and L2 go first, so a window just under B's prompt would keep the RAG slot).
    """
    configured = settings()
    context = asyncio.run(
        build_arm_input(root / "cases" / case_id, "B", review_model="primary-model", version="v5")
    ).context
    if not context.retrieved:
        raise AssertionError("the RAG case has no retrieved context")
    counter = token_counter_for("counter/v2")
    for budget in range(prompt_tokens(context, counter), 0, -1):
        fitted = fit_review_context(context, max_prompt_tokens=budget, counter=counter)
        if not fitted.retrieved:
            break
    else:
        raise AssertionError("the RAG slot is never dropped")
    # Budget = window - max_output; the gateway fits the prompt to it.
    return LlmSettings(
        replace(
            configured.primary,
            context_window=budget + configured.primary.max_output_tokens,
        ),
        None,
    )
