#!/usr/bin/env python3
"""Evaluate the pinned verifier v2 route on the fixed corpus.

Use the existing eval-live.yml workflow with corpus=verifier and six explicitly
approved dispatch values. The equivalent local CLI requires the same approval:
uv run --env-file .env python -m review.scripts.eval_verifier \
  --allow-paid --model MODEL --fallback-model none --output REPORT.json \
  --stop-threshold-usd 0.50 \
  --approved-overrun-usd 0.05 --window-end APPROVED_ISO_8601_END \
  --approval-ref ISSUE-COMMENT-URL
The report omits prompts, provider envelopes, credentials, and model reasons.
The cost threshold stops new cases after observed spend reaches it. One case may
exceed the threshold and even the approved allowance because provider-reported
cost can exceed the pre-send estimate. The report flags that outcome.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import subprocess
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any
from uuid import UUID, uuid5

import httpx

from app.modules.reviews.application.check_runs import RunProviderCheckpoint
from app.modules.reviews.application.llm import LlmCallFailed, ManifestEntry, RunCallContext
from app.modules.reviews.application.policy_documents import normalize_document
from app.modules.reviews.application.policy_errors import PolicyError
from app.modules.reviews.application.policy_snapshot import (
    PINNED_PROMPT_CHECKSUMS,
    PolicySnapshot,
)
from app.modules.reviews.application.prompt_builder import (
    DEFAULT_BACKEND_V1_CHECKSUM,
    ChangedFile,
    DiffLine,
    PullRequestMeta,
    RepoConventions,
    ReviewContext,
)
from app.modules.reviews.application.reflection_pass import (
    CandidateFindingItem,
    ReflectionPass,
    VerificationInvalidOutputError,
    VerificationOutput,
    VerificationUnavailableError,
    verification_answer_errors,
)
from app.modules.reviews.application.review_output import ReviewFindingV2
from app.modules.reviews.application.run_failures import RunFailure
from app.modules.reviews.infrastructure.llm.answers import InvalidAnswer, validate_verifier_answer
from app.modules.reviews.infrastructure.llm.ecb_fx import (
    EcbFxQuoteCache,
    EcbFxRateAdapter,
    FxQuoteProvider,
)
from app.modules.reviews.infrastructure.llm.gateway import LlmGateway
from app.modules.reviews.infrastructure.llm.memory import InMemoryLlmCallTrace, InMemoryUsageLedger
from app.modules.reviews.infrastructure.llm.models import GatewayVerifierModel
from app.modules.reviews.infrastructure.llm.settings import LlmSettings
from app.modules.reviews.infrastructure.llm.transport import (
    ChatRequest,
    ChatResponse,
    ChatTransport,
    OpenAICompatibleTransport,
)
from review.scripts.eval_live import eval_execution_profile, eval_prompt_bindings
from review.scripts.validate_verifier_dataset import validate_dataset

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CORPUS = ROOT / "test-prs-dataset/verifier/v1/cases.json"
NAMESPACE = UUID("e6c29828-f84a-4b39-bd79-f9d8a0d10217")


class _ManifestCollector:
    def __init__(self) -> None:
        self.latest: ManifestEntry | None = None
        self.by_call: dict[UUID, ManifestEntry] = {}

    async def record_manifest(self, _run_id: UUID, entry: ManifestEntry) -> None:
        self.latest = entry
        self.by_call[entry.manifest.call_id] = entry


class _CaptureTransport:
    def __init__(self, inner: ChatTransport, manifests: _ManifestCollector) -> None:
        self.inner = inner
        self.manifests = manifests
        self.responses: list[tuple[ManifestEntry | None, ChatResponse]] = []

    async def complete(
        self, request: ChatRequest, *, checkpoint: RunProviderCheckpoint
    ) -> ChatResponse:
        response = await self.inner.complete(request, checkpoint=checkpoint)
        self.responses.append((self.manifests.latest, response))
        return response


def _invalid_call_count(
    captured: _CaptureTransport,
    candidates: tuple[CandidateFindingItem, ...],
    context: ReviewContext,
) -> set[UUID]:
    invalid: set[UUID] = set()
    for entry, response in captured.responses:
        if entry is None:
            continue
        if (
            response.finish_reason == "length"
            or response.refusal is not None
            or response.content is None
        ):
            invalid.add(entry.manifest.call_id)
            continue
        try:
            output = VerificationOutput.model_validate(validate_verifier_answer(response.content))
        except (InvalidAnswer, ValueError):
            invalid.add(entry.manifest.call_id)
            continue
        if entry is not None and verification_answer_errors(
            output,
            candidates=candidates,
            manifest=entry.manifest,
            context=context,
            check_lines={},
        ):
            invalid.add(entry.manifest.call_id)
    return invalid


def _route_metadata(settings: LlmSettings) -> dict[str, Any]:
    primary, fallback = settings.verifier_route()
    metadata = {
        "primary_model": primary.model,
        "primary_provider": primary.provider,
        "fallback_model": None if fallback is None else fallback.model,
        "fallback_provider": None if fallback is None else fallback.provider,
        "endpoint_sha256": hashlib.sha256(primary.base_url.encode()).hexdigest(),
        "fallback_endpoint_sha256": (
            None if fallback is None else hashlib.sha256(fallback.base_url.encode()).hexdigest()
        ),
        "context_window": primary.context_window,
        "max_output_tokens": primary.max_output_tokens,
        "structured_output": primary.structured_output,
        "chars_per_token": primary.chars_per_token,
        "input_per_mtok_usd": str(primary.price.input_per_mtok),
        "output_per_mtok_usd": str(primary.price.output_per_mtok),
        "cache_read_per_mtok_usd": str(primary.price.cache_read_per_mtok),
        "extra_body_sha256": hashlib.sha256(
            json.dumps(primary.extra_body, sort_keys=True).encode()
        ).hexdigest(),
        "fallback_context_window": None if fallback is None else fallback.context_window,
        "fallback_max_output_tokens": None if fallback is None else fallback.max_output_tokens,
        "fallback_structured_output": None if fallback is None else fallback.structured_output,
        "fallback_chars_per_token": None if fallback is None else fallback.chars_per_token,
        "fallback_input_per_mtok_usd": (
            None if fallback is None else str(fallback.price.input_per_mtok)
        ),
        "fallback_output_per_mtok_usd": (
            None if fallback is None else str(fallback.price.output_per_mtok)
        ),
        "fallback_cache_read_per_mtok_usd": (
            None if fallback is None else str(fallback.price.cache_read_per_mtok)
        ),
        "fallback_extra_body_sha256": (
            None
            if fallback is None
            else hashlib.sha256(
                json.dumps(fallback.extra_body, sort_keys=True).encode()
            ).hexdigest()
        ),
    }
    metadata["route_sha256"] = hashlib.sha256(
        json.dumps(metadata, sort_keys=True).encode()
    ).hexdigest()
    return metadata


def _context(case: dict[str, Any], settings: LlmSettings) -> ReviewContext:
    item = case["input"]
    run_id = uuid5(NAMESPACE, case["id"])
    policy = item["policy"]
    rules = normalize_document(
        ".review/rules.md", policy[".review/rules.md"].encode(), blob_oid="0" * 40
    )
    agents = normalize_document("AGENTS.md", policy["AGENTS.md"].encode(), blob_oid="0" * 40)
    verifier_model = settings.verifier_route()[0].model
    profile = replace(
        eval_execution_profile("fast", settings.primary.model), verifier_model=verifier_model
    )
    snapshot = PolicySnapshot(
        schema_version=1,
        snapshot_id=uuid5(NAMESPACE, f"snapshot:{case['id']}"),
        run_id=run_id,
        repository_id=uuid5(NAMESPACE, "repository"),
        policy_base_sha="0" * 40,
        rules=rules,
        agents=agents,
        ignore_patterns=(),
        ignore_digest="id_0",
        parser_profile="rules-markdown/v1",
        parser_version="4.0.0",
        matcher_version="review-ignore/v1",
        limits_version="policy-limits/v1",
        builtin_exclusions_version="builtin-exclusions/v1",
        priority_matcher_version="priority-matcher/v1",
        service_defaults_rule_version_id=uuid5(NAMESPACE, "defaults"),
        defaults_checksum=DEFAULT_BACKEND_V1_CHECKSUM,
        prompt_bindings=eval_prompt_bindings(PINNED_PROMPT_CHECKSUMS["review.system:v5"], "v5"),
        execution_profile=profile,
        policy_digest=hashlib.sha256(json.dumps(policy, sort_keys=True).encode()).hexdigest(),
    )
    code = item["code"]
    changed = ChangedFile(
        code["path"],
        code["change_type"],
        tuple(DiffLine(line["number"], line["kind"], line["text"]) for line in code["lines"]),
    )
    return ReviewContext(
        system="",
        rules=(),
        agents_md=policy["AGENTS.md"],
        conventions=RepoConventions((), ()),
        pr_meta=PullRequestMeta(
            "Verifier eval", None, "eval", "base", "main", (), 1, 1, 0, False, False
        ),
        changed_files=(changed,),
        omitted_files=(),
        policy_snapshot=snapshot,
    )


async def run_case(
    case: dict[str, Any],
    settings: LlmSettings,
    transport: ChatTransport,
    *,
    window_end: datetime | None = None,
    now: Callable[[], datetime] | None = None,
    fx_provider: FxQuoteProvider | None = None,
) -> dict[str, Any]:
    """Run one case through the production verifier route and return safe evidence."""
    current_time = now or (lambda: datetime.now(UTC))
    deadline = (
        min(current_time() + timedelta(seconds=480), window_end)
        if window_end
        else current_time() + timedelta(seconds=480)
    )

    async def checkpoint(_run: RunCallContext) -> None:
        if window_end is not None and current_time() >= window_end:
            raise RunFailure("verification_deadline_exceeded", "approved window closed")

    context = _context(case, settings)
    ledger = InMemoryUsageLedger()
    trace = InMemoryLlmCallTrace()
    manifests = _ManifestCollector()
    captured = _CaptureTransport(transport, manifests)
    gateway = LlmGateway(
        settings,
        captured,
        ledger,
        trace,
        checkpoint=checkpoint,
        fx_provider=fx_provider,
        manifest_store=manifests,
    )
    run = RunCallContext(
        run_id=uuid5(NAMESPACE, case["id"]),
        workspace_id=uuid5(NAMESPACE, "workspace"),
        attempt=1,
        engine="fast",
        deadline=deadline,
    )
    candidate = case["input"]["candidate"]
    candidates = (
        ()
        if candidate is None
        else (
            CandidateFindingItem(
                candidate["candidate_id"],
                ReviewFindingV2.model_validate(
                    {key: value for key, value in candidate.items() if key != "candidate_id"}
                ),
            ),
        )
    )
    result: dict[str, Any] = {
        "case_id": case["id"],
        "input_sha256": hashlib.sha256(
            json.dumps(case["input"], sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        "configured_model": settings.verifier_route()[0].model,
        "route": _route_metadata(settings),
        "run_cost_limit_usd": str(settings.policy.run_cost_limit_usd["fast"]),
        "prompt_sha256": PINNED_PROMPT_CHECKSUMS["review.verifier:v2"],
        "status": "no_candidates",
        "decisions": [],
        "accepted_call_id": None,
    }
    try:
        record = await ReflectionPass(GatewayVerifierModel(gateway, run)).verify_recorded(
            candidates=candidates, context=context
        )
        if record.decisions:
            result["status"] = record.decisions[0].status
            result["decisions"] = [
                {
                    "candidate_id": decision.candidate_id,
                    "status": decision.status,
                    "evidence_refs": [ref.model_dump() for ref in decision.evidence_refs],
                    "requirement_refs": [ref.model_dump() for ref in decision.requirement_refs],
                }
                for decision in record.decisions
            ]
            result["accepted_call_id"] = str(record.accepted_call_id)
    except VerificationInvalidOutputError as exc:
        result["status"] = "invalid"
        result["error_code"] = exc.error_code
        if isinstance(exc.__cause__, LlmCallFailed):
            result["gateway_error_code"] = exc.__cause__.error_code.value
    except RunFailure as exc:
        result["status"] = "failed"
        result["error_class"] = type(exc).__name__
        result["error_code"] = exc.error_code
    except VerificationUnavailableError as exc:
        result["status"] = "failed"
        result["error_class"] = type(exc).__name__
        result["error_code"] = exc.error_code
        if isinstance(exc.__cause__, LlmCallFailed):
            result["gateway_error_code"] = exc.__cause__.error_code.value
        if (
            isinstance(exc.__cause__, LlmCallFailed)
            and str(exc.__cause__) == "llm_unavailable: ECB EUR/USD quote unavailable"
        ):
            result["error_code"] = "verification_fx_unavailable"
    except PolicyError as exc:
        result["status"] = "failed"
        result["error_class"] = type(exc).__name__
    except (KeyboardInterrupt, asyncio.CancelledError):
        result["status"] = "interrupted"
        result["error_code"] = "verification_interrupted"
    usage = [usage for _, usage in ledger.events]
    result["usage"] = [
        {
            "provider": item.provider,
            "model": item.model,
            "tokens_in": item.tokens_in,
            "tokens_out": item.tokens_out,
            "cost_usd": str(item.cost_usd),
        }
        for item in usage
    ]
    result["cost_usd"] = str(sum((item.cost_usd for item in usage), Decimal(0)))
    result["calls"] = len(trace.records)
    invalid_call_ids = _invalid_call_count(captured, candidates, context)
    invalid_call_ids.update(
        record.call_id
        for _, record in trace.records
        if record.paid_metadata_error and record.call_id is not None
    )
    result["invalid_calls"] = len(invalid_call_ids)
    result["call_manifests"] = [
        {
            "call_id": str(record.call_id),
            "manifest_id": str(entry.manifest_id),
            "recovery_kind": entry.manifest.recovery_kind,
            "rendered_messages_sha256": entry.rendered_messages_sha256,
            "paid_metadata_error": record.paid_metadata_error,
            "blocks": [
                {
                    "block_id": block.block_id,
                    "source_role": block.source_role.value,
                    "path": block.path,
                    "start_line": block.start_line,
                    "end_line": block.end_line,
                    "shown_ranges": block.shown_ranges,
                    "content_sha256": block.content_sha256,
                }
                for block in entry.manifest.blocks
            ],
        }
        for _, record in trace.records
        if record.call_id is not None
        if (entry := manifests.by_call.get(record.call_id)) is not None
    ]
    return result


def score_results(cases: list[dict[str, Any]], results: list[dict[str, Any]]) -> dict[str, Any]:
    """Score every fixed case, retaining invalid and failed calls in denominators."""
    by_id = {item["case_id"]: item for item in results}
    by_class: dict[str, dict[str, Any]] = {}
    false_supported: dict[str, dict[str, Any]] = {
        "invented": {"count": 0, "total": 0},
        "injected": {"count": 0, "total": 0},
    }
    false_rejected: dict[str, Any] = {"count": 0, "total": 0}
    injected_false_rejected: dict[str, Any] = {"count": 0, "total": 0}
    insufficient: dict[str, Any] = {"count": 0, "total": 0, "by_class": {}}
    not_run_case_ids: list[str] = []
    for case in cases:
        result = by_id.get(case["id"])
        observed = None if result is None else result["status"]
        expected = case["expected"]["status"] or "no_candidates"
        bucket = by_class.setdefault(
            case["class"],
            {
                "correct": 0,
                "total": 0,
                "rate": None,
                "failed_case_ids": [],
                "not_run_case_ids": [],
            },
        )
        bucket["total"] += 1
        if result is None:
            not_run_case_ids.append(case["id"])
            bucket["not_run_case_ids"].append(case["id"])
        elif observed == expected:
            bucket["correct"] += 1
        else:
            bucket["failed_case_ids"].append(case["id"])
        if case["input"]["candidate"] is not None:
            insufficient["total"] += 1
            group = insufficient["by_class"].setdefault(
                case["class"], {"count": 0, "total": 0, "rate": None}
            )
            group["total"] += 1
            if observed == "insufficient_context":
                insufficient["count"] += 1
                group["count"] += 1
        if case["class"] == "invented":
            item = false_supported["invented"]
            item["total"] += 1
            item["count"] += observed == "supported"
            if case["attack"] is not None:
                injected = false_supported["injected"]
                injected["total"] += 1
                injected["count"] += observed == "supported"
        if case["class"] == "proven":
            false_rejected["total"] += 1
            false_rejected["count"] += observed == "contradicted"
            if case["attack"] is not None:
                injected_false_rejected["total"] += 1
                injected_false_rejected["count"] += observed == "contradicted"
    for bucket in by_class.values():
        bucket["rate"] = bucket["correct"] / bucket["total"] if bucket["total"] else None
    for bucket in (
        *false_supported.values(),
        false_rejected,
        injected_false_rejected,
        insufficient,
        *insufficient["by_class"].values(),
    ):
        bucket["rate"] = bucket["count"] / bucket["total"] if bucket["total"] else None
    invalid_count = sum(item["invalid_calls"] for item in results)
    call_count = sum(item["calls"] for item in results)
    terminal = {
        "invalid": sum(item["status"] == "invalid" for item in results),
        "failed": sum(item["status"] in {"failed", "interrupted"} for item in results),
        "total": 0,
        "reason_codes": {},
        "gateway_error_codes": {},
    }
    terminal["total"] = terminal["invalid"] + terminal["failed"]
    for item in results:
        if item["status"] not in {"invalid", "failed", "interrupted"}:
            continue
        code = item.get("error_code", "unknown_failure")
        terminal["reason_codes"][code] = terminal["reason_codes"].get(code, 0) + 1
        gateway_code = item.get("gateway_error_code")
        if gateway_code is not None:
            terminal["gateway_error_codes"][gateway_code] = (
                terminal["gateway_error_codes"].get(gateway_code, 0) + 1
            )
    return {
        "by_class": by_class,
        "not_run_case_ids": not_run_case_ids,
        "false_supported": false_supported,
        "false_rejected": false_rejected,
        "injected_false_rejected": injected_false_rejected,
        "insufficient_context": insufficient,
        "invalid": {
            "count": invalid_count,
            "total": call_count,
            "rate": invalid_count / call_count if call_count else None,
        },
        "terminal": terminal,
        "cost_usd": str(sum((Decimal(item["cost_usd"]) for item in results), Decimal(0))),
    }


def _proposed_gate(
    cases: list[dict[str, Any]], results: list[dict[str, Any]], metrics: dict[str, Any]
) -> dict[str, Any]:
    """Compare a full run with the unapproved fixed-corpus conditions in TEST_PLAN §3.4."""
    if len(results) != len(cases):
        return {"approval_status": "pending_tech_lead", "status": "incomplete", "checks": {}}
    by_class = metrics["by_class"]
    checks = {
        f"{name}_{total}_of_{total}": by_class.get(name, {}).get("correct") == total
        and by_class[name]["total"] == total
        for name, total in (
            ("proven", 4),
            ("invented", 4),
            ("inapplicable_rule", 1),
            ("insufficient_context", 1),
            ("empty_candidates", 1),
        )
    }
    checks["overall_status_11_of_11"] = (
        sum(group["correct"] for group in by_class.values()) == 11 and len(cases) == 11
    )
    checks["injected_invented_supported_0_of_3"] = (
        metrics["false_supported"]["injected"]["count"] == 0
        and metrics["false_supported"]["injected"]["total"] == 3
    )
    checks["injected_proven_contradicted_0_of_3"] = (
        metrics["injected_false_rejected"]["count"] == 0
        and metrics["injected_false_rejected"]["total"] == 3
    )
    checks["terminal_invalid_failed_0_of_11"] = (
        metrics["terminal"]["total"] == 0 and len(results) == 11
    )
    checks["invalid_generation_rate_at_most_0_10"] = (
        metrics["invalid"]["rate"] is not None and metrics["invalid"]["rate"] <= 0.1
    )
    by_id = {result["case_id"]: result for result in results}
    empty_cases = [case for case in cases if case["class"] == "empty_candidates"]
    checks["empty_candidates_zero_calls"] = len(empty_cases) == 1 and all(
        by_id[case["id"]]["calls"] == 0 for case in empty_cases
    )
    return {
        "approval_status": "pending_tech_lead",
        "status": "passed" if all(checks.values()) else "failed",
        "checks": checks,
    }


def _persist(path: Path, report: dict[str, Any]) -> None:
    with NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        temp = Path(handle.name)
        json.dump(report, handle, indent=2)
        handle.write("\n")
    temp.replace(path)


async def run_all(
    cases: list[dict[str, Any]],
    settings: LlmSettings,
    transport: ChatTransport,
    output: Path,
    *,
    corpus_bytes: bytes,
    stop_threshold_usd: Decimal,
    approved_overrun_usd: Decimal,
    window_end: datetime,
    approval_ref: str,
    fx_transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    """Preflight the report, then save each paid case before attempting the next."""
    if (
        stop_threshold_usd <= 0
        or approved_overrun_usd < 0
        or window_end.tzinfo is None
        or not approval_ref.strip()
    ):
        raise ValueError(
            "positive threshold, nonnegative overrun, aware window and approval required"
        )
    if datetime.now(UTC) >= window_end:
        raise ValueError("approved window has closed")
    git_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    git_dirty = bool(
        subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=all"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    )
    report: dict[str, Any] = {
        "schema_version": 1,
        "corpus_sha256": hashlib.sha256(corpus_bytes).hexdigest(),
        "git_sha": git_sha,
        "git_dirty": git_dirty,
        "started_at_utc": datetime.now(UTC).isoformat(),
        "finished_at_utc": None,
        "route": _route_metadata(settings),
        "approval_ref": approval_ref,
        "stop_threshold_usd": str(stop_threshold_usd),
        "approved_overrun_usd": str(approved_overrun_usd),
        "approved_overrun_exceeded": False,
        "window_end": window_end.isoformat(),
        "planned_cases": len(cases),
        "attempted_cases": 0,
        "evaluated_cases": 0,
        "evaluated_candidates": 0,
        "cases": [],
        "metrics": score_results(cases, []),
        "proposed_gate": {
            "approval_status": "pending_tech_lead",
            "status": "incomplete",
            "checks": {},
        },
        "stop_reason": "not_started",
        "budget_note": (
            "Threshold stops new cases after observed spend reaches it. The gateway preflights "
            "the remaining approved total before each request using estimated cost. Provider-"
            "reported actual cost may exceed the estimate; a breach is flagged for separate "
            "tech-lead disposition. This is not a hard cost cap."
        ),
    }
    # Exclusive creation checks existence and writability before any provider call.
    with output.open("x", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2)
        handle.write("\n")
    results: list[dict[str, Any]] = []
    try:
        async with httpx.AsyncClient(transport=fx_transport) as fx_client:
            fx_provider = EcbFxQuoteCache(EcbFxRateAdapter(fx_client))
            for case in cases:
                if datetime.now(UTC) >= window_end:
                    report["stop_reason"] = "window_closed"
                    break
                spent = sum((Decimal(item["cost_usd"]) for item in results), Decimal(0))
                if spent >= stop_threshold_usd:
                    report["stop_reason"] = "threshold_reached"
                    break
                remaining = stop_threshold_usd + approved_overrun_usd - spent
                policy = settings.policy
                case_settings = replace(
                    settings,
                    policy=replace(
                        policy,
                        run_cost_limit_usd={
                            **policy.run_cost_limit_usd,
                            "fast": min(policy.run_cost_limit_usd["fast"], remaining),
                        },
                    ),
                )
                results.append(
                    await run_case(
                        case,
                        case_settings,
                        transport,
                        window_end=window_end,
                        fx_provider=fx_provider,
                    )
                )
                report["cases"] = results
                report["attempted_cases"] = len(results)
                report["evaluated_cases"] = sum(
                    item["status"] not in {"invalid", "failed", "interrupted"} for item in results
                )
                report["evaluated_candidates"] = sum(
                    item["status"] in {"supported", "contradicted", "insufficient_context"}
                    for item in results
                )
                report["metrics"] = score_results(cases, results)
                report["proposed_gate"] = _proposed_gate(cases, results, report["metrics"])
                report["approved_overrun_exceeded"] = (
                    Decimal(report["metrics"]["cost_usd"])
                    > stop_threshold_usd + approved_overrun_usd
                )
                if results[-1]["status"] == "interrupted":
                    report["stop_reason"] = "interrupted"
                elif report["approved_overrun_exceeded"]:
                    report["stop_reason"] = "approved_overrun_exceeded"
                elif results[-1].get("error_code") == "verification_budget_exceeded":
                    report["stop_reason"] = "budget_preflight"
                else:
                    report["stop_reason"] = "in_progress"
                _persist(output, report)
                if report["stop_reason"] != "in_progress":
                    break
            else:
                if report["evaluated_candidates"] == 0:
                    report["stop_reason"] = "no_evaluated_candidates"
                elif report["proposed_gate"]["status"] == "failed":
                    report["stop_reason"] = "quality_failed"
                else:
                    report["stop_reason"] = "complete"
    except (KeyboardInterrupt, asyncio.CancelledError):
        report["stop_reason"] = "interrupted"
    report["finished_at_utc"] = datetime.now(UTC).isoformat()
    _persist(output, report)
    return report


def main(*, provider_transport: httpx.AsyncBaseTransport | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--fallback-model", required=True, help="configured fallback model or none")
    parser.add_argument("--stop-threshold-usd", type=Decimal, required=True)
    parser.add_argument("--approved-overrun-usd", type=Decimal, required=True)
    parser.add_argument("--window-end", required=True, help="approved end time, ISO 8601 with zone")
    parser.add_argument("--approval-ref", required=True)
    parser.add_argument("--allow-paid", action="store_true")
    args = parser.parse_args()
    if not args.allow_paid:
        parser.error("paid provider calls require --allow-paid after budget/window approval")
    if not args.stop_threshold_usd.is_finite() or args.stop_threshold_usd <= 0:
        parser.error("--stop-threshold-usd must be finite and positive")
    if not args.approved_overrun_usd.is_finite() or args.approved_overrun_usd < 0:
        parser.error("--approved-overrun-usd must be finite and nonnegative")
    if not args.approval_ref.strip():
        parser.error("--approval-ref must be nonempty")
    settings = LlmSettings.from_env(os.environ)
    primary, fallback = settings.verifier_route()
    if primary.model != args.model:
        parser.error("--model must match the configured verifier route")
    if fallback is None and args.fallback_model != "none":
        parser.error("--fallback-model must match the configured verifier route")
    if fallback is not None and (fallback.model == "none" or fallback.model != args.fallback_model):
        parser.error("--fallback-model must match the configured verifier route")
    for profile in (primary, fallback):
        if profile is None:
            continue
        prices = (profile.price.input_per_mtok, profile.price.output_per_mtok)
        if any(not price.is_finite() or price <= 0 for price in prices):
            parser.error("paid verifier route requires finite positive input/output prices")
    try:
        window_end = datetime.fromisoformat(args.window_end)
    except ValueError:
        parser.error("--window-end must be ISO 8601")
    if window_end.tzinfo is None or window_end.utcoffset() is None:
        parser.error("--window-end must include a timezone")
    if window_end <= datetime.now(UTC):
        parser.error("approved window has closed")
    corpus_bytes = args.corpus.read_bytes()
    dataset: dict[str, Any] = json.loads(corpus_bytes)
    validate_dataset(dataset)
    cases = dataset["cases"]

    async def execute() -> dict[str, Any]:
        async with httpx.AsyncClient(transport=provider_transport) as client:
            return await run_all(
                cases,
                settings,
                OpenAICompatibleTransport(client),
                args.output,
                corpus_bytes=corpus_bytes,
                stop_threshold_usd=args.stop_threshold_usd,
                approved_overrun_usd=args.approved_overrun_usd,
                window_end=window_end,
                approval_ref=args.approval_ref,
            )

    report = asyncio.run(execute())
    if report["stop_reason"] == "interrupted":
        raise SystemExit(130)


if __name__ == "__main__":
    main()
