"""Fakes and fixed inputs shared by the eval-live recorder and replay tests."""

from __future__ import annotations

import asyncio
import json
import shutil
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

import review.scripts.eval_live as eval_live_module
from app.modules.reviews.application.check_runs import RunProviderCheckpoint
from app.modules.reviews.infrastructure.llm.settings import (
    GatewayPolicy,
    LlmSettings,
    ModelPrice,
    ModelProfile,
)
from app.modules.reviews.infrastructure.llm.transport import (
    ChatRequest,
    ChatResponse,
    TransportError,
)
from review.scripts.eval_live import record_live
from review.scripts.eval_provenance import rule_json_paths, static_input_paths

REPO_ROOT = Path(__file__).resolve().parents[1]
SYSTEM = REPO_ROOT / "review/prompts/review.system.v2.md"
BACKEND_RULES = REPO_ROOT / "review/rules/default-backend.v1.json"
FRONTEND_RULES = REPO_ROOT / "review/rules/default-frontend.v1.json"
RECORDED_AT = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)
VALID = json.dumps(
    {
        "findings": [],
        "summary": {
            "problem": "No issue was found.",
            "done_well": "The change is clear.",
            "effort": "none",
        },
    }
)


RULES_ROOT = REPO_ROOT / "test-prs-dataset" / "rules"


def settings() -> LlmSettings:
    price = ModelPrice(Decimal(0), Decimal(0))
    primary = ModelProfile(
        "fake",
        "http://localhost:8888",
        "primary-model",
        100_000,
        1_000,
        price,
        api_keys=("SENSITIVE_TEST_KEY",),
    )
    fallback = ModelProfile(
        "fake",
        "http://localhost:8888",
        "fallback-model",
        100_000,
        1_000,
        price,
        api_keys=("SENSITIVE_TEST_KEY",),
    )
    return LlmSettings(
        primary,
        fallback,
        GatewayPolicy(
            timeout_retry_delays_s=(),
            unavailable_retry_delays_s=(),
            max_jitter_s=0,
        ),
    )


def answer(
    model: str, content: str | list[dict[str, str]] | None, *, provider: str | None = None
) -> ChatResponse:
    raw: dict[str, Any] = {
        "model": model,
        "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 30},
    }
    if provider is not None:
        raw["provider"] = provider
    text = (
        "".join(part.get("text", "") for part in content if part.get("type") == "text")
        if isinstance(content, list)
        else content
    )
    return ChatResponse(raw, text, "stop", model, 100, 30, 0, None, None)


@dataclass
class FakeTransport:
    outcomes: list[ChatResponse | TransportError]
    requests: list[ChatRequest] = field(default_factory=list)

    async def complete(
        self, request: ChatRequest, *, checkpoint: RunProviderCheckpoint
    ) -> ChatResponse:
        await checkpoint()
        self.requests.append(request)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, TransportError):
            raise outcome
        return outcome


def rules_fixture_root(tmp_path: Path, case_ids: tuple[str, ...]) -> Path:
    root = tmp_path / "rules-dataset"
    for case_id in case_ids:
        shutil.copytree(RULES_ROOT / "cases" / case_id, root / "cases" / case_id)
    return root


def record_arm(
    root: Path,
    transport: FakeTransport,
    arm: str,
    *,
    responses_dir: Path | None = None,
    model_settings: LlmSettings | None = None,
    system_prompt: Path = SYSTEM,
) -> dict[str, Any]:
    return asyncio.run(
        record_live(
            root,
            model_settings or settings(),
            system_prompt=system_prompt,
            backend_rules=BACKEND_RULES,
            frontend_rules=FRONTEND_RULES,
            transport=transport,
            recorded_at=RECORDED_AT,
            policy_arm=arm,
            responses_dir=responses_dir,
        )
    )


DATASET_ROOT = REPO_ROOT / "test-prs-dataset"
INVALID = '{"findings":[]}'


# The whole diagnostic, not its prefix: the second half tells the user what to do.
RESPONSES_NOT_EMPTY = "responses directory is not empty; preserve the existing baseline"


def fixture_root(tmp_path: Path, case_ids: tuple[str, ...]) -> Path:
    root = tmp_path / "dataset"
    for case_id in case_ids:
        shutil.copytree(DATASET_ROOT / "cases" / case_id, root / "cases" / case_id)
    return root


def record(
    root: Path, transport: FakeTransport, *, model_settings: LlmSettings | None = None
) -> dict[str, Any]:
    return asyncio.run(
        record_live(
            root,
            model_settings or settings(),
            system_prompt=SYSTEM,
            backend_rules=BACKEND_RULES,
            frontend_rules=FRONTEND_RULES,
            transport=transport,
            recorded_at=RECORDED_AT,
        )
    )


def response(root: Path, case_id: str) -> bytes:
    return (root / "responses" / f"{case_id}.json").read_bytes()


def commit_context_repo(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    payload: object,
) -> Path:
    repo = tmp_path / "repo"
    for relative in static_input_paths(
        "review/prompts/review.system.v2.md", rule_json_paths(REPO_ROOT)
    ):
        destination = repo / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO_ROOT / relative, destination)
    sidecar = repo / "review/eval/pr-commit-messages.json"
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    sidecar.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(eval_live_module, "REPO_ROOT", repo)
    return repo


def record_with_repo(root: Path, repo: Path, transport: FakeTransport) -> dict[str, Any]:
    return asyncio.run(
        record_live(
            root,
            settings(),
            system_prompt=repo / "review/prompts/review.system.v2.md",
            backend_rules=repo / "review/rules/default-backend.v1.json",
            frontend_rules=repo / "review/rules/default-frontend.v1.json",
            transport=transport,
            recorded_at=RECORDED_AT,
        )
    )
