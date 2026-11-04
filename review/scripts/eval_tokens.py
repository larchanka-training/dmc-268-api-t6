#!/usr/bin/env python3
"""Offline estimate of the input tokens of the review request on the gold corpus (#317).

Usage: uv run python review/scripts/eval_tokens.py --system FILE [--system FILE] \\
           [--policy-arm legacy|none] [--root DIR]

No key, no network, no paid call. ``record_live`` runs with a transport that stays in this process
and writes its capture to a temporary directory, so the envelope and the fit to the window are
those of a live capture of the same arm; nothing is rendered a second time here. The output is
the estimate per case and the sum, and for two prompt files the change of B against A in percent.

The exit code is 0 on success, 1 when the recorder refuses the inputs or a case did not make
exactly one accepted review call, 2 on bad arguments. How to read the figures:
test-prs-dataset/README.md, "Offline token estimate".
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.modules.reviews.application.check_runs import RunProviderCheckpoint  # noqa: E402
from app.modules.reviews.infrastructure.llm.gateway import HeuristicTokenCounter  # noqa: E402
from app.modules.reviews.infrastructure.llm.settings import LlmSettings  # noqa: E402
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
from review.scripts.eval_rag_rehearsal import (  # noqa: E402
    DEFAULT_MODEL,
    RehearsalTransport,
    rehearsal_settings,
)
from review.scripts.validate_dataset import DEFAULT_ROOT  # noqa: E402

# The arms a corpus without policy-arm fields can run: ``valid`` and ``malicious`` need them.
POLICY_ARMS = ("legacy", "none")
LABELS = ("A", "B")


class _RequestTap(RehearsalTransport):
    """The rehearsal's answer without leaving the process, keeping every request it is sent."""

    def __init__(self) -> None:
        self.requests: list[ChatRequest] = []

    async def complete(
        self, request: ChatRequest, *, checkpoint: RunProviderCheckpoint
    ) -> ChatResponse:
        self.requests.append(request)
        return await super().complete(request, checkpoint=checkpoint)


@dataclass(frozen=True)
class PromptTokens:
    """One prompt file's estimate per case, in the recorder's case order."""

    prompt_path: str
    prompt_version: str
    by_case: dict[str, int]

    @property
    def total(self) -> int:
        return sum(self.by_case.values())


def _estimate(request: ChatRequest) -> int:
    """The gateway's pre-send estimate of the request: every message at ``chars_per_token``.

    The snapshot path counts with counter/v2, the same figure at 3.0 (test_gateway_counter_parity).
    """
    counter = HeuristicTokenCounter(request.profile.chars_per_token)
    return sum(counter.count(message.content) for message in request.messages)


def measure(
    root: Path, system_prompt: Path, policy_arm: str, settings: LlmSettings
) -> PromptTokens:
    """Run the live capture's own path over the corpus with an in-process transport.

    Raises ``RecorderError`` for what the recorder refuses and for a case that did not make exactly
    one accepted review call: an overflow sends nothing, a repair call adds a second request.
    """
    tap = _RequestTap()
    with tempfile.TemporaryDirectory(prefix="eval-tokens-") as scratch:
        manifest = asyncio.run(
            record_live(
                root,
                settings,
                system_prompt=system_prompt,
                backend_rules=BACKEND_RULES,
                frontend_rules=FRONTEND_RULES,
                transport=tap,
                policy_arm=policy_arm,
                responses_dir=Path(scratch) / "capture",
            )
        )
    cases = manifest["run_metadata"]["cases"]
    usage = manifest["run_metadata"]["usage"]
    unmeasured = [
        f"{case_id} ({status['gateway_status']}, {len(usage[case_id])} calls)"
        for case_id, status in cases.items()
        if status["gateway_status"] != "accepted" or len(usage[case_id]) != 1
    ]
    if unmeasured:
        raise RecorderError(
            "cases without exactly one accepted review call: " + ", ".join(unmeasured)
        )
    return PromptTokens(
        prompt_path=manifest["prompt_path"],
        prompt_version=manifest["prompt_version"],
        by_case={
            case_id: _estimate(request)
            for case_id, request in zip(cases, tap.requests, strict=True)
        },
    )


def _row(name: str, tokens: Sequence[int]) -> list[str]:
    cells = [name, *(str(value) for value in tokens)]
    if len(tokens) == 2:
        # The base is never 0: the system message holds at least the prompt's version line.
        cells.append(f"{float(Fraction(tokens[1] - tokens[0], tokens[0]) * 100):+.1f}%")
    return cells


def format_table(
    results: Sequence[PromptTokens], *, root: Path, policy_arm: str, settings: LlmSettings
) -> str:
    """The run's inputs, then the estimates by case and their sum; two files add ``B vs A``."""
    profile = settings.primary
    # Relative inside the repository, so the output of two clones is the same.
    shown_root = root.relative_to(REPO_ROOT) if root.is_relative_to(REPO_ROOT) else root
    labelled = list(zip(LABELS, results, strict=False))
    rows = [
        ["case", *(label for label, _ in labelled), *(["B vs A"] if len(results) == 2 else [])],
        *(
            _row(case_id, [result.by_case[case_id] for result in results])
            for case_id in results[0].by_case
        ),
        _row("total", [result.total for result in results]),
    ]
    widths = [max(len(cell) for cell in column) for column in zip(*rows, strict=True)]
    return "\n".join(
        [
            f"eval_tokens: root {shown_root}, policy arm {policy_arm}, model {profile.model}, "
            f"chars_per_token {profile.chars_per_token}",
            *(
                f"{label}: {result.prompt_path} ({result.prompt_version})"
                for label, result in labelled
            ),
            *(
                "  ".join(
                    [
                        row[0].ljust(widths[0]),
                        *(
                            cell.rjust(width)
                            for cell, width in zip(row[1:], widths[1:], strict=True)
                        ),
                    ]
                )
                for row in rows
            ),
        ]
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT, help="dataset root")
    parser.add_argument(
        "--system",
        type=Path,
        action="append",
        required=True,
        metavar="FILE",
        help="review prompt inside the repository; give it twice to compare two files",
    )
    parser.add_argument(
        "--policy-arm", choices=POLICY_ARMS, default="legacy", help="as in eval_live.py"
    )
    args = parser.parse_args(argv)
    if len(args.system) > len(LABELS):
        parser.error("--system takes one prompt file, or two to compare")
    settings = rehearsal_settings(DEFAULT_MODEL)
    try:
        results = [measure(args.root, path, args.policy_arm, settings) for path in args.system]
    except (RecorderError, OSError, ValueError) as exc:
        print(f"eval_tokens: {exc}", file=sys.stderr)
        return 1
    print(format_table(results, root=args.root, policy_arm=args.policy_arm, settings=settings))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
