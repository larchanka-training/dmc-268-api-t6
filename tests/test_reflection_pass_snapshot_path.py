"""The verifier of a snapshot-path review (#130 F1-7).

- Its send budget is capped by the verifier route's window (R10): ``ReflectionPass`` reads
  it from a model that knows it and applies it to the fit, the pre-send guard and the
  mandatory-budget check that ``ExecuteReviewRun`` runs before the review call.
- It sees the L2-L4 excerpts the candidates cite as evidence, never the uncited ones
  (CVS §7.1, decided by the tech lead on 2026-10-25), within that window. The own diff lines
  of every candidate are kept first, then the cited excerpts, then the other diff lines.
  The excerpts are taken as the review fit keeps them (#144): L4, then each RAG place with
  the definitions it read, then L2 and L3.
- It sees the RAG blocks the candidates cite, each place with the definitions it read, and
  never the uncited ones (ADR 0001 §9, CVS §5.2, #144); the fit pays for them and its
  manifest lists exactly the ones it shows.
- The fit counts what the pre-send guard counts, so a fitted prompt never trips the guard.
- ``ReflectionPass`` reads its prompt file when it first verifies: a legacy attempt, which
  never verifies, does not need it.
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

import app.modules.reviews.application.reflection_pass as reflection_pass_module
from app.modules.reviews.application.context_provenance import SourceRole
from app.modules.reviews.application.execute_review import ExecuteReviewRun
from app.modules.reviews.application.policy_errors import PolicyError, PolicyErrorCode
from app.modules.reviews.application.policy_snapshot import CallManifest
from app.modules.reviews.application.prompt_builder import (
    ChangedFile,
    DiffLine,
    PullRequestMeta,
    RepoConventions,
    ReviewContext,
    file_block_id,
)
from app.modules.reviews.application.reflection_pass import (
    CandidateFindingItem,
    ReflectionPass,
    VerificationInvalidOutputError,
    VerificationOutput,
    VerificationRecord,
    VerificationUnavailableError,
    VerifierAnswerCheck,
    VerifierPrompt,
    VerifierPromptBuilder,
    VerifierReply,
)
from app.modules.reviews.application.review_context import (
    ContextBlock,
    ContextLevel,
    RetrievedBlock,
)
from app.modules.reviews.application.review_output import EvidenceRef, ReviewFindingV2
from app.modules.reviews.application.run_failures import RunCancelled, RunFailure
from tests.test_gateway_review_model import _Pipeline
from tests.test_review_context_blocks import (
    BASE,
    FIRST_PLACE,
    HEAD,
    L2_WINDOW,
    L3_FULL,
    L4_SIGNATURE,
    MAIN,
    META,
    OWN_DEFINITION,
    RETRIEVED_DEFINITION,
    SECOND_PLACE,
    SLOT,
    _block,
    _context,
    _snapshot,
)

CALL_ID = UUID("00000000-0000-0000-0000-000000017101")
MAIN_BLOCK = file_block_id(MAIN.path)
# The L2 excerpt as the verifier sees it (CVS §7.1).
L2_WINDOW_XML = (
    "<context_excerpts>\n"
    f'<excerpt block_id="L2:f1:3-4" level="L2" path="app/util.py" revision="{HEAD}" '
    'side="RIGHT" representation="exact_lines">\n'
    '<line n="3">def f():</line>\n'
    '<line n="4">    return 1</line>\n'
    "</excerpt>\n"
    "</context_excerpts>"
)


def _tokens(text: str) -> int:
    """counter/v2 (RFS §3.3): three characters per token, rounded up."""
    return (len(text) + 2) // 3


def _prompt_tokens(prompt: VerifierPrompt) -> int:
    """What the pre-send guard counts: the system and the user message, each on its own."""
    return _tokens(prompt.system) + _tokens(prompt.user)


def _candidate(
    candidate_id: str,
    *refs: EvidenceRef,
    path: str = MAIN.path,
    line: int = 10,
    start_line: int | None = None,
) -> CandidateFindingItem:
    return CandidateFindingItem(
        candidate_id=candidate_id,
        finding=ReviewFindingV2(
            path=path,
            line=line,
            start_line=start_line,
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
    return _prompt_tokens(prompt)


def _shown_lines(user: str) -> list[int]:
    code = user.split("<code_context>", 1)[1].split("</code_context>", 1)[0]
    files = code.split("<context_excerpts>", 1)[0]
    return [int(number) for number in re.findall(r'<line n="(\d+)" type=', files)]


def _shown_by_path(user: str) -> dict[str, list[int]]:
    code = user.split("<code_context>", 1)[1].split("<context_excerpts>", 1)[0]
    return {
        path: [int(number) for number in re.findall(r'<line n="(\d+)" type=', body)]
        for path, body in re.findall(
            r'<file block_id="[^"]+" path="([^"]+)"[^>]*>(.*?)</file>', code, re.S
        )
    }


# ---------- the cited excerpts ----------


def test_a_finding_backed_only_by_an_excerpt_is_verified_with_that_excerpt() -> None:
    context = _context((L2_WINDOW, L4_SIGNATURE, L3_FULL))

    prompt, manifest = _build((EXCERPT_ONLY,), context)

    assert L2_WINDOW_XML in prompt.user
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

    assert L2_WINDOW_XML in prompt.user
    assert manifest.get_block("L2:f1:3-4") is not None
    shown = _shown_lines(prompt.user)
    assert 10 in shown
    assert 1 < len(shown) < 300


BIG = _block(
    "L2:big:1-200",
    ContextLevel.L2,
    "app/big.py",
    ((1, 200),),
    "\n".join(f"big_value_{number} = {number}" for number in range(1, 201)),
)


def test_a_cited_excerpt_over_the_verifier_window_is_left_out() -> None:
    cited = _candidate("c-big", EvidenceRef(block_id="L2:big:1-200", start_line=1, end_line=2))
    context = _context((BIG,))
    window = _mandatory((cited,), context) + 400

    prompt, manifest = _build((cited,), context, max_prompt_tokens=window)

    assert "<context_excerpts>" not in prompt.user
    assert manifest.get_block("L2:big:1-200") is None
    assert 10 in _shown_lines(prompt.user)


def test_a_cited_excerpt_that_does_not_fit_does_not_stop_the_next_one() -> None:
    cited = _candidate(
        "c-both",
        EvidenceRef(block_id="L2:big:1-200", start_line=1, end_line=2),
        EvidenceRef(block_id="L2:f1:3-4", start_line=3, end_line=4),
    )
    context = _context((BIG, L2_WINDOW))
    window = _mandatory((cited,), context) + 400

    prompt, manifest = _build((cited,), context, max_prompt_tokens=window)

    assert '<excerpt block_id="L2:big:1-200"' not in prompt.user
    assert manifest.get_block("L2:big:1-200") is None
    assert L2_WINDOW_XML in prompt.user
    assert manifest.get_block("L2:f1:3-4") is not None


def test_the_own_lines_of_a_candidate_come_before_the_excerpt_it_cites() -> None:
    # The window holds the candidate's 30 lines or its 30-line excerpt, never both.
    wide = _block(
        "L2:wide:1-30",
        ContextLevel.L2,
        "app/wide.py",
        ((1, 30),),
        "\n".join(f"wide_value_{number} = {number}" for number in range(1, 31)),
    )
    cited = _candidate(
        "c-wide",
        EvidenceRef(block_id="L2:wide:1-30", start_line=1, end_line=30),
        start_line=10,
        line=39,
    )
    context = _context((wide,))
    whole, _ = _build((cited,), context)

    prompt, manifest = _build((cited,), context, max_prompt_tokens=_prompt_tokens(whole) - 1)

    assert _shown_lines(prompt.user) == list(range(10, 40))
    assert '<excerpt block_id="L2:wide:1-30"' not in prompt.user
    assert manifest.get_block("L2:wide:1-30") is None


def test_a_window_the_size_of_the_whole_prompt_keeps_it_whole() -> None:
    context = replace(_context((L2_WINDOW,)), changed_files=(_long_file(61),))
    whole, _ = _build((EXCERPT_ONLY,), context)
    size = _prompt_tokens(whole)

    at_size, _ = _build((EXCERPT_ONLY,), context, max_prompt_tokens=size)
    below, _ = _build((EXCERPT_ONLY,), context, max_prompt_tokens=size - 1)

    assert at_size == whole
    assert below != whole
    assert _prompt_tokens(below) <= size - 1


def _numbered(path: str, name: str) -> ChangedFile:
    return ChangedFile(
        path,
        "modified",
        tuple(DiffLine(number, "added", f"{name}_{number} = {number}") for number in range(1, 201)),
    )


# The reviewer's probe: two 200-line files, a candidate on the first line of each; the one on
# app/a.py cites the L2 excerpt. app/b.py sorts first; before the fix it filled the window
# and app/a.py was left out.
FILE_A = _numbered("app/a.py", "a")
FILE_B = _numbered("app/b.py", "b")
ON_A = _candidate(
    "c-a", EvidenceRef(block_id="L2:f1:3-4", start_line=3, end_line=4), path="app/a.py", line=1
)
ON_B = _candidate(
    "c-b",
    EvidenceRef(block_id=file_block_id("app/b.py"), start_line=1, end_line=1),
    path="app/b.py",
    line=1,
)
TWO_FILES = replace(_context((L2_WINDOW,)), changed_files=(FILE_A, FILE_B))


def test_every_candidate_keeps_its_own_lines_before_any_file_is_topped_up() -> None:
    window = _mandatory((ON_A, ON_B), TWO_FILES) + 400

    prompt, manifest = _build((ON_A, ON_B), TWO_FILES, max_prompt_tokens=window)

    shown = _shown_by_path(prompt.user)
    assert shown["app/a.py"][0] == 1
    assert shown["app/b.py"][0] == 1
    assert L2_WINDOW_XML in prompt.user
    assert manifest.get_block("L2:f1:3-4") is not None
    # The other lines top the window up, next to the kept excerpt and within the window.
    assert len(shown["app/a.py"]) + len(shown["app/b.py"]) > 2
    assert _prompt_tokens(prompt) <= window


class _Abstainer:
    """Knows its window; answers insufficient_context for every candidate it is shown."""

    def __init__(self, window: int) -> None:
        self.window = window
        self.prompts: list[VerifierPrompt] = []

    def max_prompt_tokens(self) -> int:
        return self.window

    async def verify(self, *, system_prompt: str, user_prompt: str, **_: object) -> VerifierReply:
        self.prompts.append(VerifierPrompt(system_prompt, user_prompt))
        ids = re.findall(r'candidate candidate_id="([^"]+)"', user_prompt)
        decisions = [
            {
                "candidate_id": candidate,
                "status": "insufficient_context",
                "reason": "Not enough code.",
                "evidence_refs": [],
                "requirement_refs": [],
            }
            for candidate in ids
        ]
        return VerifierReply(output={"decisions": decisions})


def test_a_fitted_prompt_never_trips_the_pre_send_guard() -> None:
    # 600 windows in a row, each far below the 400 diff lines: before, the fit left out the
    # newlines that join the user message and rounded the system and user tokens together,
    # so in some windows the guard refused the fitted prompt by 1-19 tokens.
    floor = _mandatory((ON_A, ON_B), TWO_FILES)
    overflows: list[int] = []
    for window in range(floor + 150, floor + 750):
        verifier = _Abstainer(window)
        try:
            asyncio.run(
                ReflectionPass(verifier).verify_recorded(candidates=(ON_A, ON_B), context=TWO_FILES)
            )
        except RunFailure as failure:
            assert failure.error_code == "verification_context_overflow"
            overflows.append(window)
            continue
        (sent,) = verifier.prompts
        assert _prompt_tokens(sent) <= window
        assert len(_shown_lines(sent.user)) < 400

    assert overflows == []


# ---------- cited RAG blocks fit as the review fit keeps them ----------


def _chars(*blocks: ContextBlock | RetrievedBlock) -> int:
    return sum(len(block.text) for block in blocks)


def _fit_excerpts(
    excerpts: tuple[ContextBlock, ...], retrieved: tuple[RetrievedBlock, ...], window: int
) -> tuple[tuple[ContextBlock, ...], tuple[RetrievedBlock, ...]]:
    """The cited excerpts and RAG blocks a window of ``window`` text characters holds."""

    def fits(
        files: tuple[ChangedFile, ...],
        shown: tuple[ContextBlock, ...],
        shown_retrieved: tuple[RetrievedBlock, ...],
    ) -> bool:
        return _chars(*shown, *shown_retrieved) <= window

    files, shown, shown_retrieved = reflection_pass_module._fit_verifier_code(
        (), excerpts, (), (), fits, retrieved=retrieved
    )
    assert files == ()
    return shown, shown_retrieved


def test_the_verifier_fit_keeps_l4_then_a_rag_place_then_l2() -> None:
    # Room for L4 and the place with its definition, or for L4 and L2: never for all three.
    window = _chars(L4_SIGNATURE, FIRST_PLACE, RETRIEVED_DEFINITION)
    assert _chars(L4_SIGNATURE, L2_WINDOW) <= window
    assert _chars(L4_SIGNATURE, L2_WINDOW, FIRST_PLACE, RETRIEVED_DEFINITION) > window

    shown = _fit_excerpts((L2_WINDOW, L4_SIGNATURE), (FIRST_PLACE, RETRIEVED_DEFINITION), window)

    assert shown == ((L4_SIGNATURE,), (FIRST_PLACE, RETRIEVED_DEFINITION))


def test_the_verifier_fit_shows_a_rag_place_with_its_definitions_or_not_at_all() -> None:
    # Room for the second place, not for its own definition.
    window = _chars(FIRST_PLACE, RETRIEVED_DEFINITION, SECOND_PLACE)

    shown = _fit_excerpts((), SLOT, window)

    assert shown == ((), (FIRST_PLACE, RETRIEVED_DEFINITION))


def test_a_shared_definition_comes_with_the_next_place_when_the_first_one_is_left_out() -> None:
    wide = replace(FIRST_PLACE, text="def caller():\n    return " + "x" * 200)
    window = _chars(RETRIEVED_DEFINITION, SECOND_PLACE, OWN_DEFINITION)
    assert _chars(wide, RETRIEVED_DEFINITION) > window

    shown = _fit_excerpts((), (wide, *SLOT[1:]), window)

    # In payload order: the definition stays before the place that brought it.
    assert shown == ((), (RETRIEVED_DEFINITION, SECOND_PLACE, OWN_DEFINITION))


# ---------- the cited RAG blocks ----------

# The slot the review prompt showed; its definitions are read at the base of the diff.
WITH_SLOT = replace(
    _context((L2_WINDOW,)),
    pr_meta=replace(META, diff_old_sha=BASE),
    retrieved=SLOT,
    retrieved_section=True,
)
ON_PLACE = _candidate(
    "c-place", EvidenceRef(block_id=FIRST_PLACE.block_id, start_line=3, end_line=4)
)
# The first place and the definition it read, as the verifier sees them.
FIRST_PLACE_XML = (
    "<retrieved_excerpts>\n"
    '<excerpt block_id="RAG:app/caller.py:3-4" level="RAG" path="app/caller.py" '
    f'revision="{HEAD}" side="RIGHT" representation="exact_lines">\n'
    '<line n="3">def caller():</line>\n'
    '<line n="4">    return RETRIEVED_CALLER_TEXT</line>\n'
    "</excerpt>\n"
    '<excerpt block_id="RAG:app/defs.py:1-1" level="RAG" path="app/defs.py" '
    f'revision="{BASE}" side="LEFT" representation="signature">\n'
    '<line n="1">def load(key):</line>\n'
    "</excerpt>\n"
    "</retrieved_excerpts>"
)


def test_a_cited_rag_place_is_shown_to_the_verifier_with_the_definitions_it_read() -> None:
    prompt, manifest = _build((EXCERPT_ONLY, ON_PLACE), WITH_SLOT)

    # After the cited L2-L4 excerpts, inside the code context.
    assert f"{L2_WINDOW_XML}\n{FIRST_PLACE_XML}\n</code_context>" in prompt.user
    for block in (FIRST_PLACE, RETRIEVED_DEFINITION):
        listed = manifest.get_block(block.block_id)
        assert listed is not None
        assert listed.source_role == SourceRole.RETRIEVED_EXCERPT
        assert (listed.path, listed.revision, listed.side) == (
            block.source.path,
            block.source.revision,
            block.source.side.value if block.source.side is not None else None,
        )
        assert listed.shown_ranges == tuple(
            (item.start_line, item.end_line) for item in block.source.ranges
        )
    for block in (SECOND_PLACE, OWN_DEFINITION):
        assert manifest.get_block(block.block_id) is None
        assert block.block_id not in prompt.user


def test_a_cited_definition_is_shown_without_the_places_that_read_it() -> None:
    cited = _candidate(
        "c-definition",
        EvidenceRef(block_id=RETRIEVED_DEFINITION.block_id, start_line=1, end_line=1),
    )

    prompt, manifest = _build((cited,), WITH_SLOT)

    shown = [
        block.block_id for block in manifest.blocks if block.source_role == "retrieved_excerpt"
    ]
    assert shown == [RETRIEVED_DEFINITION.block_id]
    assert prompt.user.count('level="RAG"') == 1


def test_uncited_rag_blocks_are_neither_shown_to_the_verifier_nor_listed_in_its_manifest() -> None:
    with_slot = _build((EXCERPT_ONLY, DIFF_ONLY), WITH_SLOT)
    without = _build((EXCERPT_ONLY, DIFF_ONLY), replace(WITH_SLOT, retrieved=()))

    assert with_slot == without
    assert "<retrieved_excerpts>" not in with_slot[0].user
    assert all(block.source_role != "retrieved_excerpt" for block in with_slot[1].blocks)


def test_without_a_retrieved_section_a_cited_rag_block_is_neither_shown_nor_listed() -> None:
    # The verifier shows again what the review prompt showed: a slot it did not show stays out.
    no_section = replace(WITH_SLOT, retrieved_section=False)

    prompt, manifest = _build((EXCERPT_ONLY, ON_PLACE), no_section)

    assert (prompt, manifest) == _build((EXCERPT_ONLY, ON_PLACE), replace(no_section, retrieved=()))
    assert "<retrieved_excerpts>" not in prompt.user and 'level="RAG"' not in prompt.user
    assert all(block.source_role != "retrieved_excerpt" for block in manifest.blocks)


@pytest.mark.parametrize(
    "ref",
    [
        EvidenceRef(block_id=FIRST_PLACE.block_id, start_line=3, end_line=4),
        EvidenceRef(block_id=RETRIEVED_DEFINITION.block_id, start_line=1, end_line=1),
    ],
    ids=["place", "definition"],
)
def test_the_verifier_may_cite_the_rag_blocks_it_is_shown(ref: EvidenceRef) -> None:
    # The pre-send guard and the evidence check both run on the verifier's own manifest.
    verifier = _Supporter(ref.block_id, ref.start_line, ref.end_line)

    record = asyncio.run(
        ReflectionPass(verifier).verify_recorded(candidates=(ON_PLACE,), context=WITH_SLOT)
    )

    assert [(decision.candidate_id, decision.status) for decision in record.decisions] == [
        ("c-place", "supported")
    ]
    assert FIRST_PLACE_XML in verifier.users[0]


def test_the_cited_rag_blocks_are_kept_before_the_uncited_diff_lines_and_paid_for() -> None:
    # The window holds the whole file and no RAG block: the place and its definition take
    # the room of uncited diff lines, never more.
    whole, _ = _build((ON_PLACE,), replace(WITH_SLOT, retrieved=()))
    window = _prompt_tokens(whole)

    prompt, manifest = _build((ON_PLACE,), WITH_SLOT, max_prompt_tokens=window)

    assert FIRST_PLACE_XML in prompt.user
    assert manifest.get_block(FIRST_PLACE.block_id) is not None
    assert _prompt_tokens(prompt) <= window
    shown = _shown_lines(prompt.user)
    assert 10 in shown
    assert len(shown) < len(MAIN.lines)


def test_a_cited_rag_group_over_the_verifier_window_is_left_out() -> None:
    # The window holds the candidate's own line, not the place with its definition.
    own_line = replace(
        WITH_SLOT,
        changed_files=(reflection_pass_module._with_lines(MAIN, (0,)),),
        retrieved=(),
    )
    alone = _build((ON_PLACE,), own_line)

    fitted = _build((ON_PLACE,), WITH_SLOT, max_prompt_tokens=_prompt_tokens(alone[0]))

    assert fitted == alone
    assert "<retrieved_excerpts>" not in fitted[0].user
    assert fitted[1].get_block(FIRST_PLACE.block_id) is None


ON_DEFINITION = _candidate(
    "c-definition",
    EvidenceRef(block_id=RETRIEVED_DEFINITION.block_id, start_line=1, end_line=1),
)


def test_a_cited_definition_is_offered_on_its_own_when_its_place_group_does_not_fit() -> None:
    # The window holds the own line and the cited definition, not the cited place with it:
    # the definition is a cited excerpt of its own, whole or not at all (CVS §5.2, §7.1).
    candidates = (ON_PLACE, ON_DEFINITION)
    own_line = replace(
        WITH_SLOT,
        changed_files=(reflection_pass_module._with_lines(MAIN, (0,)),),
        retrieved=(RETRIEVED_DEFINITION,),
    )
    alone = _build(candidates, own_line)
    window = _prompt_tokens(alone[0])
    with_place, _ = _build(candidates, replace(own_line, retrieved=SLOT[:2]))
    assert _prompt_tokens(with_place) > window

    fitted = _build(candidates, WITH_SLOT, max_prompt_tokens=window)

    assert fitted == alone
    prompt, manifest = fitted
    assert f'<excerpt block_id="{RETRIEVED_DEFINITION.block_id}"' in prompt.user
    assert f'<excerpt block_id="{FIRST_PLACE.block_id}"' not in prompt.user
    assert manifest.get_block(RETRIEVED_DEFINITION.block_id) is not None
    assert manifest.get_block(FIRST_PLACE.block_id) is None


def test_an_uncited_definition_is_not_offered_alone_when_its_place_group_does_not_fit() -> None:
    # Only the place is cited. The window holds the own line and the definition the place
    # read, one token short of the place with it: the definition is no cited excerpt of its
    # own, so the verifier is shown no RAG block (CVS §5.2, §7.1).
    own_line = replace(WITH_SLOT, changed_files=(reflection_pass_module._with_lines(MAIN, (0,)),))
    with_place, _ = _build((ON_PLACE,), own_line)
    assert FIRST_PLACE_XML in with_place.user
    window = _prompt_tokens(with_place) - 1

    fitted = _build((ON_PLACE,), own_line, max_prompt_tokens=window)

    assert fitted == _build((ON_PLACE,), replace(own_line, retrieved=()))
    prompt, manifest = fitted
    assert "<retrieved_excerpts>" not in prompt.user
    assert all(block.source_role != "retrieved_excerpt" for block in manifest.blocks)


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

    # counter/v2, three characters per token rounded up: the candidates render to 280 and 295
    # characters, the conventions to 184, the excerpt (without its container) to 225.
    assert {block.block_id: block.tokens for block in manifest.blocks} == {
        "policy-rules": 47,
        MAIN_BLOCK: 496,
        "L2:f1:3-4": 75,
        "repo-conventions": 62,
        "c-excerpt": 94,
        "c-diff": 99,
    }
    conventions_xml = (
        "<repo_conventions>\n<key_patterns>\n<pattern>Use services.</pattern>\n</key_patterns>\n"
        "<recommendations>\n<recommendation>Test paths.</recommendation>\n</recommendations>\n"
        "</repo_conventions>"
    )
    assert conventions_xml in prompt.user
    assert L2_WINDOW_XML in prompt.user


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


# ---------- the reference checks follow the accepted call's manifest ----------


class _AcceptedVerifier:
    """Answers with a ref to the diff block under the manifest ``accepted`` makes of the base;
    keeps the base manifest and the ``check`` it was handed."""

    def __init__(self, accepted: Any) -> None:
        self.accepted = accepted
        self.seen: list[tuple[CallManifest, VerifierAnswerCheck | None]] = []

    async def verify(
        self, *, manifest: CallManifest, check: VerifierAnswerCheck | None = None, **_: object
    ) -> VerifierReply:
        self.seen.append((manifest, check))
        decision = {
            "candidate_id": "c-diff",
            "status": "supported",
            "reason": "The constant is returned.",
            "evidence_refs": [{"block_id": MAIN_BLOCK, "start_line": 10, "end_line": 10}],
            "requirement_refs": [],
        }
        return VerifierReply(
            {"decisions": [decision]},
            accepted_call_id=CALL_ID,
            accepted_manifest=self.accepted(manifest),
        )


def _without_the_diff(manifest: CallManifest) -> CallManifest:
    # A fallback manifest that shows fewer blocks than the base one (CVS §5.2).
    return replace(
        manifest,
        recovery_kind="fallback",
        blocks=tuple(block for block in manifest.blocks if block.block_id != MAIN_BLOCK),
    )


def _verify_accepted(verifier: _AcceptedVerifier) -> VerificationRecord:
    return asyncio.run(
        ReflectionPass(verifier).verify_recorded(candidates=(DIFF_ONLY,), context=_context(()))
    )


def test_the_final_guard_checks_the_accepted_calls_manifest() -> None:
    # CVS §7.2: refs resolve only against the manifest of the accepted verifier call.
    with pytest.raises(VerificationInvalidOutputError) as raised:
        _verify_accepted(_AcceptedVerifier(_without_the_diff))

    assert str(raised.value) == f"Decision c-diff evidence_ref {MAIN_BLOCK} not found in manifest"


def test_the_model_gets_the_check_of_the_final_guard() -> None:
    verifier = _AcceptedVerifier(lambda manifest: None)

    record = _verify_accepted(verifier)

    # Without an accepted manifest the base one is checked, and the answer passes.
    assert [decision.status for decision in record.decisions] == ["supported"]
    [(base, check)] = verifier.seen
    assert check is not None
    output = VerificationOutput.model_validate(
        {
            "decisions": [
                {
                    "candidate_id": "c-diff",
                    "status": "supported",
                    "reason": "The constant is returned.",
                    # The diff block shows lines 10-39.
                    "evidence_refs": [{"block_id": MAIN_BLOCK, "start_line": 52, "end_line": 54}],
                    "requirement_refs": [],
                }
            ]
        }
    )
    assert check(output, base) == [
        f"Decision c-diff evidence_ref {MAIN_BLOCK}:52-54 is not shown in verifier manifest"
    ]
    assert check(output, _without_the_diff(base)) == [
        f"Decision c-diff evidence_ref {MAIN_BLOCK} not found in manifest"
    ]


def _the_bound_check() -> tuple[VerifierAnswerCheck, CallManifest]:
    verifier = _AcceptedVerifier(lambda manifest: None)
    _verify_accepted(verifier)
    [(base, check)] = verifier.seen
    assert check is not None
    return check, base


def _decisions_for(*candidate_ids: str) -> VerificationOutput:
    """An answer with one decision per id; ``model_copy`` skips the 64-character schema cap."""
    decision = VerificationOutput.model_validate(
        {
            "decisions": [
                {
                    "candidate_id": "c-diff",
                    "status": "supported",
                    "reason": "The constant is returned.",
                    "evidence_refs": [{"block_id": MAIN_BLOCK, "start_line": 10, "end_line": 10}],
                    "requirement_refs": [],
                }
            ]
        }
    ).decisions[0]
    return VerificationOutput.model_construct(
        decisions=[decision.model_copy(update={"candidate_id": c_id}) for c_id in candidate_ids]
    )


def test_an_unknown_candidate_id_is_named_cut_to_64_characters() -> None:
    check, base = _the_bound_check()
    unknown = "c-" + "9" * 98

    assert check(_decisions_for(unknown), base) == [
        f"Verifier returned decisions for 1 items, expected 1: missing c-diff; "
        f"unknown {unknown[:64]}"
    ]


def test_at_most_five_unknown_candidate_ids_are_named() -> None:
    check, base = _the_bound_check()

    assert check(_decisions_for(*(f"c-{n}" for n in range(7, 0, -1))), base) == [
        "Verifier returned decisions for 7 items, expected 1: missing c-diff; "
        "unknown c-1, c-2, c-3, c-4, c-5 (+2 more)"
    ]


# ---------- the prompt file is read when the pass first verifies ----------


class _NoPromptFiles:
    """``importlib.resources.files`` of a package whose prompt files are gone."""

    def joinpath(self, name: str) -> _NoPromptFiles:
        return self

    def read_text(self, encoding: str) -> str:
        raise FileNotFoundError("review.verifier.v2.md")


def test_a_reflection_pass_reads_its_prompt_file_only_when_it_verifies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(reflection_pass_module, "files", lambda package: _NoPromptFiles())

    reflection = ReflectionPass(_Raising(AssertionError("the verifier must not be called")))

    assert asyncio.run(
        reflection.verify_recorded(candidates=(), context=_context(()))
    ) == VerificationRecord(decisions=(), candidate_ids=())
    with pytest.raises(PolicyError) as raised:
        _verify_with(reflection)
    assert raised.value.code == PolicyErrorCode.SNAPSHOT_INTEGRITY_ERROR
    assert raised.value.message.startswith(
        "Unable to read verifier prompt resource 'review.verifier.v2.md'"
    )


def _verify_with(reflection: ReflectionPass) -> None:
    asyncio.run(reflection.verify_recorded(candidates=(DIFF_ONLY,), context=_context(())))


def test_each_block_of_the_verifier_user_message_starts_on_its_own_line() -> None:
    prompt, _ = _build((DIFF_ONLY,), _context((L2_WINDOW,)))

    blocks = prompt.user.split("\n<code_context>\n")
    assert len(blocks) == 2
    assert blocks[0].endswith(">")
    assert '\n</code_context>\n<candidate_findings count="1">\n' in blocks[1]
