"""Run step records in ``run_actions`` (docs/PIPELINE_SPEC.md §2)."""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import UUID

from app.common.application.unit_of_work import UnitOfWork
from app.modules.reviews.application.policy_errors import PolicyError


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


class RunTraceStore(Protocol):
    """Repository side of the trace: adds rows and flushes, never commits."""

    async def add_action(
        self,
        run_id: UUID,
        tool: str,
        request: dict[str, Any],
        response: Any,
        started_at: datetime,
        duration_ms: int,
    ) -> None: ...

    async def save_context_payload(
        self, run_id: UUID, summary: dict[str, Any], *, mark_summary_only: bool = False
    ) -> None: ...


class RunTraceUnitOfWork(UnitOfWork, Protocol):
    @property
    def trace(self) -> RunTraceStore: ...


class TransactionalRunTrace:
    """``RunTrace`` and ``ContextPayloadStore``: each record commits in its own short UoW."""

    def __init__(self, uow_factory: Callable[[], RunTraceUnitOfWork]) -> None:
        self._uow_factory = uow_factory

    async def record(
        self,
        run_id: UUID,
        tool: str,
        request: dict[str, Any],
        response: Any,
        started_at: datetime,
        duration_ms: int,
    ) -> None:
        async with self._uow_factory() as uow:
            await uow.trace.add_action(run_id, tool, request, response, started_at, duration_ms)
            await uow.commit()

    async def save_context_payload(
        self, run_id: UUID, summary: dict[str, Any], *, mark_summary_only: bool = False
    ) -> None:
        async with self._uow_factory() as uow:
            await uow.trace.save_context_payload(
                run_id, summary, mark_summary_only=mark_summary_only
            )
            await uow.commit()


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
            error: dict[str, Any] = {"type": type(exc).__name__, "message": str(exc)[:1000]}
            if isinstance(exc, PolicyError):
                # RFS §9: the structured fields of a policy failure live in the trace only.
                error["diagnostic"] = exc.diagnostic()
            await trace.record(
                run_id,
                tool,
                request,
                {"error": error},
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
    async def save_context_payload(
        self, run_id: UUID, summary: dict[str, Any], *, mark_summary_only: bool = False
    ) -> None: ...


class ContextTrace(RunTrace, ContextPayloadStore, Protocol):
    """The trace of an attempt that also keeps its ``context_payloads`` row (the stage's)."""
