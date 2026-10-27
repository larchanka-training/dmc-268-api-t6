"""A seeded snapshot prompt can show Bitbucket graph context with exact evidence."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from uuid import UUID

from app.bootstrap.seed_prompts import load_prompt_assets
from app.modules.code_index.domain.enums import EdgeKind
from app.modules.reviews.application.context_provenance import LineRange, SourceRef, SourceRole
from app.modules.reviews.application.diff_map import DiffSide
from app.modules.reviews.application.prompt_builder import PromptBuilder
from app.modules.reviews.application.rag_context import RetrievedPlace
from app.modules.reviews.application.rag_index import has_retrieved_section
from app.modules.reviews.application.review_context import (
    ContextLevel,
    Representation,
    RetrievedBlock,
    _retrieved_block,
)
from app.modules.reviews.application.review_path import ReviewBinding, require_served_provider
from tests.test_prompt_builder import _metadata_context
from tests.test_prompt_builder_markdown import make_test_snapshot

_HEAD = "a" * 40
_INDEX = "b" * 40
_REPO = UUID(int=100)


def _source(path: str, text: str) -> SourceRef:
    return SourceRef(
        role=SourceRole.RETRIEVED_EXCERPT,
        repository_id=_REPO,
        revision=_HEAD,
        path=path,
        blob_sha="c" * 40,
        origin_paths=(),
        side=DiffSide.RIGHT,
        ranges=(LineRange(3, 3),),
        reason="rag_reverse_edge",
    )


def test_v5_is_a_real_seeded_prompt_and_routes_to_snapshot_rag() -> None:
    assets = load_prompt_assets(Path(__file__).resolve().parents[1] / "review/prompts")
    v5 = next(asset for asset in assets if (asset.key, asset.version) == ("review.system", 5))
    assert "<retrieved_excerpts>" in v5.content
    assert has_retrieved_section("review.system", "v5")
    require_served_provider(ReviewBinding(UUID(int=1), "review.system", "v5", "bitbucket"))
    assert not has_retrieved_section("review.system", "v4")


def test_v5_renders_retrieved_lines_and_empty_slot_without_changing_v4() -> None:
    snapshot = make_test_snapshot()
    bindings = replace(snapshot.prompt_bindings, review_system_version="v5")
    snapshot = replace(snapshot, prompt_bindings=bindings)
    retrieved = RetrievedBlock(
        block_id="RAG:abc",
        level=ContextLevel.RAG,
        source=_source("pkg/user.py", "return target()"),
        representation=Representation.EXACT_LINES,
        text="return target()",
        tokens=4,
        edge_kind=EdgeKind.USES_IMPORTED,
        index_commit_sha=_INDEX,
    )
    context = replace(_metadata_context(), policy_snapshot=snapshot, retrieved=(retrieved,))
    rendered = PromptBuilder().render_input(context)
    assert "<retrieved_excerpts>" in rendered
    assert 'block_id="RAG:abc"' in rendered
    assert 'path="pkg/user.py"' in rendered
    assert 'index_commit_sha="' + _INDEX + '"' in rendered
    assert '<line n="3">return target()</line>' in rendered
    assert (
        PromptBuilder()
        .build_manifest(context, call_id=UUID(int=2), stage="review")
        .get_block("RAG:abc")
        is not None
    )
    assert "<retrieved_excerpts>" in PromptBuilder().render_input(replace(context, retrieved=()))
    v4 = replace(snapshot, prompt_bindings=replace(bindings, review_system_version="v4"))
    assert "<retrieved_excerpts>" not in PromptBuilder().render_input(
        replace(context, policy_snapshot=v4, retrieved=())
    )


def test_long_retrieved_path_keeps_bounded_stable_evidence_identity() -> None:
    path = "pkg/" + "nested/" * 30 + "user.py"
    place = RetrievedPlace(
        source=_source(path, "return target()"),
        edge_kind=EdgeKind.USES_IMPORTED,
        index_commit_sha=_INDEX,
        signature=False,
        text="return target()",
        tokens=4,
    )
    block = _retrieved_block(place)
    assert len(block.block_id) <= 128
    assert block.block_id == _retrieved_block(place).block_id
    assert (
        block.block_id
        != _retrieved_block(
            replace(place, source=replace(place.source, path=path + ".other"))
        ).block_id
    )
