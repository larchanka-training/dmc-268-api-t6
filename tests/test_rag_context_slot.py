"""Graph RAG context: the RAG slot of the review context payload (ADR 0001 §9)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from typing import Any
from uuid import UUID

import pytest

from app.modules.code_index.domain.enums import EdgeKind, IndexStatus, RagIndexStatus
from app.modules.integrations.bitbucket.infrastructure.run_http import BitbucketRunHttpError
from app.modules.reviews.application.ast_resolution import (
    ImportResolution,
    RevisionTree,
)
from app.modules.reviews.application.context_provenance import LineRange, SourceRef, SourceRole
from app.modules.reviews.application.diff_map import (
    DiffMap,
    DiffSide,
)
from app.modules.reviews.application.graph_retriever import GraphHints, GraphTarget
from app.modules.reviews.application.policy_documents import missing_document
from app.modules.reviews.application.policy_errors import PolicyError, PolicyErrorCode
from app.modules.reviews.application.policy_snapshot import (
    AGENTS_DOCUMENT_PATH,
    RULES_DOCUMENT_PATH,
    ExecutionProfile,
    PromptBindings,
)
from app.modules.reviews.application.prepare_policy_snapshot import (
    RunPolicyPins,
    build_policy_snapshot,
)
from app.modules.reviews.application.rag_context import (
    CachedBlobSource,
    RagCandidates,
    RetrievedPlace,
    ReverseEdgeSelector,
)
from app.modules.reviews.application.review_context import (
    BuildReviewContext,
    ContextLevel,
    ContextOmission,
    RagPlan,
    RagStats,
    Representation,
    ReviewContextPayload,
)
from app.modules.reviews.application.review_context_stage import context_summary
from app.modules.reviews.application.review_ignore import (
    IgnoreMatcher,
    apply_ignore,
    parse_review_ignore,
)
from app.modules.reviews.application.run_failures import RunCancelled, RunFailure
from tests.rag_context_fakes import (
    _BASE,
    _HEAD,
    _INDEX_COMMIT,
    _PY_USER,
    _PY_VIEW,
    _REPOSITORY,
    BlobCacheUow,
    BlobStore,
    Graph,
    InProcessParser,
    Repository,
    SharedBlobCache,
    Words,
    _diff_map,
    _load_hints,
    _store_hints,
    _vcs_file,
)

_PLAN = RagPlan()


_MODELS = 'class Item:\n    """An item."""\n\n    def check(self):\n        return True\n'


_SLOT_STORE = (
    "from .models import Item\n\n\ndef save(item: Item):\n    item.check()\n    return item\n"
)


_SLOT_PATCH = "@@ -4,2 +4,3 @@\n def save(item: Item):\n+    item.check()\n     return item"


_TOO_LARGE_PATCH = "@@ -0,0 +1,3001 @@\n" + "\n".join("+x" for _ in range(3_001))


class TreeUnavailable(Repository):
    async def tree(self, revision: str) -> RevisionTree:
        raise ConnectionError("provider unavailable")


def _build(
    repository: Repository,
    diff_map: DiffMap,
    graph: Graph | None,
    *,
    plan: RagPlan | None = _PLAN,
    budget: int = 100_000,
    rules_text: str | None = None,
) -> ReviewContextPayload:
    """Build the context of a run; with a graph, the builder has the RAG sources of the run."""
    policy = parse_review_ignore(rules_text)
    snapshot = build_policy_snapshot(
        RunPolicyPins(
            UUID(int=2),
            diff_map.repository_id,
            "b" * 40,
            UUID(int=4),
            "2" * 64,
            PromptBindings("3" * 64, "", ""),
            ExecutionProfile.fast("model-a", ""),
        ),
        rules=missing_document(RULES_DOCUMENT_PATH),
        agents=missing_document(AGENTS_DOCUMENT_PATH),
        ignore=policy,
        snapshot_id=UUID(int=3),
    )
    matcher = IgnoreMatcher(policy)
    store = BlobStore()
    builder = BuildReviewContext(
        source=repository,
        parser=InProcessParser(),
        counter=Words(),
        retriever=graph,
        rag_blobs=None
        if graph is None
        else CachedBlobSource(
            repository_id=diff_map.repository_id,
            source=repository,
            cache=SharedBlobCache(store),
            uow_factory=lambda: BlobCacheUow(store),
        ),
    )
    return asyncio.run(
        builder.execute(
            diff_map=diff_map,
            snapshot=snapshot,
            matcher=matcher,
            ignore=apply_ignore(matcher, diff_map),
            token_budget=budget,
            rag=plan,
        )
    )


def _slot_repository(**importers: str) -> tuple[Repository, DiffMap]:
    """A PR that changes ``save``, which reads the imported ``Item``: L1 is 11 tokens, the
    L4 signature of ``Item`` 4, the full file (L3) 10 and its window (L2) 6."""
    repository = Repository({"pkg/models.py": _MODELS, "pkg/store.py": _SLOT_STORE, **importers})
    return repository, _diff_map(_vcs_file(repository, "pkg/store.py", _SLOT_PATCH))


def _bulky_user(words: int) -> str:
    """An importer of ``save`` whose one place is ``words + 8`` tokens."""
    return (
        "from .store import save\n\n\ndef run(item):\n"
        f'    """{" ".join(["w"] * words)}"""\n    return save(item)\n'
    )


def test_a_retrieved_block_is_read_at_the_pr_head_and_counted_in_the_payload() -> None:
    repository, diff_map = _slot_repository(**{"pkg/app.py": _PY_USER})

    payload = _build(repository, diff_map, _store_hints("pkg/app.py", "pkg/gone.py"))

    (block,) = payload.retrieved
    assert (block.block_id, block.level, block.representation) == (
        "RAG:pkg/app.py:1-1,4-5",
        ContextLevel.RAG,
        Representation.EXACT_LINES,
    )
    assert block.source == SourceRef(
        role=SourceRole.RETRIEVED_EXCERPT,
        repository_id=_REPOSITORY,
        # The revision of the PR side, never the commit of the index.
        revision=_HEAD,
        path="pkg/app.py",
        blob_sha=repository.sha("pkg/app.py"),
        origin_paths=(),
        side=DiffSide.RIGHT,
        ranges=(LineRange(1, 1), LineRange(4, 5)),
        reason="rag_reverse_edge",
    )
    assert (block.edge_kind, block.index_commit_sha) == (EdgeKind.USES_IMPORTED, _INDEX_COMMIT)
    assert (block.text, block.tokens) == (
        "from .store import save\ndef run(item):\n    return save(item)",
        8,
    )
    # A retrieved block belongs to no diff file: it is kept apart, and still counted.
    assert ContextLevel.RAG not in {item.level for item in payload.blocks}
    assert payload.levels_present == (
        ContextLevel.L1,
        ContextLevel.L3,
        ContextLevel.L4,
        ContextLevel.RAG,
    )
    assert payload.tokens_used == 11 + 10 + 4 + 8
    assert payload.omissions == (ContextOmission("rag_candidate_missing", "pkg/gone.py"),)
    stats = payload.rag
    assert stats is not None and stats.index is not None and stats.index.usable
    assert (stats.reason, stats.found, stats.confirmed, dict(stats.dropped), stats.tokens) == (
        None,
        2,
        1,
        {"rag_candidate_missing": 1},
        8,
    )

    summary = context_summary(diff_map, payload)

    assert summary["levels_present"] == ["L1", "L3", "L4", "RAG"]
    assert summary["tokens_used"] == 33
    assert summary["tokens_by_role"] == {
        "diff": 11,
        "ast_excerpt": 4,
        "source_excerpt": 10,
        "retrieved_excerpt": 8,
    }
    assert summary["tokens_by_level"] == {"L1": 11, "L4": 4, "L3": 10, "RAG": 8}
    assert summary["blocks_by_level"] == {"L1": 1, "L4": 1, "L3": 1, "RAG": 1}


def test_a_definition_l4_cannot_show_does_not_hide_its_rag_place() -> None:
    docs = "\n\n\n".join(
        f'def {name}(value):\n    """{" ".join(["w"] * 1_995)}"""\n    return value'
        for name in "abcd"
    )
    app = (
        "from .store import save\n\n\ndef run(item):\n"
        f'    """{" ".join(["w"] * 20)}"""\n    return save(item)\n'
    )
    store = (
        "from .docs import a, b, c, d\nfrom .app import run\n\n\n"
        "def save(item):\n    item.check()\n    return run(a(b(c(d(item)))))\n"
    )
    repository = Repository({"pkg/docs.py": f"{docs}\n", "pkg/app.py": app, "pkg/store.py": store})
    patch = (
        "@@ -5,2 +5,3 @@\n def save(item):\n+    item.check()\n     return run(a(b(c(d(item)))))"
    )
    diff_map = _diff_map(_vcs_file(repository, "pkg/store.py", patch))

    payload = _build(repository, diff_map, _store_hints("pkg/app.py"))

    # The changed code reads `run`, but the four docstrings before it fill the 8 000
    # tokens of L4: L4 omits it, so the slot shows its place.
    assert len([block for block in payload.blocks if block.level is ContextLevel.L4]) == 4
    assert ContextOmission("ast_limit", "pkg/app.py") in payload.omissions
    (block,) = payload.retrieved
    assert (block.source.path, block.source.ranges) == (
        "pkg/app.py",
        (LineRange(1, 1), LineRange(4, 6)),
    )
    stats = payload.rag
    assert stats is not None and (stats.found, stats.confirmed) == (1, 1)


def test_a_definition_l4_displays_gives_no_rag_block() -> None:
    store = "from .app import run\n\n\ndef save(item):\n    item.check()\n    return run(item)\n"
    app = "from .store import save\n\n\ndef run(item):\n    return save(item)\n"
    repository = Repository({"pkg/store.py": store, "pkg/app.py": app})
    patch = "@@ -4,2 +4,3 @@\n def save(item):\n+    item.check()\n     return run(item)"
    diff_map = _diff_map(_vcs_file(repository, "pkg/store.py", patch))

    payload = _build(repository, diff_map, _store_hints("pkg/app.py"))

    # L4 shows `run`, the place of the confirmed importer: the slot does not repeat it.
    assert len([block for block in payload.blocks if block.level is ContextLevel.L4]) == 1
    assert payload.retrieved == ()
    stats = payload.rag
    assert stats is not None and (stats.found, stats.confirmed) == (1, 1)


def test_the_rag_slot_displaces_l3_but_never_l1_or_l4() -> None:
    repository, diff_map = _slot_repository(**{"pkg/app.py": _PY_USER})
    graph = _store_hints("pkg/app.py")

    # L1, L4 and the full file fill the budget exactly.
    without = _build(repository, diff_map, graph, plan=None, budget=25)
    with_rag = _build(repository, diff_map, graph, budget=25)

    assert [item.level for item in without.blocks] == [
        ContextLevel.L1,
        ContextLevel.L4,
        ContextLevel.L3,
    ]
    assert without.omissions == ()
    assert with_rag.blocks == tuple(
        item for item in without.blocks if item.level is not ContextLevel.L3
    )
    assert [item.tokens for item in with_rag.retrieved] == [8]
    # Two tokens are left: neither the full file nor its window fits.
    assert with_rag.omissions == (ContextOmission("budget", "pkg/store.py"),)
    assert with_rag.tokens_used == 11 + 4 + 8
    assert len(graph.reads) == 1


def test_the_rag_slot_holds_at_most_4000_tokens() -> None:
    paths = ["pkg/user_0.py", "pkg/user_1.py", "pkg/user_2.py"]
    repository, diff_map = _slot_repository(**{path: _bulky_user(1_492) for path in paths})

    payload = _build(repository, diff_map, _store_hints(*paths))

    # Two places of 1 500 tokens fit the slot; the third would pass 4 000.
    assert [(item.source.path, item.tokens) for item in payload.retrieved] == [
        ("pkg/user_0.py", 1_500),
        ("pkg/user_1.py", 1_500),
    ]
    assert payload.omissions == (ContextOmission("rag_limit", "pkg/user_2.py"),)
    # The budget left after the slot still goes to L3.
    assert [item.level for item in payload.blocks] == [
        ContextLevel.L1,
        ContextLevel.L4,
        ContextLevel.L3,
    ]
    stats = payload.rag
    assert stats is not None
    # A candidate whose place does not fit is still confirmed.
    assert (stats.found, stats.confirmed, dict(stats.dropped), stats.tokens) == (3, 3, {}, 3_000)


def test_the_rag_slot_is_bounded_by_what_l1_and_l4_leave() -> None:
    paths = ["pkg/user_0.py", "pkg/user_1.py"]
    repository, diff_map = _slot_repository(**{path: _bulky_user(1_492) for path in paths})

    # L1 and L4 take 15 tokens and leave 1 600, less than the slot.
    payload = _build(repository, diff_map, _store_hints(*paths), budget=15 + 1_600)

    assert [item.source.path for item in payload.retrieved] == ["pkg/user_0.py"]
    assert payload.omissions == (ContextOmission("rag_limit", "pkg/user_1.py"),)
    # The full file takes 10 of the 100 tokens the slot left.
    assert [item.level for item in payload.blocks] == [
        ContextLevel.L1,
        ContextLevel.L4,
        ContextLevel.L3,
    ]
    assert payload.tokens_used == 15 + 1_500 + 10


def test_an_index_not_usable_at_the_graph_read_gives_no_block_and_its_fresh_status() -> None:
    repository, diff_map = _slot_repository(**{"pkg/app.py": _PY_USER})
    graph = _store_hints("pkg/app.py")
    graph.status = IndexStatus.EMPTY

    payload = _build(repository, diff_map, graph)

    assert payload.retrieved == ()
    stats = payload.rag
    assert stats is not None and stats.index is not None
    assert stats.index.status is RagIndexStatus.EMPTY
    assert (stats.reason, stats.found, stats.tokens) == (None, 0, 0)
    assert "pkg/app.py" not in repository.read


def _returned_early(reason: str, graph: Graph) -> ReviewContextPayload:
    """A build with a plan that returns before the RAG slot, for each reason it has."""
    repository, diff_map = _slot_repository(**{"pkg/app.py": _PY_USER})
    changed = _vcs_file(repository, "pkg/store.py", _SLOT_PATCH)
    match reason:
        case "diff_inventory_incomplete":
            return _build(repository, _diff_map(changed, listed=2), graph)
        case "no_changes":
            return _build(repository, _diff_map(), graph)
        case "all_changes_ignored":
            return _build(repository, diff_map, graph, rules_text="```review-ignore\n**\n```\n")
        case "no_reviewable_files":
            unanchorable = _vcs_file(repository, "pkg/store.py", None, additions=3)
            return _build(repository, _diff_map(unanchorable), graph)
        case "diff_too_large":
            large = _vcs_file(
                repository, "pkg/big.py", _TOO_LARGE_PATCH, additions=3_001, status="added"
            )
            return _build(repository, _diff_map(large), graph)
        case "budget":
            # Not even the first hunk fits: L1 is cut.
            return _build(repository, diff_map, graph, budget=1)
        case "tree_unavailable":
            unavailable = TreeUnavailable(repository.files)
            unavailable_map = _diff_map(_vcs_file(unavailable, "pkg/store.py", _SLOT_PATCH))
            return _build(unavailable, unavailable_map, graph)
    raise AssertionError(reason)


@pytest.mark.parametrize(
    "reason",
    [
        "diff_inventory_incomplete",
        "no_changes",
        "all_changes_ignored",
        "no_reviewable_files",
        "diff_too_large",
        "budget",
        "tree_unavailable",
    ],
)
def test_a_build_that_returns_before_the_slot_gives_its_reason_and_zero_counters(
    reason: str,
) -> None:
    graph = _store_hints("pkg/app.py")

    payload = _returned_early(reason, graph)

    stats = payload.rag
    assert stats == RagStats(reason=reason)
    assert stats is not None and (stats.index, stats.found, stats.tokens) == (None, 0, 0)
    assert payload.retrieved == ()
    assert graph.reads == []


def test_without_a_plan_the_rag_sources_change_nothing() -> None:
    repository, diff_map = _slot_repository(**{"pkg/app.py": _PY_USER})
    graph = _store_hints("pkg/app.py")

    with_sources = _build(repository, diff_map, graph, plan=None)
    without_sources = _build(repository, diff_map, None, plan=None)

    assert with_sources == without_sources
    assert (with_sources.retrieved, with_sources.rag) == ((), None)
    assert graph.reads == []
    assert "pkg/app.py" not in repository.read


def test_a_plan_without_the_rag_sources_is_refused_before_any_read() -> None:
    repository, diff_map = _slot_repository(**{"pkg/app.py": _PY_USER})

    with pytest.raises(ValueError, match="RAG plan"):
        _build(repository, diff_map, None)
    assert repository.read == []


@dataclass
class FailingGraph(Graph):
    """A graph read that fails once it is asked."""

    error: Exception = field(default_factory=lambda: RuntimeError("graph unavailable"))

    async def read_hints(self, repository_id: UUID, targets: Sequence[GraphTarget]) -> GraphHints:
        self.reads.append((repository_id, tuple(targets)))
        raise self.error


def test_a_failing_rag_slot_keeps_every_other_level_and_reports_the_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    repository, diff_map = _slot_repository(**{"pkg/app.py": _PY_USER})
    graph = FailingGraph(error=RuntimeError("x" * 1_500))

    without = _build(repository, diff_map, graph, plan=None)
    with caplog.at_level(logging.WARNING, logger="app.modules.reviews.application.review_context"):
        payload = _build(repository, diff_map, graph)

    assert len(graph.reads) == 1
    # No block of the slot; L1, L4 and L3 are those of a build without RAG.
    assert (payload.blocks, payload.omissions, payload.tokens_used) == (
        without.blocks,
        without.omissions,
        without.tokens_used,
    )
    assert payload.retrieved == ()
    assert payload.rag == RagStats(error_type="RuntimeError", error_message="x" * 1_000)
    assert [record.getMessage() for record in caplog.records] == [
        f"RAG slot failed head_sha={_HEAD}"
    ]


def test_rag_blob_byte_exhaustion_omits_candidate_and_keeps_review_context() -> None:
    repository, diff_map = _slot_repository(**{"pkg/app.py": _PY_USER})

    class ExhaustedRagBlob(Repository):
        async def read_blob(self, blob_sha: str) -> bytes:
            if blob_sha == self.sha("pkg/app.py"):
                raise BitbucketRunHttpError("total_budget_exceeded")
            return await super().read_blob(blob_sha)

    exhausted = ExhaustedRagBlob(repository.files)
    payload = _build(exhausted, diff_map, _store_hints("pkg/app.py"))

    assert payload.reason is None
    assert payload.retrieved == ()
    assert ContextLevel.L1 in payload.levels_present
    assert ContextLevel.L3 in payload.levels_present
    assert ContextOmission("source_read_failed", "pkg/app.py") in payload.omissions
    assert payload.rag is not None
    assert dict(payload.rag.dropped) == {"source_read_failed": 1}


@pytest.mark.parametrize(
    "error",
    [
        RunCancelled("installation_unavailable"),
        RunFailure("deadline_exceeded", "the run deadline passed"),
        PolicyError(PolicyErrorCode.ACCESS_DENIED, "GitHub answered 403"),
    ],
    ids=["cancel", "deadline", "policy"],
)
def test_control_raised_in_the_rag_slot_ends_the_build(error: Exception) -> None:
    repository, diff_map = _slot_repository(**{"pkg/app.py": _PY_USER})

    with pytest.raises(type(error)) as caught:
        _build(repository, diff_map, FailingGraph(error=error))

    assert caught.value is error


_SLOT_BASE_STORE = (
    "from .models import Item\n\n\ndef save(item: Item):\n    return item\n\n\n"
    "def load(key):\n    return key\n"
)


_SLOT_REMOVAL_PATCH = (
    "@@ -4,6 +4,3 @@\n def save(item: Item):\n+    item.check()\n     return item\n"
    "-\n-\n-def load(key):\n-    return key"
)


def _slot_removal(**importers: str) -> tuple[Repository, DiffMap]:
    """The PR of the slot tests that also removes ``load`` from ``pkg/store.py``."""
    repository = Repository(
        {"pkg/models.py": _MODELS, "pkg/store.py": _SLOT_STORE, **importers},
        base={"pkg/store.py": _SLOT_BASE_STORE},
    )
    patch = _vcs_file(repository, "pkg/store.py", _SLOT_REMOVAL_PATCH, deletions=4)
    return repository, _diff_map(patch)


def test_a_broken_reference_gives_a_block_per_revision_and_its_definition_once() -> None:
    repository, diff_map = _slot_removal(**{"pkg/panel.py": _PY_VIEW, "pkg/view.py": _PY_VIEW})

    payload = _build(repository, diff_map, _load_hints("pkg/panel.py", "pkg/view.py"))

    assert [
        (block.block_id, block.representation, block.source.revision, block.source.side)
        for block in payload.retrieved
    ] == [
        ("RAG:pkg/panel.py:1-1,4-5", Representation.EXACT_LINES, _HEAD, DiffSide.RIGHT),
        ("RAG:pkg/store.py:8-8", Representation.SIGNATURE, _BASE, DiffSide.LEFT),
        ("RAG:pkg/view.py:1-1,4-5", Representation.EXACT_LINES, _HEAD, DiffSide.RIGHT),
    ]
    assert payload.retrieved[1].text == "def load(key):"
    stats = payload.rag
    assert stats is not None
    assert (stats.found, stats.confirmed, dict(stats.dropped), stats.tokens) == (2, 2, {}, 18)


def test_a_broken_reference_is_shown_with_its_definition_or_not_at_all() -> None:
    repository, diff_map = _slot_removal(**{"pkg/view.py": _PY_VIEW})
    without = _build(repository, diff_map, None, plan=None)
    kept = sum(
        block.tokens
        for block in without.blocks
        if block.level in {ContextLevel.L1, ContextLevel.L4}
    )

    # The use (8 tokens) would fit alone; with its definition (2 tokens) it does not.
    payload = _build(repository, diff_map, _load_hints("pkg/view.py"), budget=kept + 9)

    assert payload.retrieved == ()
    assert ContextOmission("rag_limit", "pkg/view.py") in payload.omissions
    stats = payload.rag
    assert stats is not None
    assert (stats.confirmed, stats.tokens) == (1, 0)


def test_each_place_of_a_broken_reference_names_the_definitions_it_read() -> None:
    repository, diff_map = _slot_removal(**{"pkg/panel.py": _PY_VIEW, "pkg/view.py": _PY_VIEW})

    payload = _build(repository, diff_map, _load_hints("pkg/panel.py", "pkg/view.py"))

    # The definition is shown once, after the first place; both places name it, so the fit
    # keeps it while either of them stays.
    panel, definition, view = payload.retrieved
    assert (panel.definitions, definition.definitions, view.definitions) == (
        (definition.source,),
        (),
        (definition.source,),
    )


def _extra_line(monkeypatch: pytest.MonkeyPatch, path: str) -> None:
    """The selector's places as it returns them, with one text line too many at ``path``."""
    select = ReverseEdgeSelector.select

    def broken(place: RetrievedPlace) -> RetrievedPlace:
        text = f"{place.text}\nan extra line" if place.source.path == path else place.text
        return replace(place, text=text, definitions=tuple(map(broken, place.definitions)))

    async def tampered(self: ReverseEdgeSelector, **kwargs: Any) -> RagCandidates:
        candidates = await select(self, **kwargs)
        return replace(candidates, places=tuple(map(broken, candidates.places)))

    monkeypatch.setattr(ReverseEdgeSelector, "select", tampered)


def test_a_place_whose_text_is_not_its_lines_is_dropped_and_the_slot_keeps_the_rest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, diff_map = _slot_removal(**{"pkg/panel.py": _PY_VIEW, "pkg/view.py": _PY_VIEW})
    _extra_line(monkeypatch, "pkg/panel.py")

    payload = _build(repository, diff_map, _load_hints("pkg/panel.py", "pkg/view.py"))

    # The render would refuse it and fail the run: the place goes, the next one takes the
    # definition they share.
    assert [block.block_id for block in payload.retrieved] == [
        "RAG:pkg/view.py:1-1,4-5",
        "RAG:pkg/store.py:8-8",
    ]
    assert ContextOmission("rag_candidate_missing", "pkg/panel.py") in payload.omissions
    stats = payload.rag
    assert stats is not None
    assert (stats.found, stats.confirmed, dict(stats.dropped), stats.tokens) == (2, 2, {}, 10)


def test_a_definition_whose_text_is_not_its_lines_takes_the_places_that_read_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, diff_map = _slot_removal(**{"pkg/panel.py": _PY_VIEW, "pkg/view.py": _PY_VIEW})
    _extra_line(monkeypatch, "pkg/store.py")

    payload = _build(repository, diff_map, _load_hints("pkg/panel.py", "pkg/view.py"))

    # A broken reference is shown with its definition or not at all.
    assert payload.retrieved == ()
    assert [item for item in payload.omissions if item.reason == "rag_candidate_missing"] == [
        ContextOmission("rag_candidate_missing", "pkg/panel.py"),
        ContextOmission("rag_candidate_missing", "pkg/view.py"),
    ]
    stats = payload.rag
    assert stats is not None and (stats.confirmed, stats.tokens) == (2, 0)


class RefusedCounter:
    """A counter the build must stop using once ``with_counter`` replaced it."""

    def count(self, text: str) -> int:
        raise AssertionError("counted with the replaced counter")


class SubclassedBuild(BuildReviewContext):
    """A subclass of the build, as a test double of the stage is."""


@pytest.mark.parametrize("swapped", [False, True], ids=["built-with-words", "swapped-to-words"])
def test_with_counter_keeps_every_collaborator_and_the_type_of_the_build(swapped: bool) -> None:
    # R19: the stage swaps the counter of every snapshot-path build. The copy keeps the RAG
    # sources (else a RAG plan raises and the stage reports "context build failed") and the
    # built-in exclusion (else an imported generated file becomes an L4 excerpt).
    repository, diff_map = _slot_repository(**{"pkg/app.py": _PY_USER})
    store = BlobStore()
    builder: BuildReviewContext = SubclassedBuild(
        source=repository,
        parser=InProcessParser(),
        counter=RefusedCounter() if swapped else Words(),
        builtin_excluded=lambda path: path == "pkg/models.py",
        retriever=_store_hints("pkg/app.py"),
        rag_blobs=CachedBlobSource(
            repository_id=diff_map.repository_id,
            source=repository,
            cache=SharedBlobCache(store),
            uow_factory=lambda: BlobCacheUow(store),
        ),
    )
    if swapped:
        builder = builder.with_counter(Words())
    policy = parse_review_ignore(None)
    snapshot = build_policy_snapshot(
        RunPolicyPins(
            UUID(int=2),
            diff_map.repository_id,
            "b" * 40,
            UUID(int=4),
            "2" * 64,
            PromptBindings("3" * 64, "", ""),
            ExecutionProfile.fast("model-a", ""),
        ),
        rules=missing_document(RULES_DOCUMENT_PATH),
        agents=missing_document(AGENTS_DOCUMENT_PATH),
        ignore=policy,
        snapshot_id=UUID(int=3),
    )
    matcher = IgnoreMatcher(policy)

    payload = asyncio.run(
        builder.execute(
            diff_map=diff_map,
            snapshot=snapshot,
            matcher=matcher,
            ignore=apply_ignore(matcher, diff_map),
            token_budget=100_000,
            rag=_PLAN,
        )
    )

    # The RAG slot is filled and counted with the new counter.
    [block] = payload.retrieved
    assert (block.block_id, block.tokens) == ("RAG:pkg/app.py:1-1,4-5", 8)
    # The imported definition is behind the built-in exclusion: never read, never L4.
    assert [ref.resolution for ref in payload.imports] == [ImportResolution.EXCLUDED]
    assert ContextLevel.L4 not in payload.levels_present
    assert "pkg/models.py" not in repository.read
    # L1 11, L3 10 and the retrieved place 8, all counted by ``Words``.
    assert payload.tokens_used == 11 + 10 + 8
    assert type(builder) is SubclassedBuild
