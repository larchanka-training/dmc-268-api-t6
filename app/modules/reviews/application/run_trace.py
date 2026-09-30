"""Run step records in ``run_actions`` (docs/PIPELINE_SPEC.md §2)."""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import UUID

from app.modules.reviews.application.conventions import GeneratedConventions
from app.modules.reviews.application.execute_review import (
    ReviewPromptInput,
    ReviewPromptRepository,
)


class RunTrace(Protocol):
    """Each call is its own short transaction, outside any provider call."""

    async def record(
        self,
        run_id: UUID,
        tool: str,
        request: dict[str, Any],
        response: Any,
        started_at: datetime,
        duration_ms: int,
    ) -> None: ...


@dataclass
class TracedStep:
    response: Any = None


@asynccontextmanager
async def traced_step(
    trace: RunTrace | None,
    run_id: UUID,
    tool: str,
    request: dict[str, Any],
    *,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> AsyncIterator[TracedStep]:
    """Record the step with its duration; a failed step stores ``{error: {...}}`` and re-raises."""
    step = TracedStep()
    started_at = now()
    started = time.monotonic()
    try:
        yield step
    except BaseException as exc:
        if trace is not None:
            await trace.record(
                run_id,
                tool,
                request,
                {"error": {"type": type(exc).__name__, "message": str(exc)[:1000]}},
                started_at,
                int((time.monotonic() - started) * 1000),
            )
        raise
    if trace is not None:
        await trace.record(
            run_id,
            tool,
            request,
            step.response,
            started_at,
            int((time.monotonic() - started) * 1000),
        )


class ContextPayloadStore(Protocol):
    async def save_context_payload(self, run_id: UUID, summary: dict[str, Any]) -> None: ...


# SD §13: input token limit of the fast engine; only L1 (the diff) is built in sprint 2.
FAST_TOKEN_LIMIT = 60_000


class TracedReviewPromptRepository:
    """Record ``context.build`` and its ``ContextPayload`` summary around the context read."""

    def __init__(
        self,
        inner: ReviewPromptRepository,
        trace: RunTrace,
        payloads: ContextPayloadStore,
        *,
        engine: str,
    ) -> None:
        self._inner = inner
        self._trace = trace
        self._payloads = payloads
        self._engine = engine

    async def get_review_prompt_input(
        self, run_id: UUID, conventions: GeneratedConventions
    ) -> ReviewPromptInput | None:
        request = {"engine": self._engine, "token_limit": FAST_TOKEN_LIMIT}
        async with traced_step(self._trace, run_id, "context.build", request) as step:
            prompt_input = await self._inner.get_review_prompt_input(run_id, conventions)
            if prompt_input is None:
                return None
            summary = {
                "files": [
                    {"path": file.path, "level_used": "L1", "priority": index}
                    for index, file in enumerate(prompt_input.changed_files)
                ],
                "omitted_files": list(prompt_input.omitted_files),
                "budget": {
                    "limit": FAST_TOKEN_LIMIT,
                    # Rough estimate, four characters per token; the gateway (#33) counts exactly.
                    "used": sum(
                        len(line.content)
                        for file in prompt_input.changed_files
                        for line in file.lines
                    )
                    // 4,
                },
            }
            await self._payloads.save_context_payload(run_id, summary)
            step.response = summary
        return prompt_input
