"""The L2-L4 context of the payload in the snapshot-path review prompt (#130 F1-4: R2, P6-4).

The stage builds L1-L4 (CONTEXT_AND_VERIFICATION_SPEC §3-4); the review prompt shows L1 as
the changed files and, on the snapshot path, every L2-L4 block as a numbered excerpt that
names its block, path, revision, side and level. The fit drops excerpts in the reverse of
the selection order of CVS §4.1 (L1 → L4 → RAG → L2 → L3): L3, then L2, then L4, before it
trims a changed file. An ignored file is counted, never named (RULES_FORMAT_SPEC §5.3).
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from hashlib import sha256
from typing import Any
from uuid import UUID

import pytest

from app.modules.code_index.domain.enums import EdgeKind
from app.modules.reviews.application import rag_index
from app.modules.reviews.application.context_provenance import LineRange, SourceRef, SourceRole
from app.modules.reviews.application.conventions import CachedConventions, GeneratedConventions
from app.modules.reviews.application.diff_map import DiffSide
from app.modules.reviews.application.execute_review import ExecuteReviewRun, ReviewPromptInput
from app.modules.reviews.application.policy_snapshot import (
    POLICY_SNAPSHOT_SCHEMA_VERSION,
    ManifestBlock,
    PolicySnapshot,
)
from app.modules.reviews.application.prompt_budget import (
    EstimatedTokenCounter,
    fit_review_context,
    prompt_tokens,
)
from app.modules.reviews.application.prompt_builder import (
    ChangedFile,
    DiffLine,
    PromptBuilder,
    PullRequestMeta,
    RepoConventions,
    ReviewContext,
)
from app.modules.reviews.application.reflection_pass import ReflectionPass
from app.modules.reviews.application.review_context import (
    ContextBlock,
    ContextLevel,
    ContextStatus,
    Representation,
    RetrievedBlock,
    ReviewContextPayload,
    snapshot_block,
)
from app.modules.reviews.application.review_output import EvidenceRef
from app.modules.reviews.application.review_path import ReviewBinding
from tests.policy_snapshot_fakes import (
    make_complete_execution_profile,
    make_complete_prompt_bindings,
    make_policy_document_missing,
    make_policy_document_present,
)
from tests.test_review_context_stage import _HEAD as STAGE_HEAD
from tests.test_review_context_stage import _RUN as STAGE_RUN
from tests.test_review_context_stage import Trace as StageTrace
from tests.test_review_context_stage import _stage as _context_stage

RUN_ID = UUID("00000000-0000-0000-0000-000000000300")
REPOSITORY_ID = UUID("00000000-0000-0000-0000-000000000301")
V4_REVIEW_ID = UUID("00000000-0000-0000-0000-000000000341")
HEAD = "a" * 40

META = PullRequestMeta(
    "Title", None, "author", "feature", "main", (), 1, 1, 0, False, False, head_sha=HEAD
)


def _block(
    block_id: str,
    level: ContextLevel,
    path: str,
    ranges: tuple[tuple[int, int], ...],
    text: str,
    *,
    role: SourceRole = SourceRole.SOURCE_EXCERPT,
    representation: Representation = Representation.EXACT_LINES,
) -> ContextBlock:
    return ContextBlock(
        block_id=block_id,
        level=level,
        file_id=None,
        source=SourceRef(
            role=role,
            repository_id=REPOSITORY_ID,
            revision=HEAD,
            path=path,
            blob_sha=None,
            origin_paths=(),
            side=DiffSide.RIGHT,
            ranges=tuple(LineRange(start, end) for start, end in ranges),
            reason="window",
        ),
        representation=representation,
        text=text,
        # The stage's own count (4 characters per token until R19): never the fit's.
        tokens=99_999,
        truncated=False,
    )


L2_WINDOW = _block("L2:f1:3-4", ContextLevel.L2, "app/util.py", ((3, 4),), "def f():\n    return 1")
L4_SIGNATURE = _block(
    "L4:sym-1",
    ContextLevel.L4,
    "app/models.py",
    ((1, 1), (5, 6)),
    "class User:\n    def save(self) -> None:\n        ...",
    role=SourceRole.AST_EXCERPT,
    representation=Representation.SIGNATURE,
)
L3_FULL = _block("L3:f2:1-3", ContextLevel.L3, "app/full.py", ((1, 3),), "a = 1\nb = 2\nc = 3")
L2_SECOND = _block("L2:f3:7-7", ContextLevel.L2, "app/other.py", ((7, 7),), "return value")
L1_HUNK = _block(
    "L1:h1",
    ContextLevel.L1,
    "app/main.py",
    ((10, 10),),
    "+x = 1",
    role=SourceRole.DIFF,
)

MAIN = ChangedFile(
    "app/main.py",
    "modified",
    tuple(DiffLine(number, "added", f"line_{number} = {number}") for number in range(10, 40)),
)


def _snapshot(review_system_version: str = "v4") -> PolicySnapshot:
    return PolicySnapshot(
        schema_version=POLICY_SNAPSHOT_SCHEMA_VERSION,
        snapshot_id=UUID("00000000-0000-0000-0000-000000000303"),
        run_id=RUN_ID,
        repository_id=REPOSITORY_ID,
        policy_base_sha="b" * 40,
        rules=make_policy_document_present(path=".review/rules.md", text="# Rules\nBe kind.\n"),
        agents=make_policy_document_missing("AGENTS.md"),
        ignore_patterns=(),
        ignore_digest="0" * 64,
        parser_profile="rules-markdown/v1",
        parser_version="4.0.0",
        matcher_version="review-ignore/v1",
        limits_version="limits/v1",
        builtin_exclusions_version="builtin-exclusions/v1",
        priority_matcher_version="priority/v1",
        service_defaults_rule_version_id=UUID("00000000-0000-0000-0000-000000000304"),
        defaults_checksum="default-backend.v1",
        prompt_bindings=replace(
            make_complete_prompt_bindings(review_system_id=V4_REVIEW_ID),
            review_system_version=review_system_version,
        ),
        execution_profile=make_complete_execution_profile("fast"),
        policy_digest="d" * 64,
    )


def _context(blocks: tuple[ContextBlock, ...], *, excluded_files: int = 0) -> ReviewContext:
    return ReviewContext(
        system="Review carefully.",
        rules=(),
        agents_md=None,
        conventions=RepoConventions((), ()),
        pr_meta=META,
        changed_files=(MAIN,),
        omitted_files=("app/too_big.py",),
        policy_snapshot=_snapshot(),
        context_blocks=blocks,
        excluded_files=excluded_files,
    )


def test_excerpts_carry_block_id_level_path_revision_and_side_after_the_changed_files() -> None:
    user = PromptBuilder().build_prompt(_context((L2_WINDOW, L4_SIGNATURE))).user

    excerpts = (
        "<context_excerpts>\n"
        f'<excerpt block_id="L2:f1:3-4" level="L2" path="app/util.py" revision="{HEAD}" '
        'side="RIGHT" representation="exact_lines">\n'
        '<line n="3">def f():</line>\n'
        '<line n="4">    return 1</line>\n'
        "</excerpt>\n"
        f'<excerpt block_id="L4:sym-1" level="L4" path="app/models.py" revision="{HEAD}" '
        'side="RIGHT" representation="signature">\n'
        '<line n="1">class User:</line>\n'
        '<line n="5">    def save(self) -&gt; None:</line>\n'
        '<line n="6">        ...</line>\n'
        "</excerpt>\n"
        "</context_excerpts>"
    )
    assert f"</changed_files>\n{excerpts}\n<omitted_files>\napp/too_big.py\n</omitted_files>" in (
        user
    )
    assert user.endswith("</omitted_files>")


def test_manifest_names_each_excerpt_by_role_with_its_shown_ranges() -> None:
    context = _context((L2_WINDOW, L4_SIGNATURE))

    manifest = PromptBuilder().build_manifest(context, call_id=RUN_ID)

    assert manifest.blocks[-2:] == (
        ManifestBlock(
            block_id="L2:f1:3-4",
            source_role=SourceRole.SOURCE_EXCERPT,
            source_kind="source_code",
            path="app/util.py",
            revision=HEAD,
            side="RIGHT",
            start_line=3,
            end_line=4,
            shown_line_count=2,
            shown_ranges=((3, 4),),
            repository_id=REPOSITORY_ID,
        ),
        ManifestBlock(
            block_id="L4:sym-1",
            source_role=SourceRole.AST_EXCERPT,
            source_kind="source_code",
            path="app/models.py",
            revision=HEAD,
            side="RIGHT",
            start_line=1,
            end_line=6,
            shown_line_count=3,
            shown_ranges=((1, 1), (5, 6)),
            repository_id=REPOSITORY_ID,
        ),
    )
    assert manifest.is_range_shown("L4:sym-1", 5, 6)
    assert not manifest.is_range_shown("L4:sym-1", 2, 4)


def test_excluded_files_are_one_counter_line_without_a_path() -> None:
    user = PromptBuilder().build_prompt(_context((), excluded_files=2)).user

    assert user.endswith('</omitted_files>\n<excluded_files count="2" />')
    assert "<context_excerpts>" not in user
    assert "<excluded_files" not in PromptBuilder().build_prompt(_context(())).user


class _CountOnly:
    """The same counting as ``EstimatedTokenCounter`` without the length-aware fast path."""

    def count(self, text: str) -> int:
        return EstimatedTokenCounter().count(text)


ORDERED = (L4_SIGNATURE, L2_WINDOW, L3_FULL, L2_SECOND)


@pytest.mark.parametrize(
    ("budget_of", "kept"),
    [
        ((L4_SIGNATURE, L2_WINDOW, L2_SECOND), ("L4:sym-1", "L2:f1:3-4", "L2:f3:7-7")),
        ((L4_SIGNATURE, L2_WINDOW), ("L4:sym-1", "L2:f1:3-4")),
        ((L4_SIGNATURE,), ("L4:sym-1",)),
        ((), ()),
    ],
    ids=["l3-first", "then-l2-from-the-end", "then-the-other-l2", "l4-last"],
)
@pytest.mark.parametrize("counter", [EstimatedTokenCounter(), _CountOnly()], ids=["fast", "plain"])
def test_fit_drops_l3_then_l2_then_l4_before_any_changed_file(
    budget_of: tuple[ContextBlock, ...], kept: tuple[str, ...], counter: Any
) -> None:
    budget = prompt_tokens(_context(budget_of), EstimatedTokenCounter())

    fitted = fit_review_context(_context(ORDERED), max_prompt_tokens=budget, counter=counter)

    assert tuple(block.block_id for block in fitted.context_blocks) == kept
    assert fitted.changed_files == (MAIN,)
    assert fitted.omitted_files == ("app/too_big.py",)


@pytest.mark.parametrize("counter", [EstimatedTokenCounter(), _CountOnly()], ids=["fast", "plain"])
def test_fit_trims_changed_files_only_without_any_excerpt(counter: Any) -> None:
    budget = prompt_tokens(_context(()), EstimatedTokenCounter()) - 1

    fitted = fit_review_context(_context(ORDERED), max_prompt_tokens=budget, counter=counter)

    assert fitted.context_blocks == ()
    [main] = fitted.changed_files
    assert main.total_lines == 30 and len(main.lines) < 30
    assert prompt_tokens(fitted, EstimatedTokenCounter()) <= budget


def test_fit_with_both_counters_gives_the_same_cut() -> None:
    budget = prompt_tokens(_context(()), EstimatedTokenCounter()) - 40

    fast = fit_review_context(
        _context(ORDERED), max_prompt_tokens=budget, counter=EstimatedTokenCounter()
    )
    plain = fit_review_context(_context(ORDERED), max_prompt_tokens=budget, counter=_CountOnly())

    assert fast == plain


def test_fit_keeps_every_excerpt_that_fits() -> None:
    context = _context(ORDERED)
    budget = prompt_tokens(context, EstimatedTokenCounter())

    assert fit_review_context(context, max_prompt_tokens=budget, counter=_CountOnly()) is context


# The execute_review side: what the stage hands over reaches the snapshot-path prompt only.
RETRIEVED = RetrievedBlock(
    block_id="RAG:app/caller.py:2-3",
    level=ContextLevel.RAG,
    source=replace(L2_WINDOW.source, role=SourceRole.RETRIEVED_EXCERPT, path="app/caller.py"),
    representation=Representation.EXACT_LINES,
    text="RETRIEVED_CALLER_TEXT",
    tokens=5,
    edge_kind=EdgeKind.USES_IMPORTED,
    index_commit_sha="c" * 40,
)


def _payload(*, ignored_files: int = 2) -> ReviewContextPayload:
    return ReviewContextPayload(
        status=ContextStatus.REVIEW,
        reason=None,
        policy_snapshot_id=UUID("00000000-0000-0000-0000-000000000303"),
        policy_digest="d" * 64,
        diff_digest="e" * 64,
        blocks=(L1_HUNK, L4_SIGNATURE, L2_WINDOW),
        omissions=(),
        imports=(),
        levels_present=(ContextLevel.L1, ContextLevel.L2, ContextLevel.L4, ContextLevel.RAG),
        reviewable_lines=1,
        ignored_files=ignored_files,
        tokens_used=0,
        retrieved=(RETRIEVED,),
    )


class _Stage:
    """The context stage as execute_review sees it."""

    diff_old_sha: str | None = None
    head_sha: str | None = None

    def __init__(self, snapshot: PolicySnapshot | None, payload: ReviewContextPayload | None):
        self.snapshot_for_llm = snapshot
        self.payload = payload

    def excluded(self, path: str) -> bool:
        return path.startswith("secret/")


class _Verifier:
    async def verify(self, **_: object) -> dict[str, object]:
        return {"decisions": []}


class _Pipeline:
    def __init__(self, version: str) -> None:
        self.version = version
        self.contexts: list[ReviewContext] = []

    async def prepare(self, run_id: UUID) -> GeneratedConventions:
        cached = CachedConventions(REPOSITORY_ID, None, RUN_ID, (), (), {})
        return GeneratedConventions(cached, None, True)

    async def get_review_prompt_input(
        self, run_id: UUID, saved: GeneratedConventions
    ) -> ReviewPromptInput:
        return ReviewPromptInput(
            system="Review carefully.",
            rules=(),
            agents_md=None,
            conventions=RepoConventions((), ()),
            changed_files=(
                MAIN,
                ChangedFile("secret/key.py", "added", (DiffLine(1, "added", "KEY = 1"),)),
            ),
            omitted_files=("secret/huge.py", "app/too_big.py"),
            review_binding=ReviewBinding(V4_REVIEW_ID, "review.system", self.version, "github"),
        )

    async def get_pull_request_meta(self, run_id: UUID) -> PullRequestMeta:
        return META

    async def draft_review(self, *, context: ReviewContext) -> dict[str, Any]:
        self.contexts.append(context)
        return {"findings": [], "summary": {"problem": "p", "done_well": "d", "effort": "none"}}

    async def execute(self, run_id: UUID, raw_output: object) -> bool:
        return True


def _review(version: str, payload: ReviewContextPayload | None) -> ReviewContext:
    pipeline = _Pipeline(version)
    snapshot = _snapshot(version)
    run = ExecuteReviewRun(
        pipeline,
        pipeline,
        pipeline,
        pipeline,
        context_filter=_Stage(snapshot, payload),
        reflection_pass=ReflectionPass(_Verifier()),
    )
    assert asyncio.run(run.execute(RUN_ID)) is True
    [context] = pipeline.contexts
    return context


def test_snapshot_review_shows_the_l2_l4_excerpts_and_counts_the_ignored_files() -> None:
    context = _review("v4", _payload())

    l4 = replace(L4_SIGNATURE, block_id=_bounded_l4_id(L4_SIGNATURE))
    assert context.context_blocks == (l4, L2_WINDOW)
    assert context.excluded_files == 2
    user = PromptBuilder().build_prompt(context).user
    assert f'<excerpt block_id="{l4.block_id}" level="L4"' in user
    assert '<excerpt block_id="L2:f1:3-4" level="L2"' in user
    # L1 is the changed files, never a second time as an excerpt.
    assert 'block_id="L1:h1"' not in user
    assert user.endswith(
        '<omitted_files>\napp/too_big.py\n</omitted_files>\n<excluded_files count="2" />'
    )
    assert "secret/" not in user


def test_retrieved_blocks_are_neither_kept_nor_shown_without_a_retrieved_section() -> None:
    context = _review("v4", _payload())

    assert not rag_index.RETRIEVED_PROMPT_VERSIONS
    assert context.retrieved == ()
    assert "RETRIEVED_CALLER_TEXT" not in PromptBuilder().build_prompt(context).user


def test_retrieved_blocks_reach_the_context_of_a_prompt_with_a_retrieved_section(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        rag_index, "RETRIEVED_PROMPT_VERSIONS", frozenset({("review.system", "v4")})
    )

    context = _review("v4", _payload())

    assert context.retrieved == (RETRIEVED,)
    # #144 renders them; F1 only fills the slot.
    assert "RETRIEVED_CALLER_TEXT" not in PromptBuilder().build_prompt(context).user


def test_snapshot_review_after_a_failed_build_shows_no_excerpt_and_no_counter() -> None:
    context = _review("v4", None)

    assert context.context_blocks == () and context.excluded_files == 0
    user = PromptBuilder().build_prompt(context).user
    assert "<context_excerpts>" not in user and "<excluded_files" not in user
    assert "secret/" not in user


def test_legacy_review_gets_neither_excerpts_nor_the_counter() -> None:
    context = _review("v3", _payload())

    assert context.policy_snapshot is None
    assert context.context_blocks == () and context.retrieved == ()
    assert context.excluded_files == 0
    user = PromptBuilder().build_prompt(context).user
    assert "<context_excerpts>" not in user and "<excluded_files" not in user
    assert user.endswith("<omitted_files>\napp/too_big.py\n</omitted_files>")


def test_the_stage_names_the_head_of_the_attempts_diff_map() -> None:
    # execute_review labels the snapshot-path diff and excerpts with it, not the live head.
    stage = _context_stage(StageTrace())
    assert stage.head_sha is None

    asyncio.run(stage.run(STAGE_RUN))

    assert stage.head_sha == STAGE_HEAD


def _bounded_l4_id(block: ContextBlock) -> str:
    # The snapshot-path id of an L4 block: its revision and a digest of the stage's id.
    return f"L4:{HEAD[:12]}:{sha256(block.block_id.encode()).hexdigest()[:16]}"


# A 200-character path: the stage's L4 id ``L4:{revision[:12]}:{path}#{index}`` embeds it.
LONG_PATH = "pkg/" + "d" * 191 + "/m.py"
LONG_FIRST = _block(
    f"L4:{HEAD[:12]}:{LONG_PATH}#0",
    ContextLevel.L4,
    LONG_PATH,
    ((1, 1),),
    "class First:",
    role=SourceRole.AST_EXCERPT,
    representation=Representation.SIGNATURE,
)
LONG_SECOND = replace(
    LONG_FIRST,
    block_id=f"L4:{HEAD[:12]}:{LONG_PATH}#1",
    source=replace(LONG_FIRST.source, ranges=(LineRange(4, 4),)),
    text="class Second:",
)


def test_snapshot_review_cites_an_l4_excerpt_of_a_long_path_by_a_bounded_id() -> None:
    assert len(LONG_PATH) == 200
    payload = replace(_payload(), blocks=(L1_HUNK, LONG_FIRST, LONG_SECOND, L2_WINDOW))

    context = _review("v4", payload)

    first, second = _bounded_l4_id(LONG_FIRST), _bounded_l4_id(LONG_SECOND)
    assert [block.block_id for block in context.context_blocks] == [first, second, "L2:f1:3-4"]
    assert first != second and len(first) <= 128
    # The stage's own id is past the v2 limit: it could not be cited.
    with pytest.raises(ValueError, match="at most 128 characters"):
        EvidenceRef(block_id=LONG_FIRST.block_id, start_line=1, end_line=1)
    # The one bounded id is shown, recorded in the manifest and cited.
    user = PromptBuilder().build_prompt(context).user
    assert f'<excerpt block_id="{first}" level="L4" path="{LONG_PATH}"' in user
    assert LONG_FIRST.block_id not in user
    manifest = PromptBuilder().build_manifest(context, call_id=RUN_ID)
    ref = EvidenceRef(block_id=first, start_line=1, end_line=1)
    assert manifest.is_range_shown(ref.block_id, ref.start_line, ref.end_line)
    assert manifest.is_range_shown(second, 4, 4) and not manifest.is_range_shown(second, 1, 1)
    # Only L4 is renamed; the payload keeps the stage's ids (its trace and the legacy path).
    assert snapshot_block(L2_WINDOW) is L2_WINDOW
    assert payload.blocks[1].block_id == f"L4:{HEAD[:12]}:{LONG_PATH}#0"
