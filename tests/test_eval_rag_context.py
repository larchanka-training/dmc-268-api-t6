"""The offline context of the #146 A/B arms: a fixture repository goes through the
production ``BuildReviewContext`` without a database, a network or a model (Task 1 spike)."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest

from app.modules.code_index.domain.enums import EdgeKind
from app.modules.reviews.application import execute_review
from app.modules.reviews.application.context_provenance import SourceRole
from app.modules.reviews.application.graph_retriever import GraphHint
from app.modules.reviews.application.policy_errors import PolicyError
from app.modules.reviews.application.policy_snapshot import PINNED_PROMPT_CHECKSUMS
from app.modules.reviews.application.review_context import ContextLevel, ContextStatus
from review.scripts import eval_rag_context
from review.scripts.eval_rag_context import Arm, ArmInput, build_arm_input

_SPIKE = Path(__file__).parent / "fixtures" / "rag_eval" / "spike"
_CALLER = "pkg/api.py"
# The frozen graph of the fixture: ``pkg/api.py`` uses the ``save`` of ``pkg/store.py``.
_HINTS = (GraphHint(_CALLER, EdgeKind.USES_IMPORTED, "pkg/store.py", "save"),)
_EMPTY_SECTION = re.compile(r"<retrieved_excerpts>\s*</retrieved_excerpts>")
_SECTION = re.compile(r"<retrieved_excerpts>.*?</retrieved_excerpts>", re.DOTALL)


def _build(arm: Arm) -> ArmInput:
    return asyncio.run(build_arm_input(_SPIKE, arm, _HINTS))


def test_arm_b_has_one_retrieved_block_for_the_caller_and_arm_a_has_none() -> None:
    arm_a, arm_b = _build("A"), _build("B")

    assert arm_a.payload.status is ContextStatus.REVIEW
    assert arm_a.payload.retrieved == ()
    assert [(block.level, block.source.path) for block in arm_b.payload.retrieved] == [
        (ContextLevel.RAG, _CALLER)
    ]
    assert arm_b.payload.rag is not None
    assert arm_b.payload.rag.confirmed == 1


def test_the_non_rag_blocks_of_both_arms_are_identical() -> None:
    arm_a, arm_b = _build("A"), _build("B")

    def shown(arm: ArmInput) -> list[tuple[str, str]]:
        return [(block.block_id, block.text) for block in arm.context.context_blocks]

    assert shown(arm_a) == shown(arm_b)
    # Not vacuous: L2/L3 and L4 (the ``Item`` definition) are in both.
    assert {block.level for block in arm_a.context.context_blocks} >= {
        ContextLevel.L3,
        ContextLevel.L4,
    }
    assert arm_a.context.changed_files == arm_b.context.changed_files
    # The whole prompt differs only in the retrieved section.
    assert arm_a.prompt.system == arm_b.prompt.system
    assert _SECTION.sub("", arm_a.prompt.user) == _SECTION.sub("", arm_b.prompt.user)
    assert _SECTION.search(arm_a.prompt.user) and _SECTION.search(arm_b.prompt.user)


def test_the_v5_prompt_shows_an_empty_section_for_arm_a_and_one_excerpt_for_arm_b() -> None:
    arm_a, arm_b = _build("A"), _build("B")

    assert arm_a.prompt.user.count("<excerpt ") == len(arm_a.context.context_blocks)
    assert _EMPTY_SECTION.search(arm_a.prompt.user)
    assert 'level="RAG"' not in arm_a.prompt.user
    assert not _EMPTY_SECTION.search(arm_b.prompt.user)
    assert arm_b.prompt.user.count('level="RAG"') == 1
    assert f'path="{_CALLER}"' in arm_b.prompt.user
    assert "return save(Item(name))" in arm_b.prompt.user


def test_the_manifest_of_arm_b_carries_the_retrieved_block_the_guard_accepted() -> None:
    """``build_arm_input`` runs the gateway's pre-send provenance guard on the head id."""
    arm_a, arm_b = _build("A"), _build("B")

    def retrieved(arm: ArmInput) -> list[tuple[str | None, str | None, str | None]]:
        return [
            (block.path, block.edge_kind, block.index_commit_sha)
            for block in arm.manifest.blocks
            if block.source_role is SourceRole.RETRIEVED_EXCERPT
        ]

    assert retrieved(arm_a) == []
    [(path, edge_kind, index_commit)] = retrieved(arm_b)
    assert (path, edge_kind) == (_CALLER, EdgeKind.USES_IMPORTED.value)
    assert index_commit is not None


def test_the_manifests_of_two_builds_are_identical() -> None:
    """Nothing random reaches the manifest, so a recording can name it by its content."""
    assert _build("B").manifest == _build("B").manifest
    assert _build("A").manifest == _build("A").manifest


def test_the_excerpt_levels_are_those_of_the_use_case() -> None:
    """Tripwire for the copy: ``execute_review`` keeps its set inline and private."""
    assert eval_rag_context._EXCERPT_LEVELS == execute_review._EXCERPT_LEVELS


def test_a_system_prompt_that_is_not_the_pinned_v5_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(PINNED_PROMPT_CHECKSUMS, "review.system:v5", "0" * 64)
    with pytest.raises(PolicyError, match="review.system:v5"):
        asyncio.run(build_arm_input(_SPIKE, "A", _HINTS))


def test_an_unknown_arm_is_refused() -> None:
    with pytest.raises(ValueError, match="arm"):
        asyncio.run(build_arm_input(_SPIKE, "C", _HINTS))  # type: ignore[arg-type]
