"""The verifier of a snapshot-path review (#130 F1-7).

- Its send budget is capped by the verifier route's window (R10): ``ReflectionPass`` reads
  it from a model that knows it and applies it to the fit, the pre-send guard and the
  mandatory-budget check that ``ExecuteReviewRun`` runs before the review call.
- It sees the L2-L4 excerpts the candidates cite as evidence, never the uncited ones
  (CVS §7.1, decided by the tech lead on 2026-10-25), within that window. The candidates'
  own diff lines are kept first, then the cited excerpts, then the other diff lines.
- Every block of its manifest carries the tokens of its rendered text (R19).
- A run failure that is not a provider error (a manifest write that failed:
  ``internal_error``) and a cancellation keep their outcome; provider errors keep their
  ``verification_*`` mapping.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import replace
from typing import Any
from uuid import UUID

import pytest

from app.modules.reviews.application.context_provenance import SourceRole
from app.modules.reviews.application.execute_review import ExecuteReviewRun
from app.modules.reviews.application.policy_errors import PolicyError, PolicyErrorCode
from app.modules.reviews.application.prompt_budget import token_counter_for
from app.modules.reviews.application.prompt_builder import (
    ChangedFile,
    DiffLine,
    PromptBuilder,
    PullRequestMeta,
    RepoConventions,
    ReviewContext,
    file_block_id,
    render_context_excerpts,
)
from app.modules.reviews.application.reflection_pass import (
    CandidateFindingItem,
    ReflectionPass,
    VerificationUnavailableError,
    VerifierPrompt,
    VerifierPromptBuilder,
    VerifierReply,
)
from app.modules.reviews.application.review_context import ContextLevel
from app.modules.reviews.application.review_output import EvidenceRef, ReviewFindingV2
from app.modules.reviews.application.run_failures import RunCancelled, RunFailure
from tests.test_gateway_review_model import _Pipeline
from tests.test_review_context_blocks import (
    HEAD,
    L2_WINDOW,
    L3_FULL,
    L4_SIGNATURE,
    MAIN,
    _block,
    _context,
    _snapshot,
)

CALL_ID = UUID("00000000-0000-0000-0000-000000017101")
MAIN_BLOCK = file_block_id(MAIN.path)
COUNTER = token_counter_for("counter/v2")


def _candidate(candidate_id: str, *refs: EvidenceRef, line: int = 10) -> CandidateFindingItem:
    return CandidateFindingItem(
        candidate_id=candidate_id,
        finding=ReviewFindingV2(
            path=MAIN.path,
            line=line,
            start_line=None,
            severity="high",
            category="correctness",
            title="Wrong value",
            body="The helper returns a constant.",
            suggestion=None,
            confidence=0.9,
            rule_name=None,
            evidence_refs=list(refs),
        ),
    )


EXCERPT_ONLY = _candidate("c-excerpt", EvidenceRef(block_id="L2:f1:3-4", start_line=3, end_line=4))
DIFF_ONLY = _candidate("c-diff", EvidenceRef(block_id=MAIN_BLOCK, start_line=10, end_line=10))


def _build(
    candidates: tuple[CandidateFindingItem, ...],
    context: ReviewContext,
    *,
    max_prompt_tokens: int | None = None,
) -> tuple[VerifierPrompt, Any]:
    return VerifierPromptBuilder().build_verifier_prompt(
        candidates=candidates,
        context=context,
        call_id=CALL_ID,
        max_prompt_tokens=max_prompt_tokens,
    )


def _mandatory(candidates: tuple[CandidateFindingItem, ...], context: ReviewContext) -> int:
    """The tokens of the verifier prompt without any code: the room the code is fitted to."""
    prompt, _ = _build(candidates, replace(context, changed_files=(), context_blocks=()))
    return COUNTER.count(prompt.system) + COUNTER.count(prompt.user)


def _shown_lines(user: str) -> list[int]:
    code = user.split("<code_context>", 1)[1].split("</code_context>", 1)[0]
    files = code.split("<context_excerpts>", 1)[0]
    return [int(number) for number in re.findall(r'<line n="(\d+)" type=', files)]


# ---------- the cited excerpts ----------


def test_a_finding_backed_only_by_an_excerpt_is_verified_with_that_excerpt() -> None:
    context = _context((L2_WINDOW, L4_SIGNATURE, L3_FULL))

    prompt, manifest = _build((EXCERPT_ONLY,), context)

    assert render_context_excerpts((L2_WINDOW,)) in prompt.user
    assert prompt.user.index("</changed_files>") < prompt.user.index("<context_excerpts>")
    assert prompt.user.index("</context_excerpts>") < prompt.user.index("</code_context>")
    block = manifest.get_block("L2:f1:3-4")
    assert block is not None
    assert block.source_role == SourceRole.SOURCE_EXCERPT
    assert (block.path, block.revision, block.side) == ("app/util.py", HEAD, "RIGHT")
    assert block.shown_ranges == ((3, 4),)
    assert manifest.get_block("L4:sym-1") is None
    assert manifest.get_block("L3:f2:1-3") is None
    assert "L4:sym-1" not in prompt.user and "L3:f2:1-3" not in prompt.user


class _Supporter:
    """Supports every candidate it is shown, citing ``block_id`` lines ``start``-``end``."""

    def __init__(self, block_id: str, start: int, end: int) -> None:
        self.ref = {"block_id": block_id, "start_line": start, "end_line": end}
        self.users: list[str] = []

    async def verify(self, *, system_prompt: str, user_prompt: str, **_: object) -> VerifierReply:
        self.users.append(user_prompt)
        ids = re.findall(r'candidate candidate_id="([^"]+)"', user_prompt)
        decisions = [
            {
                "candidate_id": candidate,
                "status": "supported",
                "reason": "The excerpt shows it.",
                "evidence_refs": [self.ref],
                "requirement_refs": [],
            }
            for candidate in ids
        ]
        return VerifierReply(output={"decisions": decisions})


def test_the_verifier_may_cite_the_excerpt_the_candidate_cites() -> None:
    verifier = _Supporter("L2:f1:3-4", 3, 4)

    record = asyncio.run(
        ReflectionPass(verifier).verify_recorded(
            candidates=(EXCERPT_ONLY,), context=_context((L2_WINDOW, L4_SIGNATURE))
        )
    )

    assert [(decision.candidate_id, decision.status) for decision in record.decisions] == [
        ("c-excerpt", "supported")
    ]
    assert '<excerpt block_id="L2:f1:3-4"' in verifier.users[0]


def test_an_uncited_excerpt_is_not_sent_to_the_verifier() -> None:
    with_blocks = _build((DIFF_ONLY,), _context((L2_WINDOW, L4_SIGNATURE, L3_FULL)))
    without = _build((DIFF_ONLY,), _context(()))

    assert with_blocks == without
    assert "<context_excerpts>" not in with_blocks[0].user


def _long_file(lines: int) -> ChangedFile:
    return ChangedFile(
        MAIN.path,
        "modified",
        tuple(
            DiffLine(number, "added", f"value_{number} = {number}") for number in range(1, lines)
        ),
    )


def test_the_cited_excerpt_is_kept_before_the_uncited_diff_lines() -> None:
    # The window holds the candidate's line and the excerpt, not the 300-line file.
    context = replace(_context((L2_WINDOW,)), changed_files=(_long_file(301),))
    window = _mandatory((EXCERPT_ONLY,), context) + 400

    prompt, manifest = _build((EXCERPT_ONLY,), context, max_prompt_tokens=window)

    assert render_context_excerpts((L2_WINDOW,)) in prompt.user
    assert manifest.get_block("L2:f1:3-4") is not None
    shown = _shown_lines(prompt.user)
    assert 10 in shown
    assert 1 < len(shown) < 300


def test_a_cited_excerpt_over_the_verifier_window_is_left_out() -> None:
    big = _block(
        "L2:big:1-200",
        ContextLevel.L2,
        "app/big.py",
        ((1, 200),),
        "\n".join(f"big_value_{number} = {number}" for number in range(1, 201)),
    )
    cited = _candidate("c-big", EvidenceRef(block_id="L2:big:1-200", start_line=1, end_line=2))
    context = _context((big,))
    window = _mandatory((cited,), context) + 400

    prompt, manifest = _build((cited,), context, max_prompt_tokens=window)

    assert "<context_excerpts>" not in prompt.user
    assert manifest.get_block("L2:big:1-200") is None
    assert 10 in _shown_lines(prompt.user)


# ---------- the verifier route window ----------


class _WindowVerifier:
    """A verifier model that knows its route window and must not be called."""

    def __init__(self, window: int) -> None:
        self.window = window
        self.calls = 0

    def max_prompt_tokens(self) -> int:
        return self.window

    async def verify(self, **_: object) -> VerifierReply:
        self.calls += 1
        raise AssertionError("the verifier must not be called")


def test_the_fit_is_capped_by_the_verifier_window() -> None:
    with pytest.raises(RunFailure) as raised:
        _build((DIFF_ONLY,), _context(()), max_prompt_tokens=100)

    assert raised.value.error_code == "verification_context_overflow"
    assert raised.value.message.endswith("limit is 100")


def test_the_reflection_pass_fits_to_the_window_of_its_model() -> None:
    verifier = _WindowVerifier(100)

    with pytest.raises(RunFailure) as raised:
        asyncio.run(
            ReflectionPass(verifier).verify_recorded(candidates=(DIFF_ONLY,), context=_context(()))
        )

    assert raised.value.error_code == "verification_context_overflow"
    assert raised.value.message.endswith("limit is 100")
    assert verifier.calls == 0


def test_the_pre_send_guard_is_capped_by_the_verifier_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}
    real = VerifierPromptBuilder.build_verifier_prompt

    def oversized(self: VerifierPromptBuilder, **kwargs: Any) -> tuple[VerifierPrompt, Any]:
        # The fit under a large window, then a user message over the model's window.
        seen["window"] = kwargs.pop("max_prompt_tokens")
        prompt, manifest = real(self, **kwargs)
        return VerifierPrompt(prompt.system, prompt.user + "x" * 3_000), manifest

    monkeypatch.setattr(VerifierPromptBuilder, "build_verifier_prompt", oversized)
    window = _mandatory((DIFF_ONLY,), _context(())) + 300
    verifier = _WindowVerifier(window)

    with pytest.raises(RunFailure) as raised:
        asyncio.run(
            ReflectionPass(verifier).verify_recorded(candidates=(DIFF_ONLY,), context=_context(()))
        )

    assert seen["window"] == window
    assert raised.value.error_code == "verification_context_overflow"
    assert raised.value.message.startswith("Verifier prompt is ")
    assert raised.value.message.endswith(f"limit is {window}")
    assert verifier.calls == 0


class _NoReview:
    def __init__(self) -> None:
        self.calls = 0

    async def get_pull_request_meta(self, run_id: UUID) -> PullRequestMeta:
        return PullRequestMeta("PR", None, "u", "b", "m", (), 1, 1, 0, False, False)

    async def draft_review(self, *, context: ReviewContext) -> dict[str, object]:
        self.calls += 1
        raise AssertionError("the review must not be called")


def test_the_mandatory_verifier_budget_uses_the_window_before_the_review_call() -> None:
    pipeline = _Pipeline((MAIN,), _snapshot())
    model = _NoReview()
    review = ExecuteReviewRun(
        pipeline, pipeline, model, pipeline, reflection_pass=ReflectionPass(_WindowVerifier(100))
    )

    with pytest.raises(PolicyError) as raised:
        asyncio.run(review.execute(UUID("00000000-0000-0000-0000-000000017102")))

    assert raised.value.code == PolicyErrorCode.PROMPT_BUDGET_EXCEEDED
    assert raised.value.limit == 100
    assert model.calls == 0


# ---------- the manifest counts its blocks ----------


def test_every_block_of_the_verifier_manifest_carries_its_tokens() -> None:
    conventions = RepoConventions(("Use services.",), ("Test paths.",))
    context = replace(_context((L2_WINDOW,)), conventions=conventions)

    prompt, manifest = _build((EXCERPT_ONLY, DIFF_ONLY), context)

    tokens = {block.block_id: block.tokens for block in manifest.blocks}
    assert None not in tokens.values()
    candidates = re.findall(r"<candidate candidate_id=.*?</candidate>", prompt.user, re.S)
    assert len(candidates) == 2
    assert tokens["c-excerpt"] == COUNTER.count(candidates[0])
    assert tokens["c-diff"] == COUNTER.count(candidates[1])
    assert tokens["repo-conventions"] == COUNTER.count(
        PromptBuilder._render_conventions(conventions)
    )
    assert tokens["L2:f1:3-4"] == COUNTER.count(
        render_context_excerpts((L2_WINDOW,)).split("\n", 1)[1].rsplit("\n", 1)[0]
    )


# ---------- failures that are not provider errors ----------


class _Raising:
    def __init__(self, error: Exception) -> None:
        self.error = error

    async def verify(self, **_: object) -> VerifierReply:
        raise self.error


def _verify(error: Exception) -> None:
    asyncio.run(
        ReflectionPass(_Raising(error)).verify_recorded(
            candidates=(DIFF_ONLY,), context=_context(())
        )
    )


def test_a_failed_manifest_write_keeps_internal_error() -> None:
    with pytest.raises(RunFailure) as raised:
        _verify(RunFailure("internal_error", "the context manifest could not be stored"))

    assert raised.value.error_code == "internal_error"
    assert raised.value.message == "the context manifest could not be stored"


def test_a_cancellation_during_the_verifier_call_stays_a_cancellation() -> None:
    with pytest.raises(RunCancelled) as raised:
        _verify(RunCancelled("pr_closed"))

    assert str(raised.value) == "pr_closed"


def test_a_provider_timeout_still_maps_to_verification_unavailable() -> None:
    with pytest.raises(VerificationUnavailableError):
        _verify(RunFailure("llm_timeout", "slow"))
