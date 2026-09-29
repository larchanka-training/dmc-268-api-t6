"""Composition of the LLM gateway, and the database-free "case → ReviewOutput + usage" entry.

Worker (#34), once per process::

    gateway = build_gateway(LlmSettings.from_env(os.environ), http_client, session_factory)

and once per claimed attempt::

    run = RunCallContext(run_id, workspace_id, attempt, engine, deadline, prompt_id, rule_id)
    review_model = GatewayReviewModel(gateway, run, vcs_provider)
    conventions_model = GatewayConventionsModel(gateway, run)

Eval (#30, live mode) and manual live runs call ``review_case`` — no database, the
ledger and the trace are kept in memory and returned with the result::

    uv run python -m app.bootstrap.llm_gateway review/examples/sample.diff
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.analytics.infrastructure.usage_ledger import SqlAlchemyUsageLedger
from app.modules.reviews.application.llm import (
    EngineName,
    LlmCallRecord,
    LlmUsage,
    RunCallContext,
)
from app.modules.reviews.application.prompt_builder import (
    PullRequestMeta,
    RepoConventions,
    ReviewContext,
    ReviewRule,
    parse_unified_diff,
)
from app.modules.reviews.application.review_output import ReviewOutput, parse_review_output
from app.modules.reviews.infrastructure.llm.gateway import LlmGateway
from app.modules.reviews.infrastructure.llm.memory import (
    InMemoryLlmCallTrace,
    InMemoryUsageLedger,
)
from app.modules.reviews.infrastructure.llm.models import review_with_gateway
from app.modules.reviews.infrastructure.llm.settings import LlmSettings
from app.modules.reviews.infrastructure.llm.transport import (
    ChatTransport,
    OpenAICompatibleTransport,
)
from app.modules.reviews.infrastructure.llm_call_trace import SqlAlchemyLlmCallTrace

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SYSTEM_PROMPT = REPO_ROOT / "review" / "prompts" / "review.system.v2.md"
_EVAL_WORKSPACE = UUID(int=0)


def build_gateway(
    settings: LlmSettings,
    client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> LlmGateway:
    """The production gateway: HTTP transport, ``usage_events`` and ``llm.call`` in PG."""
    return LlmGateway(
        settings,
        OpenAICompatibleTransport(client),
        SqlAlchemyUsageLedger(session_factory),
        SqlAlchemyLlmCallTrace(session_factory),
    )


@dataclass(frozen=True)
class ReviewCase:
    """One review input as eval cases describe it: a unified diff plus its context."""

    diff: str
    system: str
    pr_meta: PullRequestMeta
    rules: tuple[ReviewRule, ...] = ()
    agents_md: str | None = None
    conventions: RepoConventions = field(default_factory=lambda: RepoConventions((), ()))
    engine: EngineName = "fast"


@dataclass(frozen=True)
class ReviewCaseResult:
    output: ReviewOutput
    provider: str
    model: str
    usage: tuple[LlmUsage, ...]
    calls: tuple[LlmCallRecord, ...]
    latency_ms: int


async def review_case(
    case: ReviewCase,
    settings: LlmSettings,
    *,
    transport: ChatTransport | None = None,
) -> ReviewCaseResult:
    """Run one case through the real gateway policy without a database.

    Raises ``LlmCallFailed`` with the normalized ``error_code`` like the worker path.
    """
    ledger = InMemoryUsageLedger()
    trace = InMemoryLlmCallTrace()
    run = RunCallContext(
        run_id=uuid4(),
        workspace_id=_EVAL_WORKSPACE,
        attempt=1,
        engine=case.engine,
        deadline=datetime.now(UTC) + timedelta(minutes=8),
    )
    context = ReviewContext(
        system=case.system,
        rules=case.rules,
        agents_md=case.agents_md,
        conventions=case.conventions,
        pr_meta=case.pr_meta,
        changed_files=parse_unified_diff(case.diff),
        omitted_files=(),
    )
    started = time.monotonic()
    async with _ClientScope(transport) as effective:
        gateway = LlmGateway(settings, effective, ledger, trace)
        result = await review_with_gateway(gateway, context, run)
    return ReviewCaseResult(
        output=parse_review_output(result.output),
        provider=result.provider,
        model=result.model,
        usage=result.usage,
        calls=tuple(record for _, record in trace.records),
        latency_ms=int((time.monotonic() - started) * 1000),
    )


class _ClientScope:
    """Own an HTTP client only when the caller did not inject a transport."""

    def __init__(self, transport: ChatTransport | None) -> None:
        self._transport = transport
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> ChatTransport:
        if self._transport is not None:
            return self._transport
        self._client = httpx.AsyncClient()
        return OpenAICompatibleTransport(self._client)

    async def __aexit__(self, *_: object) -> None:
        if self._client is not None:
            await self._client.aclose()


def main(argv: list[str] | None = None) -> int:
    """Manual live run (needs LLM_* env): prints provider, model, tokens, cost, time."""
    parser = argparse.ArgumentParser(description="Review one unified diff through the gateway.")
    parser.add_argument("diff", type=Path)
    parser.add_argument("--system", type=Path, default=DEFAULT_SYSTEM_PROMPT)
    parser.add_argument("--engine", choices=("fast", "deep"), default="fast")
    parser.add_argument("--title", default="Live gateway check")
    args = parser.parse_args(argv)
    diff = args.diff.read_text(encoding="utf-8")
    files = parse_unified_diff(diff)
    case = ReviewCase(
        diff=diff,
        system=args.system.read_text(encoding="utf-8"),
        pr_meta=PullRequestMeta(
            title=args.title,
            description=None,
            author="eval",
            source_branch="eval",
            target_branch="main",
            labels=(),
            files_changed=len(files),
            lines_added=sum(line.type == "added" for f in files for line in f.lines),
            lines_removed=sum(line.type == "removed" for f in files for line in f.lines),
            is_draft=False,
            is_fork=False,
        ),
        engine=args.engine,
    )
    result = asyncio.run(review_case(case, LlmSettings.from_env(os.environ)))
    json.dump(
        {
            "provider": result.provider,
            "model": result.model,
            "calls": [
                {"kind": item.kind.value, "model": item.model, "duration_ms": item.duration_ms}
                for item in result.calls
            ],
            "tokens_in": sum(item.tokens_in for item in result.usage),
            "tokens_out": sum(item.tokens_out for item in result.usage),
            "cache_read_tokens": sum(item.cache_read_tokens for item in result.usage),
            "cost_usd": str(sum((item.cost_usd for item in result.usage), Decimal(0))),
            "latency_ms": result.latency_ms,
            "findings": len(result.output.findings),
            "output": result.output.model_dump(mode="json"),
        },
        sys.stdout,
        indent=2,
    )
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
