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
from collections.abc import Awaitable, Callable, Mapping
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from functools import partial
from pathlib import Path
from uuid import UUID, uuid4

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.analytics.infrastructure.usage_ledger import SqlAlchemyUsageLedger
from app.modules.reviews.application.conventions import ConventionsDraft, ConventionsRequest
from app.modules.reviews.application.llm import (
    EngineName,
    LlmCallFailed,
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
from app.modules.reviews.application.run_failures import FAST_ATTEMPT_DEADLINE
from app.modules.reviews.application.run_trace import TransactionalRunTrace
from app.modules.reviews.infrastructure.llm.answers import review_output_schema
from app.modules.reviews.infrastructure.llm.ecb_fx import (
    EcbFxQuoteCache,
    EcbFxRateAdapter,
    FxQuoteProvider,
)
from app.modules.reviews.infrastructure.llm.gateway import LlmGateway
from app.modules.reviews.infrastructure.llm.memory import (
    InMemoryLlmCallTrace,
    InMemoryUsageLedger,
)
from app.modules.reviews.infrastructure.llm.models import (
    GatewayConventionsModel,
    review_with_gateway,
)
from app.modules.reviews.infrastructure.llm.settings import LlmConfigError, LlmSettings
from app.modules.reviews.infrastructure.llm.transport import (
    ChatTransport,
    OpenAICompatibleTransport,
)
from app.modules.reviews.infrastructure.llm_call_trace import RunTraceLlmCalls
from app.modules.reviews.infrastructure.run_lifecycle_store import SqlAlchemyRunTraceUnitOfWork

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SYSTEM_PROMPT = REPO_ROOT / "review" / "prompts" / "review.system.v2.md"
DEFAULT_CONVENTIONS_PROMPT = REPO_ROOT / "review" / "prompts" / "review.conventions.v2.md"
_EVAL_WORKSPACE = UUID(int=0)


def build_gateway(
    settings: LlmSettings,
    client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    fx_provider: FxQuoteProvider | None = None,
) -> LlmGateway:
    """The production gateway: HTTP transport, ``usage_events`` and ``llm.call`` in PG.

    The ReviewOutput schema is read here, so a missing file fails the process start,
    not the first review.
    """
    review_output_schema()
    return LlmGateway(
        settings,
        OpenAICompatibleTransport(client),
        SqlAlchemyUsageLedger(session_factory),
        RunTraceLlmCalls(
            TransactionalRunTrace(partial(SqlAlchemyRunTraceUnitOfWork, session_factory))
        ),
        fx_provider=fx_provider,
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


# Attempt deadlines of docs/PIPELINE_SPEC.md §3: fast 8 min, deep (SandboxEngine) 10 min.
SANDBOX_ENGINE_DEADLINE = timedelta(minutes=10)
CASE_DEADLINE: dict[str, timedelta] = {
    "fast": FAST_ATTEMPT_DEADLINE,
    "deep": SANDBOX_ENGINE_DEADLINE,
}


class ReviewCaseFailed(LlmCallFailed):
    """``LlmCallFailed`` of a case, with the in-memory trace and usage it produced."""

    def __init__(self, failure: LlmCallFailed, calls: tuple[LlmCallRecord, ...]) -> None:
        super().__init__(
            failure.error_code,
            str(failure).removeprefix(f"{failure.error_code.value}: "),
            usage=failure.usage,
            calls=failure.calls,
        )
        self.trace = calls


@dataclass(frozen=True)
class ReviewCaseResult:
    output: ReviewOutput
    provider: str
    model: str
    usage: tuple[LlmUsage, ...]
    calls: tuple[LlmCallRecord, ...]
    latency_ms: int


async def _run_database_free_case[CaseOutput](
    settings: LlmSettings,
    engine: EngineName,
    invoke: Callable[[LlmGateway, RunCallContext], Awaitable[CaseOutput]],
    *,
    transport: ChatTransport | None,
    fx_provider: FxQuoteProvider | None,
    fx_transport: httpx.AsyncBaseTransport | None,
) -> tuple[CaseOutput, tuple[LlmUsage, ...], tuple[LlmCallRecord, ...], int]:
    """Share gateway, ECB quote, deadline and failure trace for both case tasks."""
    ledger = InMemoryUsageLedger()
    trace = InMemoryLlmCallTrace()
    run = RunCallContext(
        run_id=uuid4(),
        workspace_id=_EVAL_WORKSPACE,
        attempt=1,
        engine=engine,
        deadline=datetime.now(UTC) + CASE_DEADLINE[engine],
    )
    started = time.monotonic()
    async with AsyncExitStack() as stack:
        effective = await stack.enter_async_context(_ClientScope(transport))
        if fx_provider is None:
            fx_client = await stack.enter_async_context(httpx.AsyncClient(transport=fx_transport))
            fx_provider = EcbFxQuoteCache(EcbFxRateAdapter(fx_client))
        gateway = LlmGateway(settings, effective, ledger, trace, fx_provider=fx_provider)
        try:
            output = await invoke(gateway, run)
        except LlmCallFailed as failure:
            raise ReviewCaseFailed(failure, tuple(record for _, record in trace.records)) from None
    return (
        output,
        tuple(item for _, item in ledger.events),
        tuple(record for _, record in trace.records),
        int((time.monotonic() - started) * 1000),
    )


async def review_case(
    case: ReviewCase,
    settings: LlmSettings,
    *,
    transport: ChatTransport | None = None,
    fx_provider: FxQuoteProvider | None = None,
    fx_transport: httpx.AsyncBaseTransport | None = None,
) -> ReviewCaseResult:
    """Run one case through the real gateway policy without a database.

    Raises ``ReviewCaseFailed`` (an ``LlmCallFailed`` with the normalized ``error_code``,
    like the worker path) that also carries every ``llm.call`` record of the case.
    """
    context = ReviewContext(
        system=case.system,
        rules=case.rules,
        agents_md=case.agents_md,
        conventions=case.conventions,
        pr_meta=case.pr_meta,
        changed_files=parse_unified_diff(case.diff),
        omitted_files=(),
    )
    result, _, calls, latency_ms = await _run_database_free_case(
        settings,
        case.engine,
        lambda gateway, run: review_with_gateway(gateway, context, run),
        transport=transport,
        fx_provider=fx_provider,
        fx_transport=fx_transport,
    )
    return ReviewCaseResult(
        output=parse_review_output(result.output),
        provider=result.provider,
        model=result.model,
        usage=result.usage,
        calls=calls,
        latency_ms=latency_ms,
    )


async def conventions_case(
    request: ConventionsRequest,
    settings: LlmSettings,
    *,
    transport: ChatTransport | None = None,
    fx_provider: FxQuoteProvider | None = None,
    fx_transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, object]:
    """Check the strict conventions call through the database-free gateway."""
    output, usage, calls, latency_ms = await _run_database_free_case(
        settings,
        "fast",
        lambda gateway, run: GatewayConventionsModel(gateway, run).draft_conventions(
            request=request
        ),
        transport=transport,
        fx_provider=fx_provider,
        fx_transport=fx_transport,
    )
    draft = ConventionsDraft.model_validate(output)
    return {
        "provider": usage[-1].provider,
        "model": usage[-1].model,
        "calls": [_call_json(item) for item in calls],
        **_usage_json(usage),
        "latency_ms": latency_ms,
        "files": len(draft.files),
        "output": draft.model_dump(mode="json"),
    }


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


def main(
    argv: list[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    transport: ChatTransport | None = None,
    fx_provider: FxQuoteProvider | None = None,
    fx_transport: httpx.AsyncBaseTransport | None = None,
) -> int:
    """Manual live run: prints provider, model, calls, tokens, cost and time as JSON.

    Exit 0 on an accepted answer, 1 when the gateway failed (the JSON then carries
    ``error_code`` and every call with its error), 2 on a configuration error. The
    environment is read as is: ``uv run --env-file .env …`` loads a local ``.env``.
    """
    parser = argparse.ArgumentParser(description="Check one strict gateway task on a unified diff.")
    parser.add_argument("diff", type=Path)
    parser.add_argument("--task", choices=("review", "conventions"), default="review")
    parser.add_argument("--system", type=Path, default=DEFAULT_SYSTEM_PROMPT)
    parser.add_argument("--conventions-system", type=Path, default=DEFAULT_CONVENTIONS_PROMPT)
    parser.add_argument("--engine", choices=("fast", "deep"), default="fast")
    parser.add_argument("--title", default="Live gateway check")
    args = parser.parse_args(argv)
    try:
        settings = LlmSettings.from_env(os.environ if env is None else env)
    except LlmConfigError as error:
        _emit({"error_code": "config_error", "message": str(error)})
        sys.stderr.write(f"configuration error: {error}\n")
        return 2
    diff = args.diff.read_text(encoding="utf-8")
    files = parse_unified_diff(diff)
    try:
        if args.task == "conventions":
            paths = tuple(file.path for file in files)
            request = ConventionsRequest(
                system=args.conventions_system.read_text(encoding="utf-8"),
                rules=(),
                agents_md=None,
                repo_tree=paths,
                repo_files=(),
                languages={},
                changed_files=paths,
            )
            payload = asyncio.run(
                conventions_case(
                    request,
                    settings,
                    transport=transport,
                    fx_provider=fx_provider,
                    fx_transport=fx_transport,
                )
            )
        else:
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
            result = asyncio.run(
                review_case(
                    case,
                    settings,
                    transport=transport,
                    fx_provider=fx_provider,
                    fx_transport=fx_transport,
                )
            )
            payload = {
                "provider": result.provider,
                "model": result.model,
                "calls": [_call_json(item) for item in result.calls],
                **_usage_json(result.usage),
                "latency_ms": result.latency_ms,
                "findings": len(result.output.findings),
                "output": result.output.model_dump(mode="json"),
            }
    except ReviewCaseFailed as failure:
        _emit(
            {
                "error_code": failure.error_code.value,
                "message": str(failure),
                "calls": [_call_json(item) for item in failure.trace],
                **_usage_json(failure.usage),
            }
        )
        sys.stderr.write(f"gateway failed: {failure.error_code.value}\n")
        return 1
    _emit(payload)
    return 0


def _call_json(record: LlmCallRecord) -> dict[str, object]:
    item: dict[str, object] = {
        "kind": record.kind.value,
        "model": record.model,
        "call_no": record.call_no,
        "duration_ms": record.duration_ms,
    }
    if record.fx is not None:
        item["fx"] = record.fx.as_json()
    if record.error is not None:
        item["error"] = record.response_json()["error"]
    else:
        item["response_model"] = (
            record.response.get("model") if isinstance(record.response, dict) else None
        )
    return item


def _usage_json(usage: tuple[LlmUsage, ...]) -> dict[str, object]:
    return {
        "tokens_in": sum(item.tokens_in for item in usage),
        "tokens_out": sum(item.tokens_out for item in usage),
        "cache_read_tokens": sum(item.cache_read_tokens for item in usage),
        "cost_usd": str(sum((item.cost_usd for item in usage), Decimal(0))),
    }


def _emit(payload: dict[str, object]) -> None:
    json.dump(payload, sys.stdout, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    raise SystemExit(main())
