"""The frozen graph-index snapshot of the #146 eval: its builder drives the production
indexer over a case's ``base/`` tree, its retriever answers like the SQL one (Task 3)."""

from __future__ import annotations

import asyncio
import json
import shutil
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from app.bootstrap.code_index_worker import CodeIndexSettings, index_versions
from app.modules.code_index.application.exclusions import INDEX_EXCLUSIONS_VERSION, IndexExclusions
from app.modules.code_index.application.index_resolution import IndexImportResolver
from app.modules.code_index.application.index_store import IndexedEdge
from app.modules.code_index.application.symbol_rows import file_rows, symbol_rows
from app.modules.code_index.domain.enums import EdgeKind, RagIndexStatus
from app.modules.code_index.domain.limits import IndexLimits
from app.modules.reviews.application.ast_resolution import RevisionTree
from app.modules.reviews.application.ast_symbols import AstFacts, language_for_path
from app.modules.reviews.application.graph_retriever import GraphHint, GraphTarget
from app.modules.reviews.application.review_ignore import IgnoreMatcher, parse_review_ignore
from app.modules.reviews.infrastructure.tree_sitter_facts import extract_facts
from review.scripts import build_rag_snapshot, rag_snapshot
from review.scripts.eval_rag_source import commit_id, git_blob_oid, read_tree
from review.scripts.rag_snapshot import (
    FrozenGraphRetriever,
    IndexSnapshot,
    SnapshotError,
    SnapshotFile,
    build_snapshot,
    parse_snapshot,
    render_snapshot,
)

_ROOT = Path(__file__).parent / "fixtures" / "rag_eval"
_CASE = _ROOT / "snapshot"
_INDEXED = "ab" * 20
_REPOSITORY = UUID(int=146)


def _edge(path: str, kind: EdgeKind, target_path: str, target_name: str = "") -> IndexedEdge:
    return IndexedEdge(path, kind, target_path, target_name)


def _build(tree: Mapping[str, bytes]) -> IndexSnapshot:
    return asyncio.run(build_snapshot(tree, indexed_commit_sha=_INDEXED))


def _fixture() -> dict[str, bytes]:
    return read_tree(_CASE / "base")


def _reference_edges(tree: Mapping[str, bytes]) -> list[IndexedEdge]:
    """The rows of the production resolver for ``tree``, put together from its public pieces.

    No unit of work and no archive: ``IndexImportResolver`` over a ``RevisionTree`` with the
    full facts of every source file, then ``symbol_rows`` / ``file_rows`` as the indexer's
    batch loop calls them. The default policy admits every supported file of the fixture.
    """
    facts: dict[str, AstFacts] = {}
    for path, raw in tree.items():
        language = language_for_path(path)
        if language is not None:
            facts[path] = extract_facts(language, raw)

    class Facts:
        async def facts_for(self, path: str) -> AstFacts:
            return facts[path]

    exclusions = IndexExclusions(IgnoreMatcher(parse_review_ignore(None)), IndexLimits())
    resolver = IndexImportResolver(
        tree=RevisionTree(_INDEXED, {path: git_blob_oid(raw) for path, raw in tree.items()}, True),
        facts=Facts(),
        path_exclusion=exclusions.path_class,
        go_module=None,
    )

    async def rows() -> list[IndexedEdge]:
        found: list[IndexedEdge] = []
        for path in sorted(facts):
            imports = await resolver.resolve_file(path, facts[path])
            indexed = file_rows(
                path,
                git_blob_oid(tree[path]),
                symbol_rows(path, facts[path]),
                ((item.record, item.edges) for item in imports),
            )
            found.extend(indexed.edges)
        return found

    return sorted(asyncio.run(rows()), key=_order)


def _order(edge: IndexedEdge) -> tuple[str, str, str, str]:
    return edge.path, edge.kind.value, edge.target_path, edge.target_name


# --- the builder: the rows of the production indexer ---------------------------------------


def test_the_builder_rows_equal_the_rows_of_the_production_resolver() -> None:
    tree = _fixture()

    snapshot = _build(tree)

    assert list(snapshot.edges) == _reference_edges(tree)
    assert snapshot.files == tuple(
        SnapshotFile(path, git_blob_oid(raw))
        for path, raw in sorted(tree.items())
        if language_for_path(path) is not None
    )


def test_the_edges_of_the_fixture_are_the_ones_its_imports_declare() -> None:
    """Expected rows written by hand from the fixture's imports, not derived by the builder."""
    snapshot = _build(_fixture())
    imports, uses = EdgeKind.IMPORTS, EdgeKind.USES_IMPORTED
    expected = {
        # A plain relative import and the name it uses.
        _edge("pkg/store.py", imports, "pkg/models.py"),
        _edge("pkg/store.py", uses, "pkg/models.py", "Item"),
        # ``from .store import save as persist``: the edge names the target's own name.
        _edge("pkg/api.py", imports, "pkg/store.py"),
        _edge("pkg/api.py", uses, "pkg/store.py", "save"),
        _edge("pkg/api.py", imports, "pkg/models.py"),
        _edge("pkg/api.py", uses, "pkg/models.py", "Item"),
        # A re-export is not followed: ``from pkg import save`` ends at ``pkg/__init__.py``.
        _edge("pkg/cli.py", imports, "pkg/__init__.py"),
        _edge("pkg/cli.py", uses, "pkg/__init__.py", "save"),
        # The barrel itself imports the module it re-exports from.
        _edge("pkg/__init__.py", imports, "pkg/store.py"),
        # TypeScript: ``export { save as persist } from "./store"`` is an export, not an
        # import, so the barrel has no edge of its own; its user binds ``persist`` at the barrel.
        _edge("lib/use.ts", imports, "lib/index.ts"),
        _edge("lib/use.ts", imports, "lib/store.ts"),
        _edge("lib/use.ts", uses, "lib/index.ts", "persist"),
        _edge("lib/use.ts", uses, "lib/store.ts", "save"),
    }

    assert set(snapshot.edges) == expected
    assert len(snapshot.edges) == len(expected)


def test_the_counts_and_the_commit_are_those_of_the_built_index() -> None:
    snapshot = _build(_fixture())

    assert snapshot.schema_version == 1
    assert snapshot.indexed_commit_sha == _INDEXED
    assert [file.path for file in snapshot.files] == [
        "lib/index.ts",
        "lib/store.ts",
        "lib/use.ts",
        "pkg/__init__.py",
        "pkg/api.py",
        "pkg/cli.py",
        "pkg/models.py",
        "pkg/store.py",
    ]
    assert snapshot.file_count == 8
    # Item and Item.__init__, save, create, run, __all__ (Python); save, run (TypeScript).
    assert snapshot.symbol_count == 8


def test_the_extractor_block_names_the_production_versions() -> None:
    versions = index_versions(CodeIndexSettings().limits)
    extractor = _build(_fixture()).extractor

    assert extractor.limits_version == versions.limits_profile_version
    assert extractor.parser_profile == versions.parser_profile_version
    assert extractor.exclusions_version == INDEX_EXCLUSIONS_VERSION


def test_the_ignore_policy_of_the_base_tree_excludes_its_paths() -> None:
    """``.review/rules.md`` of the case revision is read as the indexer reads it at HEAD."""
    tree = _fixture()
    tree[".review/rules.md"] = b"```review-ignore\nlib/**\n```\n"

    snapshot = _build(tree)

    assert all(not file.path.startswith("lib/") for file in snapshot.files)
    assert all(not edge.path.startswith("lib/") for edge in snapshot.edges)
    assert any(file.path == "pkg/api.py" for file in snapshot.files)


def test_a_build_the_indexer_does_not_publish_is_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A limit stops the job: no partial snapshot is returned."""
    monkeypatch.setattr(rag_snapshot, "SETTINGS", CodeIndexSettings(max_archive_entries=2))

    with pytest.raises(SnapshotError, match="limit_exceeded"):
        _build(_fixture())


def test_the_storage_quota_of_the_repository_is_enforced_as_in_production(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fake store reports the estimated bytes of its rows, so ``_check_storage`` can fire."""
    monkeypatch.setattr(
        rag_snapshot, "SETTINGS", CodeIndexSettings(max_repository_storage_bytes=1_000)
    )

    with pytest.raises(SnapshotError, match="repository_storage"):
        _build(_fixture())


def test_a_tree_without_a_supported_source_file_is_an_empty_snapshot() -> None:
    snapshot = _build({"README.md": b"# nothing to index\n"})

    assert (snapshot.files, snapshot.edges, snapshot.file_count) == ((), (), 0)


# --- the bytes -----------------------------------------------------------------------------


def test_two_builds_of_one_tree_are_byte_identical() -> None:
    tree = _fixture()

    first, second = render_snapshot(_build(tree)), render_snapshot(_build(tree))

    assert first == second
    assert first.endswith(b"\n") and not first.endswith(b"\n\n")
    assert b"\r" not in first
    document = json.loads(first)
    assert list(document) == [
        "schema_version",
        "indexed_commit_sha",
        "extractor",
        "file_count",
        "symbol_count",
        "files",
        "edges",
    ]
    assert list(document["extractor"]) == ["limits_version", "parser_profile", "exclusions_version"]
    assert list(document["edges"][0]) == ["path", "kind", "target_path", "target_name"]


def test_the_edges_are_sorted_as_the_store_reads_them() -> None:
    edges = _build(_fixture()).edges

    assert list(edges) == sorted(edges, key=_order)
    assert len(set(edges)) == len(edges)


def test_a_rendered_snapshot_parses_back_to_the_same_snapshot() -> None:
    snapshot = _build(_fixture())

    assert parse_snapshot(render_snapshot(snapshot)) == snapshot


def _mutated(change: Callable[[dict[str, Any]], None]) -> bytes:
    document = json.loads(render_snapshot(_build(_fixture())))
    change(document)
    return json.dumps(document).encode()


def _other_schema(document: dict[str, Any]) -> None:
    document["schema_version"] = 2


def _true_schema(document: dict[str, Any]) -> None:
    document["schema_version"] = True


def _no_edges(document: dict[str, Any]) -> None:
    del document["edges"]


def _unknown_kind(document: dict[str, Any]) -> None:
    document["edges"][0]["kind"] = "defines"


def _short_commit(document: dict[str, Any]) -> None:
    document["indexed_commit_sha"] = "short"


def _string_count(document: dict[str, Any]) -> None:
    document["file_count"] = str(document["file_count"])


def _float_count(document: dict[str, Any]) -> None:
    document["symbol_count"] = float(document["symbol_count"]) + 0.9


def _bool_count(document: dict[str, Any]) -> None:
    document["file_count"] = True


def _wrong_file_count(document: dict[str, Any]) -> None:
    document["file_count"] += 1


def _reordered_edges(document: dict[str, Any]) -> None:
    document["edges"].reverse()


def _duplicate_edge(document: dict[str, Any]) -> None:
    document["edges"].insert(1, dict(document["edges"][0]))


def _edge_from_an_unknown_file(document: dict[str, Any]) -> None:
    document["edges"][0]["path"] = "a/ghost.py"
    document["edges"].sort(key=lambda e: (e["path"], e["kind"], e["target_path"], e["target_name"]))


def _extractor_without_a_key(document: dict[str, Any]) -> None:
    del document["extractor"]["parser_profile"]


def _extractor_not_an_object(document: dict[str, Any]) -> None:
    document["extractor"] = [1]


def _extractor_null(document: dict[str, Any]) -> None:
    document["extractor"] = None


def _unsorted_files(document: dict[str, Any]) -> None:
    document["files"].reverse()


@pytest.mark.parametrize(
    "mutation",
    [
        _other_schema,
        _true_schema,
        _no_edges,
        _unknown_kind,
        _short_commit,
        _string_count,
        _float_count,
        _bool_count,
        _wrong_file_count,
        _reordered_edges,
        _duplicate_edge,
        _edge_from_an_unknown_file,
        _unsorted_files,
        _extractor_without_a_key,
        _extractor_not_an_object,
        _extractor_null,
    ],
)
def test_a_snapshot_with_another_schema_or_a_broken_row_is_refused(
    mutation: Callable[[dict[str, Any]], None],
) -> None:
    with pytest.raises(SnapshotError):
        parse_snapshot(_mutated(mutation))


# --- the retriever -------------------------------------------------------------------------


def _retriever(snapshot: IndexSnapshot | None = None) -> FrozenGraphRetriever:
    return FrozenGraphRetriever(snapshot or _build(_fixture()))


def _hints(*targets: GraphTarget) -> tuple[GraphHint, ...]:
    return asyncio.run(_retriever().read_hints(_REPOSITORY, targets)).hints


def _uses(path: str, target_path: str, name: str) -> GraphHint:
    return GraphHint(path, EdgeKind.USES_IMPORTED, target_path, name)


def _imports(path: str, target_path: str) -> GraphHint:
    return GraphHint(path, EdgeKind.IMPORTS, target_path, "")


def test_a_hit_returns_the_edges_into_the_target_ordered_by_importer() -> None:
    assert _hints(GraphTarget("pkg/models.py", "Item")) == (
        _uses("pkg/api.py", "pkg/models.py", "Item"),
        _uses("pkg/store.py", "pkg/models.py", "Item"),
    )


def test_a_module_level_target_returns_the_imports_edges_of_its_file() -> None:
    assert _hints(GraphTarget("pkg/store.py", "")) == (
        _imports("pkg/__init__.py", "pkg/store.py"),
        _imports("pkg/api.py", "pkg/store.py"),
    )


def test_a_miss_returns_no_hint_and_a_usable_index() -> None:
    read = asyncio.run(_retriever().read_hints(_REPOSITORY, [GraphTarget("pkg/store.py", "nope")]))

    assert read.hints == ()
    assert read.index.usable
    assert read.indexed_commit_sha == _INDEXED


def test_no_targets_return_no_hint() -> None:
    assert _hints() == ()


def test_a_repeated_target_is_read_once_and_the_order_of_the_targets_is_kept() -> None:
    module = GraphTarget("pkg/store.py", "")
    name = GraphTarget("pkg/store.py", "save")

    assert _hints(module, name, module) == (
        _imports("pkg/__init__.py", "pkg/store.py"),
        _imports("pkg/api.py", "pkg/store.py"),
        _uses("pkg/api.py", "pkg/store.py", "save"),
    )
    assert _hints(name, module) == (
        _uses("pkg/api.py", "pkg/store.py", "save"),
        _imports("pkg/__init__.py", "pkg/store.py"),
        _imports("pkg/api.py", "pkg/store.py"),
    )


def test_the_index_is_ready_at_the_snapshot_commit_with_its_counts() -> None:
    snapshot = _build(_fixture())

    read = asyncio.run(_retriever(snapshot).read_hints(_REPOSITORY, []))

    assert read.index.status is RagIndexStatus.READY
    assert read.index.indexed_commit_sha == _INDEXED
    assert (read.index.file_count, read.index.symbol_count) == (
        snapshot.file_count,
        snapshot.symbol_count,
    )


def test_the_retriever_does_not_read_the_repository_id() -> None:
    first = asyncio.run(_retriever().read_hints(UUID(int=1), [GraphTarget("pkg/store.py", "")]))
    second = asyncio.run(_retriever().read_hints(UUID(int=2), [GraphTarget("pkg/store.py", "")]))

    assert first == second


# --- build_rag_snapshot: --write and --check -----------------------------------------------


def _copy_case(tmp_path: Path) -> Path:
    root = tmp_path / "cases"
    shutil.copytree(_CASE, root / "RAG-01", ignore=shutil.ignore_patterns("index.json"))
    return root


def _run(*args: str | Path) -> int:
    return build_rag_snapshot.main([str(arg) for arg in args])


def test_write_then_check_passes_and_check_alone_fails_without_an_index(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _copy_case(tmp_path)

    assert _run("--check", root) == 1
    assert "RAG-01" in capsys.readouterr().err
    assert _run("--write", root) == 0
    assert _run("--check", root) == 0
    written = (root / "RAG-01" / "index.json").read_bytes()
    assert parse_snapshot(written).indexed_commit_sha == commit_id("RAG-01", "base")
    assert _run("--write", root) == 0
    assert (root / "RAG-01" / "index.json").read_bytes() == written


@pytest.mark.parametrize("target", ["pkg/store.py", "lib/use.ts"])
def test_check_fails_when_a_file_of_base_changes(tmp_path: Path, target: str) -> None:
    root = _copy_case(tmp_path)
    assert _run("--write", root) == 0

    with (root / "RAG-01" / "base" / target).open("a") as handle:
        handle.write("\n# a change\n" if target.endswith(".py") else "\n// a change\n")

    assert _run("--check", root) == 1


def test_check_ignores_a_change_of_a_file_the_index_does_not_hold(tmp_path: Path) -> None:
    """Pinned scope: indexed files and ``.review/rules.md`` only; the corpus digest (Task 6)
    covers the rest of ``base/``."""
    root = _copy_case(tmp_path)
    assert _run("--write", root) == 0

    (root / "RAG-01" / "base" / "notes.md").write_text("changed\n")
    (root / "RAG-01" / "base" / "docs.txt").write_text("new\n")

    assert _run("--check", root) == 0


def test_check_fails_when_the_ignore_policy_of_base_changes(tmp_path: Path) -> None:
    root = _copy_case(tmp_path)
    assert _run("--write", root) == 0

    rules = root / "RAG-01" / "base" / ".review" / "rules.md"
    rules.parent.mkdir()
    rules.write_text("```review-ignore\nlib/**\n```\n")

    assert _run("--check", root) == 1


def test_check_fails_when_the_index_is_edited_by_hand(tmp_path: Path) -> None:
    root = _copy_case(tmp_path)
    assert _run("--write", root) == 0
    index = root / "RAG-01" / "index.json"

    index.write_bytes(index.read_bytes().replace(b'"pkg/api.py"', b'"pkg/apx.py"', 1))

    assert _run("--check", root) == 1


def test_check_fails_when_a_file_is_added_to_base(tmp_path: Path) -> None:
    root = _copy_case(tmp_path)
    assert _run("--write", root) == 0

    (root / "RAG-01" / "base" / "pkg" / "extra.py").write_text("X = 1\n")

    assert _run("--check", root) == 1


def test_a_root_without_a_case_is_an_error_not_a_pass(tmp_path: Path) -> None:
    root = tmp_path / "cases"
    (root / "notes").mkdir(parents=True)

    assert _run("--check", root) == 1


def test_the_committed_snapshots_are_what_the_current_extractors_build() -> None:
    """Drift gate: a change to the extractors or resolver needs a regenerate commit."""
    assert _run("--check", _ROOT) == 0


def test_the_snapshot_of_a_case_names_the_commit_of_its_case_id(tmp_path: Path) -> None:
    root = _copy_case(tmp_path)
    shutil.copytree(root / "RAG-01", root / "RAG-02")

    assert _run("--write", root) == 0

    first = parse_snapshot((root / "RAG-01" / "index.json").read_bytes())
    second = parse_snapshot((root / "RAG-02" / "index.json").read_bytes())
    assert first.indexed_commit_sha == commit_id("RAG-01", "base")
    assert second.indexed_commit_sha == commit_id("RAG-02", "base")
    assert first.indexed_commit_sha != second.indexed_commit_sha
    assert first.edges == second.edges
