#!/usr/bin/env python3
"""Offline estimate of the input tokens of the review request on the gold corpus (#317).

Usage: uv run python review/scripts/eval_tokens.py --system FILE [--system FILE] \\
           [--policy-arm legacy|none] [--root DIR]
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import tempfile
from dataclasses import dataclass
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
    """The gateway's pre-send estimate of the request: every message at ``chars_per_token``."""
    counter = HeuristicTokenCounter(request.profile.chars_per_token)
    return sum(counter.count(message.content) for message in request.messages)


def measure(
    root: Path, system_prompt: Path, policy_arm: str, settings: LlmSettings
) -> PromptTokens:
    """Run the live capture's own path over the corpus with an in-process transport."""
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
    return PromptTokens(
        prompt_path=manifest["prompt_path"],
        prompt_version=manifest["prompt_version"],
        by_case={
            case_id: _estimate(request)
            for case_id, request in zip(cases, tap.requests, strict=True)
        },
    )


def format_table(
    result: PromptTokens, *, root: Path, policy_arm: str, settings: LlmSettings
) -> str:
    profile = settings.primary
    rows = [
        ["case", "A"],
        *([case_id, str(tokens)] for case_id, tokens in result.by_case.items()),
        ["total", str(result.total)],
    ]
    widths = [max(len(row[column]) for row in rows) for column in range(len(rows[0]))]
    return "\n".join(
        [
            f"eval_tokens: root {root}, policy arm {policy_arm}, model {profile.model}, "
            f"chars_per_token {profile.chars_per_token}",
            f"A: {result.prompt_path} ({result.prompt_version})",
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
    parser.add_argument("--system", type=Path, required=True, metavar="FILE", help="review prompt")
    parser.add_argument(
        "--policy-arm", choices=POLICY_ARMS, default="legacy", help="as in eval_live.py"
    )
    args = parser.parse_args(argv)
    settings = rehearsal_settings(DEFAULT_MODEL)
    result = measure(args.root, args.system, args.policy_arm, settings)
    print(format_table(result, root=args.root, policy_arm=args.policy_arm, settings=settings))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
