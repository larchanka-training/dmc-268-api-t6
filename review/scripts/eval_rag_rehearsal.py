#!/usr/bin/env python3
"""Offline dress rehearsal of the graph-RAG v1 contribution eval (#146), with a token projection.

Usage: uv run python review/scripts/eval_rag_rehearsal.py [--root test-prs-dataset/rag] \\
           [--model mistral-small-4] [--out DIR] [--require-gate]

One command runs the whole chain of the paid recording without a single paid call: record arm
A three times (``eval_live --rag-arm none``, runs ``rag_off-1..3``), record arm B three times
(``--rag-arm graph``, runs ``rag_on-1..3``), replay the six captures and run the pre-registered
gate (``eval_rag_gate``) over them. The model is a fake transport inside this process: no
network, no key, no ``.env``, nothing leaves the machine. Every answer is the same empty review,
so the gate verdict of a rehearsal means nothing (no finding, so no Recall); what it proves is
that the chain runs and what the real run will cost in input tokens.

The projection printed at the end is the one to read: per arm, the input tokens of the
72 review calls (24 cases x 3 runs) as a total and a median, and the median B/A ratio the gate
holds to <= 1.30 (ADR 0001 section 12). The fake transport reports, as the provider's prompt
token count, the gateway's own pre-send estimate (``HeuristicTokenCounter`` at the model's
``chars_per_token``) of the two messages it was sent. The prompt, the retrieved block and the
fit to the model's window are the recorder's (``record_live``: the production context build,
``fit_review_context`` at ``input_budget_tokens``); nothing is computed a second time here.

How to read the ratio: the estimate is 3 characters per token for prose and code alike, while
a provider's tokenizer spends more tokens per character on code than on prose, so the retrieved
code block (arm B only) is understated against the prose-heavy base prompt and the true B/A
ratio is higher than the printed one. A printed ratio above 1.30 means stop. At or below about
1.20 it is probably safe. Between 1.20 and 1.30 it is unproven. Before the full paid run, send
one pilot pair (one arm A call and one arm B call on the same case) and compare the provider's
``prompt_tokens`` with this estimate; that pilot belongs to the user-gated paid step.
``--model`` picks the window and ``chars_per_token`` of a known model of the catalog
(``KNOWN_MODELS``); use the model the paid run will use. A window too small for arm B (its
block fitted away) is refused by the gate's loader, as in the real run.

The corpus rules are the gate's, not weakened here: the gate needs exactly 10 reverse-edge
cases, at least 24 cases, distinct ids. A corpus that does not meet them (the tiny fixtures)
still gets the projection, and the gate part prints ``gate skipped: <the gate's own refusal>``;
any other refusal of the gate (the runs) stops the command with exit code 2. With
``--require-gate`` a skipped gate exits 3 (after printing the summary), so a script or CI step
cannot mistake a skipped gate for a green rehearsal.

``--out DIR`` keeps the six capture directories (``DIR/rag_off-1`` ...) for inspection; without
it they live in a temporary directory (``TMPDIR`` is honoured) that is removed on exit. The
exit code is 0 when the chain ran, 3 when the gate was skipped under ``--require-gate``, 1
when a recording was refused (an invalid corpus, a prompt that is not the pinned v5) and 2 when
the gate (its loader, or its checks of the runs) refused the captures. The run takes about 1 s
per case and run (~3 min for 24 cases).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from fractions import Fraction
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.modules.reviews.application.check_runs import RunProviderCheckpoint  # noqa: E402
from app.modules.reviews.infrastructure.llm.gateway import HeuristicTokenCounter  # noqa: E402
from app.modules.reviews.infrastructure.llm.settings import (  # noqa: E402
    KNOWN_MODELS,
    LlmSettings,
)
from app.modules.reviews.infrastructure.llm.transport import (  # noqa: E402
    ChatRequest,
    ChatResponse,
)
from review.scripts.eval_live import (  # noqa: E402
    BACKEND_RULES,
    FRONTEND_RULES,
    REPO_ROOT,
    RecorderError,
    record_live,
)
from review.scripts.eval_rag_gate import (  # noqa: E402
    MAX_TOKEN_RATIO,
    RAG_OFF,
    RAG_ON,
    RUNS_PER_ARM,
    GateCase,
    GateInputError,
    GateRun,
    check_corpus,
    compute_gate,
    judge_token_ratio,
    load_inputs,
    median,
)
from review.scripts.eval_replay import RAG_ARM_LABELS  # noqa: E402
from review.scripts.validate_dataset import DEFAULT_ROOT  # noqa: E402

SYSTEM_V5 = REPO_ROOT / "review" / "prompts" / "review.system.v5.md"
DEFAULT_MODEL = "mistral-small-4"
# An arbitrary fixed moment: the three runs of an arm must differ in bytes (the A/B loader
# refuses a copied report), so run N is recorded N hours after it.
FIRST_RECORDED_AT = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
EMPTY_REVIEW = (
    '{"findings": [], "summary": {"problem": "No issue was found.", '
    '"done_well": "The change is clear.", "effort": "none"}}'
)
COMPLETION_TOKENS = 30
_ARM_OF_LABEL = {label: arm for arm, label in RAG_ARM_LABELS.items()}  # rag_off -> none


class RehearsalTransport:
    """A chat transport that never leaves the process: the same empty review for every request.

    The provider's prompt token count is the gateway's estimate of the two messages, so the
    recorded ``tokens_in`` moves with the prompt exactly as far as the prompt does.
    """

    async def complete(
        self, request: ChatRequest, *, checkpoint: RunProviderCheckpoint
    ) -> ChatResponse:
        await checkpoint()
        counter = HeuristicTokenCounter(request.profile.chars_per_token)
        prompt_tokens = sum(counter.count(message.content) for message in request.messages)
        model = request.profile.model
        raw: dict[str, Any] = {
            "model": model,
            "choices": [{"message": {"content": EMPTY_REVIEW}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": COMPLETION_TOKENS},
        }
        return ChatResponse(
            raw, EMPTY_REVIEW, "stop", model, prompt_tokens, COMPLETION_TOKENS, 0, None, None
        )


def rehearsal_settings(model: str) -> LlmSettings:
    """The window, output reserve, price and ``chars_per_token`` of a catalog model, with a
    placeholder key and an endpoint that cannot resolve: the transport is never the real one."""
    profile = replace(
        KNOWN_MODELS[model],
        base_url="https://rehearsal.invalid/v1",
        api_keys=("rehearsal-placeholder-key",),
    )
    return LlmSettings(profile, None)


@dataclass(frozen=True)
class ArmTokens:
    """An arm's input tokens over its runs: the total of the usage records, and the median over
    the review calls (``None`` when a case has no recorded review call)."""

    total: int
    calls: int
    median: Fraction | None


@dataclass(frozen=True)
class Projection:
    off: ArmTokens
    on: ArmTokens

    @property
    def ratio(self) -> Fraction | None:
        """Median B / median A; the gate's ``tokens`` condition (``None`` when undefined)."""
        if self.off.median is None or self.on.median is None or self.off.median == 0:
            return None
        return self.on.median / self.off.median


def _arm_tokens(runs: Sequence[GateRun]) -> ArmTokens:
    review_calls = [item.tokens_in for run in runs for item in run.cases.values()]
    known = [value for value in review_calls if value is not None]
    return ArmTokens(
        total=sum(run.usage.tokens_in for run in runs if run.usage is not None),
        calls=len(review_calls),
        median=median(known) if len(known) == len(review_calls) else None,
    )


def project_tokens(runs: Sequence[GateRun]) -> Projection:
    return Projection(
        off=_arm_tokens([run for run in runs if run.arm == RAG_OFF]),
        on=_arm_tokens([run for run in runs if run.arm == RAG_ON]),
    )


@dataclass(frozen=True)
class Rehearsal:
    corpus: list[GateCase]
    runs: list[GateRun]
    projection: Projection
    warnings: list[str]
    gate: dict[str, Any] | None  # the computed gate, or None when it refused the corpus
    gate_skipped: str | None  # the gate's own refusal


async def _record_runs(root: Path, settings: LlmSettings, out: Path) -> list[tuple[str, Path]]:
    captures: list[tuple[str, Path]] = []
    moment = FIRST_RECORDED_AT
    for label in (RAG_OFF, RAG_ON):
        for number in range(1, RUNS_PER_ARM + 1):
            directory = out / f"{label}-{number}"
            await record_live(
                root,
                settings,
                system_prompt=SYSTEM_V5,
                backend_rules=BACKEND_RULES,
                frontend_rules=FRONTEND_RULES,
                transport=RehearsalTransport(),
                recorded_at=moment,
                engine="fast",
                policy_arm="none",
                responses_dir=directory,
                rag_arm=_ARM_OF_LABEL[label],
            )
            moment += timedelta(hours=1)
            captures.append((label, directory))
    return captures


def rehearse(root: Path, out: Path, settings: LlmSettings) -> Rehearsal:
    """Record A x3, record B x3, replay and gate; raises what the recorder or the loader refuse."""
    out.mkdir(parents=True, exist_ok=True)
    captures = asyncio.run(_record_runs(root, settings, out))
    # Outside the event loop: the replay runs its own.
    corpus, runs, warnings = load_inputs(root, captures)
    # Only the gate's refusal of the CORPUS is a skip (the tiny fixtures, a corpus still being
    # written); any other refusal of ``compute_gate`` (the runs) is an error, not a skip.
    gate: dict[str, Any] | None
    skipped: str | None
    try:
        check_corpus(corpus)
    except GateInputError as exc:
        gate, skipped = None, str(exc)
    else:
        gate, skipped = compute_gate(corpus, runs, None), None
    return Rehearsal(corpus, runs, project_tokens(runs), warnings, gate, skipped)


def _ratio_text(ratio: Fraction | None) -> str:
    if ratio is None:
        return "n/a"
    return f"{float(ratio):.4f} (exact {ratio.numerator}/{ratio.denominator})"


def _arm_line(title: str, tokens: ArmTokens) -> str:
    median_text = "n/a" if tokens.median is None else str(float(tokens.median))
    return (
        f"input tokens, {title}: total {tokens.total}, "
        f"median {median_text} over {tokens.calls} review calls"
    )


def format_summary(result: Rehearsal, *, root: Path, model: str) -> str:
    projection = result.projection
    status = judge_token_ratio(projection.off.median, projection.on.median)
    lines = [
        f"rehearsal: root {root}, model {model}, fake transport (nothing left the process)",
        f"cases: {len(result.corpus)}; runs: {RUNS_PER_ARM} per arm",
        _arm_line("arm A (rag_off)", projection.off),
        _arm_line("arm B (rag_on)", projection.on),
        f"median B/A: {_ratio_text(projection.ratio)}; "
        f"gate threshold <= {float(MAX_TOKEN_RATIO):.2f}: {status}",
    ]
    if result.gate is None:
        lines.append(f"gate skipped: {result.gate_skipped}")
    else:
        conditions = ", ".join(
            f"{name} {item['status']}" for name, item in result.gate["conditions"].items()
        )
        lines.append(
            f"gate: {result.gate['verdict']} ({conditions}); the answers are empty reviews, "
            "so only the token condition is meaningful"
        )
    lines.extend(f"warning: {warning}" for warning in result.warnings)
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT / "rag", help="RAG dataset root")
    parser.add_argument(
        "--model",
        choices=sorted(KNOWN_MODELS),
        default=DEFAULT_MODEL,
        help="catalog model whose window and chars_per_token the fit and the estimate use",
    )
    parser.add_argument(
        "--out", type=Path, metavar="DIR", help="keep the six capture directories here"
    )
    parser.add_argument(
        "--require-gate",
        action="store_true",
        help="exit 3 when the gate was skipped (the corpus does not meet its rules)",
    )
    args = parser.parse_args(argv)
    settings = rehearsal_settings(args.model)
    try:
        if args.out is not None:
            result = rehearse(args.root, args.out, settings)
        else:
            with tempfile.TemporaryDirectory(prefix="eval-rag-rehearsal-") as scratch:
                result = rehearse(args.root, Path(scratch), settings)
    except GateInputError as exc:
        print(f"eval_rag_rehearsal: {exc}", file=sys.stderr)
        return 2
    except (RecorderError, OSError, ValueError) as exc:
        print(f"eval_rag_rehearsal: {exc}", file=sys.stderr)
        return 1
    print(format_summary(result, root=args.root, model=args.model), end="")
    if args.require_gate and result.gate is None:
        print("eval_rag_rehearsal: the gate was skipped and --require-gate is set", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
