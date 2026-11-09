"""Graph RAG context: broken references, an export the PR removes, checked at both revisions
(ADR 0001 §8.3)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from hashlib import sha256

import pytest

from app.modules.code_index.domain.enums import EdgeKind
from app.modules.reviews.application.ast_resolution import (
    RevisionTree,
)
from app.modules.reviews.application.context_provenance import LineRange, SourceRef, SourceRole
from app.modules.reviews.application.diff_map import (
    DiffMap,
    DiffSide,
    VcsProviderName,
)
from app.modules.reviews.application.get_run_file_lines import (
    BlobCacheKey,
    BlobCacheStatus,
)
from app.modules.reviews.application.graph_retriever import GraphHint, GraphTarget
from app.modules.reviews.application.policy_errors import PolicyError, PolicyErrorCode
from app.modules.reviews.application.rag_context import (
    CachedBlobSource,
    RagOmission,
    RetrievedPlace,
)
from app.modules.reviews.application.run_failures import RunCancelled, RunFailure
from app.modules.reviews.application.vcs_diff import VcsFile
from tests.rag_context_fakes import (
    _BASE,
    _HEAD,
    _INDEX_COMMIT,
    _PY_STORE,
    _PY_STORE_PATCH,
    _PY_USER,
    _PY_VIEW,
    _REPOSITORY,
    BlobCacheUow,
    BlobStore,
    Graph,
    Repository,
    SharedBlobCache,
    _diff_map,
    _load_hints,
    _select,
    _store_hints,
    _uses,
    _vcs_file,
)

_PY_BASE_STORE = "def save(item):\n    return item\n\n\ndef load(key):\n    return key\n"


# The PR changes `save` and removes `load` (the base above, `_PY_STORE` at head).
_PY_REMOVAL_PATCH = (
    "@@ -1,6 +1,3 @@\n def save(item):\n+    item.check()\n     return item\n"
    "-\n-\n-def load(key):\n-    return key"
)


def _deletion(repository: Repository, path: str = "pkg/store.py") -> VcsFile:
    text = repository.base[path]
    assert text is not None
    removed = text.rstrip("\n").split("\n")
    patch = f"@@ -1,{len(removed)} +0,0 @@\n" + "\n".join(f"-{line}" for line in removed)
    return _vcs_file(repository, path, patch, additions=0, deletions=len(removed), status="removed")


def _removal(
    case: str, *, provider: VcsProviderName = VcsProviderName.GITHUB
) -> tuple[Repository, DiffMap]:
    """A PR that takes the export ``load`` away from ``pkg/store.py``; ``pkg/view.py``
    imports it, and the PR leaves that file alone."""
    if case == "renamed_symbol":
        head = "def save(item):\n    return item\n\n\ndef fetch(key):\n    return key\n"
        repository = Repository(
            {"pkg/store.py": head, "pkg/view.py": _PY_VIEW}, base={"pkg/store.py": _PY_BASE_STORE}
        )
        patch = "@@ -5,2 +5,2 @@\n-def load(key):\n+def fetch(key):\n     return key"
        return repository, _diff_map(
            _vcs_file(repository, "pkg/store.py", patch, deletions=1), provider=provider
        )
    if case == "deleted_file":
        repository = Repository({"pkg/view.py": _PY_VIEW}, base={"pkg/store.py": _PY_BASE_STORE})
        return repository, _diff_map(_deletion(repository), provider=provider)
    assert case == "moved_file"
    repository = Repository(
        {"pkg/storage.py": _PY_BASE_STORE, "pkg/view.py": _PY_VIEW},
        base={"pkg/store.py": _PY_BASE_STORE, "pkg/storage.py": None},
    )
    moved = _vcs_file(
        repository, "pkg/storage.py", None, additions=0, status="renamed", previous="pkg/store.py"
    )
    return repository, _diff_map(moved, provider=provider)


def _view_place(repository: Repository) -> RetrievedPlace:
    """The broken reference of ``pkg/view.py``: the import and its use at head, with the
    definition of ``load`` at the base."""
    return RetrievedPlace(
        source=SourceRef(
            role=SourceRole.RETRIEVED_EXCERPT,
            repository_id=_REPOSITORY,
            revision=_HEAD,
            path="pkg/view.py",
            blob_sha=repository.sha("pkg/view.py"),
            origin_paths=(),
            side=DiffSide.RIGHT,
            ranges=(LineRange(1, 1), LineRange(4, 5)),
            reason="rag_broken_reference",
        ),
        edge_kind=EdgeKind.USES_IMPORTED,
        index_commit_sha=_INDEX_COMMIT,
        signature=False,
        text="from .store import load\ndef show(key):\n    return load(key)",
        tokens=8,
        definitions=(
            RetrievedPlace(
                source=SourceRef(
                    role=SourceRole.RETRIEVED_EXCERPT,
                    repository_id=_REPOSITORY,
                    revision=_BASE,
                    path="pkg/store.py",
                    blob_sha=repository.base_sha("pkg/store.py"),
                    origin_paths=(),
                    side=DiffSide.LEFT,
                    ranges=(LineRange(5, 5),),
                    reason="rag_broken_reference",
                ),
                edge_kind=EdgeKind.USES_IMPORTED,
                index_commit_sha=_INDEX_COMMIT,
                signature=True,
                text="def load(key):",
                tokens=2,
            ),
        ),
    )


@pytest.mark.parametrize(
    ("case", "targets"),
    [
        ("renamed_symbol", (("pkg/store.py", "load"), ("pkg/store.py", "fetch"))),
        ("deleted_file", (("pkg/store.py", "save"), ("pkg/store.py", "load"))),
        ("moved_file", (("pkg/store.py", "save"), ("pkg/store.py", "load"))),
    ],
)
def test_an_importer_of_a_removed_export_is_a_broken_reference(
    case: str, targets: tuple[tuple[str, str], ...]
) -> None:
    repository, diff_map = _removal(case)
    graph = _load_hints("pkg/view.py")

    result = _select(repository, diff_map, graph)

    # The removed exports come first, under the default-branch path of the module.
    assert graph.reads == [(_REPOSITORY, tuple(GraphTarget(path, name) for path, name in targets))]
    # Two blocks, one revision each: the use at head and the definition at the base.
    assert result.places == (_view_place(repository),)
    assert (result.found, result.confirmed, dict(result.dropped)) == (1, 1, {})
    assert result.omissions == ()
    # The base extraction of the module is a RAG read; the importer has one blob on both
    # sides and is read once.
    assert repository.read == ["pkg/store.py@base", "pkg/view.py"]
    assert repository.trees == [_HEAD, _BASE]


def test_unchanged_bitbucket_importer_with_revision_bound_ids_is_a_broken_reference() -> None:
    class BitbucketRepository(Repository):
        def sha(self, path: str) -> str:
            return sha256(f"bitbucket:{_REPOSITORY}:{_HEAD}:{path}".encode()).hexdigest()

        def base_sha(self, path: str) -> str:
            return sha256(f"bitbucket:{_REPOSITORY}:{_BASE}:{path}".encode()).hexdigest()

    original, diff_map = _removal("renamed_symbol", provider=VcsProviderName.BITBUCKET)
    repository = BitbucketRepository(original.files, base=original.base)

    result = _select(repository, diff_map, _load_hints("pkg/view.py"))

    assert len(result.places) == 1
    assert result.places[0].source.path == "pkg/view.py"
    assert result.places[0].source.reason == "rag_broken_reference"
    assert result.places[0].text == "from .store import load\ndef show(key):\n    return load(key)"
    assert (result.found, result.confirmed, dict(result.dropped)) == (1, 1, {})
    assert repository.read == ["pkg/store.py@base", "pkg/view.py"]


def test_revision_bound_importer_with_changed_content_is_not_a_broken_reference() -> None:
    class BitbucketRepository(Repository):
        def sha(self, path: str) -> str:
            return sha256(f"bitbucket:{_REPOSITORY}:{_HEAD}:{path}".encode()).hexdigest()

        def base_sha(self, path: str) -> str:
            return sha256(f"bitbucket:{_REPOSITORY}:{_BASE}:{path}".encode()).hexdigest()

    original, diff_map = _removal("renamed_symbol", provider=VcsProviderName.BITBUCKET)
    # A partial provider list cannot prove that the importer was unchanged.
    diff_map = replace(
        diff_map,
        file_list_complete=False,
        incomplete_reasons=("file_count_mismatch",),
    )
    repository = BitbucketRepository(
        original.files,
        base={**original.base, "pkg/view.py": "def show(key):\n    return key\n"},
    )

    result = _select(repository, diff_map, _load_hints("pkg/view.py"))

    assert result.places == ()
    assert (result.found, result.confirmed, dict(result.dropped)) == (
        1,
        0,
        {"rag_edge_unconfirmed": 1},
    )
    assert repository.read == ["pkg/store.py@base", "pkg/view.py", "pkg/view.py@base"]


def test_revision_bound_importer_base_read_at_cap_reports_rag_limit() -> None:
    class BitbucketRepository(Repository):
        def sha(self, path: str) -> str:
            return sha256(f"bitbucket:{_REPOSITORY}:{_HEAD}:{path}".encode()).hexdigest()

        def base_sha(self, path: str) -> str:
            return sha256(f"bitbucket:{_REPOSITORY}:{_BASE}:{path}".encode()).hexdigest()

    original, diff_map = _removal("renamed_symbol")
    repository = BitbucketRepository(original.files, base=original.base)
    store = BlobStore()
    reader = CachedBlobSource(
        repository_id=_REPOSITORY,
        source=repository,
        cache=SharedBlobCache(store),
        uow_factory=lambda: BlobCacheUow(store),
    )

    async def preload() -> None:
        for index in range(18):
            blob = f"{index:040x}"
            store.cached[BlobCacheKey(_REPOSITORY, blob)] = (BlobCacheStatus.HIT, "x")
            assert await reader.text(blob) == "x"

    asyncio.run(preload())
    result = _select(repository, diff_map, _load_hints("pkg/view.py"), store=store, reader=reader)

    assert result.places == ()
    assert result.omissions[-1] == RagOmission("rag_limit", "pkg/view.py")
    assert (result.found, result.confirmed, dict(result.dropped)) == (1, 0, {"rag_limit": 1})
    assert "pkg/view.py@base" not in repository.read


@pytest.mark.parametrize(
    ("base_store", "view", "found"),
    [
        # At the base `load` came from a module that did not exist; a stale graph still
        # names the edge to the file the PR deletes.
        (_PY_BASE_STORE, "from .legacy import load\n\n\ndef show(key):\n    return load(key)\n", 1),
        # The deleted file had no `load` at the base: the PR did not remove it.
        ("def save(item):\n    return item\n", _PY_VIEW, 0),
    ],
)
def test_an_import_broken_before_the_pr_is_not_blamed(
    base_store: str, view: str, found: int
) -> None:
    repository = Repository({"pkg/view.py": view}, base={"pkg/store.py": base_store})

    result = _select(repository, _diff_map(_deletion(repository)), _load_hints("pkg/view.py"))

    assert result.places == ()
    assert (result.found, result.confirmed) == (found, 0)
    assert dict(result.dropped) == ({"rag_edge_unconfirmed": 1} if found else {})


def test_an_importer_the_pr_fixes_is_no_candidate() -> None:
    # The PR moves the module and changes the import of `pkg/view.py` to the new path.
    view = "from .storage import load\n\n\ndef show(key):\n    return load(key)\n"
    repository = Repository(
        {"pkg/storage.py": _PY_BASE_STORE, "pkg/view.py": view},
        base={"pkg/store.py": _PY_BASE_STORE, "pkg/storage.py": None, "pkg/view.py": _PY_VIEW},
    )
    diff_map = _diff_map(
        _vcs_file(
            repository,
            "pkg/storage.py",
            None,
            additions=0,
            status="renamed",
            previous="pkg/store.py",
        ),
        _vcs_file(
            repository,
            "pkg/view.py",
            "@@ -1,1 +1,1 @@\n-from .store import load\n+from .storage import load",
            deletions=1,
        ),
    )

    result = _select(repository, diff_map, _load_hints("pkg/view.py"))

    assert (result.places, result.omissions, result.found) == ((), (), 0)


def _package_move() -> tuple[Repository, DiffMap]:
    """A PR that turns the module ``pkg/store.py`` into the package ``pkg/store``."""
    repository = Repository(
        {"pkg/store/__init__.py": _PY_BASE_STORE, "pkg/view.py": _PY_VIEW},
        base={"pkg/store.py": _PY_BASE_STORE, "pkg/store/__init__.py": None},
    )
    added = "@@ -0,0 +1,6 @@\n" + "\n".join(
        f"+{line}" for line in _PY_BASE_STORE.rstrip("\n").split("\n")
    )
    diff_map = _diff_map(
        _deletion(repository),
        _vcs_file(repository, "pkg/store/__init__.py", added, additions=6, status="added"),
    )
    return repository, diff_map


def test_an_import_that_still_resolves_at_head_is_no_broken_reference() -> None:
    # The same specifier still binds `load`, now from the package.
    repository, diff_map = _package_move()

    result = _select(repository, diff_map, _load_hints("pkg/view.py"))

    assert result.places == ()
    assert result.omissions == (RagOmission("rag_edge_unconfirmed", "pkg/view.py"),)
    assert (result.found, result.confirmed) == (1, 0)


def test_a_head_module_that_cannot_be_read_is_no_evidence_of_a_broken_reference() -> None:
    # The package lies under an ignored path: at head the import cannot be looked into,
    # which shows nothing about this PR.
    repository, diff_map = _package_move()

    result = _select(
        repository,
        diff_map,
        _load_hints("pkg/view.py"),
        denied=lambda path: path.startswith("pkg/store/"),
    )

    assert result.places == ()
    assert result.omissions == (RagOmission("rag_edge_unconfirmed", "pkg/view.py"),)
    assert (result.found, result.confirmed) == (1, 0)


@pytest.mark.parametrize(
    ("case", "view", "broken"),
    [
        # The module is gone: an incomplete listing cannot show its absence, neither for
        # a relative nor for an absolute import.
        ("deleted_file", _PY_VIEW, False),
        (
            "deleted_file",
            "from pkg.store import load\n\n\ndef show(key):\n    return load(key)\n",
            False,
        ),
        # The module is there without the name: that needs no complete listing.
        ("renamed_symbol", _PY_VIEW, True),
    ],
)
def test_on_an_incomplete_head_tree_only_a_module_without_the_name_shows_a_break(
    case: str, view: str, broken: bool
) -> None:
    repository, diff_map = _removal(case)
    repository.files["pkg/view.py"] = view
    repository.head_complete = False

    result = _select(repository, diff_map, _load_hints("pkg/view.py"))

    assert [place.source.reason for place in result.places] == (
        ["rag_broken_reference"] if broken else []
    )
    assert dict(result.dropped) == ({} if broken else {"rag_edge_unconfirmed": 1})


@pytest.mark.parametrize("reason", ["source_read_failed", "rag_limit"])
def test_a_removed_module_whose_base_cannot_be_read_is_counted_with_its_path(
    reason: str,
) -> None:
    fillers = {f"pkg/filler_{number:02d}.py": "" for number in range(20)}
    repository = Repository(
        {"pkg/view.py": _PY_VIEW, **fillers}, base={"pkg/store.py": _PY_BASE_STORE}
    )
    store = BlobStore()
    reader = CachedBlobSource(
        repository_id=_REPOSITORY,
        source=repository,
        cache=SharedBlobCache(store),
        uow_factory=lambda: BlobCacheUow(store),
    )
    if reason == "source_read_failed":
        repository.failing = frozenset({"pkg/store.py"})
    else:
        # The run's reader spent its reads already, as on an earlier attempt.

        async def spend() -> None:
            for path in fillers:
                await reader.text(repository.sha(path))

        asyncio.run(spend())
    graph = _load_hints("pkg/view.py")

    result = _select(repository, _diff_map(_deletion(repository)), graph, reader=reader)

    # Its exports are unknown, so none is asked; the file is counted, never lost.
    assert graph.reads == [(_REPOSITORY, ())]
    assert result.omissions == (RagOmission(reason, "pkg/store.py"),)
    assert (result.found, dict(result.dropped)) == (1, {reason: 1})


def test_base_extractions_take_at_most_ten_reads_and_leave_the_rest_to_candidates() -> None:
    kept = "def keep():\n    return 0\n"
    removing = [f"pkg/mod_{number:02d}.py" for number in range(12)]
    callers = [f"pkg/user_{number}.py" for number in range(9)]
    repository = Repository(
        {
            "pkg/store.py": _PY_STORE,
            **{path: kept for path in removing},
            **{path: _PY_USER for path in callers},
        },
        base={path: kept + "\n\ndef gone():\n    return 1\n" for path in removing},
    )
    patch = "@@ -1,6 +1,2 @@\n def keep():\n     return 0\n-\n-\n-def gone():\n-    return 1"
    diff_map = _diff_map(
        _vcs_file(repository, "pkg/store.py", _PY_STORE_PATCH),
        *(_vcs_file(repository, path, patch, additions=0, deletions=4) for path in removing),
    )

    result = _select(repository, diff_map, _store_hints(*callers))

    # Twelve base reads would leave eight, too few for the nine callers of `save`.
    assert len([item for item in repository.read if item.endswith("@base")]) == 10
    assert [place.source.path for place in result.places] == callers
    assert result.omissions == (
        RagOmission("rag_limit", "pkg/mod_10.py"),
        RagOmission("rag_limit", "pkg/mod_11.py"),
    )
    assert (result.found, result.confirmed, dict(result.dropped)) == (11, 9, {"rag_limit": 2})


@pytest.mark.parametrize(
    ("diff_old_sha", "unavailable", "reason", "trees"),
    [
        (None, False, "old_revision_unknown", [_HEAD]),
        (_BASE, True, "source_read_failed", [_HEAD, _BASE]),
    ],
)
def test_without_a_readable_base_the_importers_of_a_removed_module_are_dropped(
    diff_old_sha: str | None, unavailable: bool, reason: str, trees: list[str]
) -> None:
    repository = Repository(
        {"pkg/view.py": _PY_VIEW},
        base={"pkg/store.py": _PY_BASE_STORE},
        base_unavailable=unavailable,
    )
    diff_map = _diff_map(_deletion(repository), diff_old_sha=diff_old_sha)
    graph = Graph(
        {
            _REPOSITORY: (
                GraphHint("pkg/view.py", EdgeKind.IMPORTS, "pkg/store.py", ""),
                _uses("pkg/view.py", "pkg/store.py", "load"),
            )
        }
    )

    result = _select(repository, diff_map, graph)

    # The names the deleted module exported are unknown: the graph gives its importers.
    assert graph.reads == [(_REPOSITORY, (GraphTarget("pkg/store.py", ""),))]
    assert result.places == ()
    assert result.omissions == (RagOmission(reason, "pkg/view.py"),)
    assert (result.found, dict(result.dropped)) == (1, {reason: 1})
    assert repository.read == []
    assert repository.trees == trees


@pytest.mark.parametrize(
    ("diff_old_sha", "unavailable", "reason"),
    [(None, False, "old_revision_unknown"), (_BASE, True, "source_read_failed")],
)
def test_a_modified_source_whose_base_cannot_be_read_is_counted_with_its_path(
    diff_old_sha: str | None, unavailable: bool, reason: str
) -> None:
    repository = Repository(
        {"pkg/store.py": _PY_STORE, "pkg/view.py": _PY_VIEW},
        base={"pkg/store.py": _PY_BASE_STORE},
        base_unavailable=unavailable,
    )
    diff_map = _diff_map(
        _vcs_file(repository, "pkg/store.py", _PY_REMOVAL_PATCH, deletions=4),
        diff_old_sha=diff_old_sha,
    )
    graph = _load_hints("pkg/view.py")

    result = _select(repository, diff_map, graph)

    # The exports the modification removes are unknown: the file is counted, never lost.
    assert graph.reads == [(_REPOSITORY, (GraphTarget("pkg/store.py", "save"),))]
    assert result.places == ()
    assert result.omissions == (RagOmission(reason, "pkg/store.py"),)
    assert (result.found, dict(result.dropped)) == (1, {reason: 1})
    assert repository.read == []


def _generated(path: str) -> bool:
    return path.startswith("generated/")


@pytest.mark.parametrize("case", ["deleted", "moved_into_ignored"])
def test_a_removed_file_the_ignore_policy_denies_is_never_read_at_the_base(case: str) -> None:
    if case == "deleted":
        # The PR deletes an ignored module that a file it leaves alone imports.
        view = "from generated.store import load\n\n\ndef show(key):\n    return load(key)\n"
        repository = Repository({"pkg/view.py": view}, base={"generated/store.py": _PY_BASE_STORE})
        diff_map = _diff_map(_deletion(repository, "generated/store.py"))
        graph = Graph({_REPOSITORY: (_uses("pkg/view.py", "generated/store.py", "load"),)})
    else:
        # The PR moves a module into an ignored directory.
        repository = Repository(
            {"generated/store.py": _PY_BASE_STORE, "pkg/view.py": _PY_VIEW},
            base={"pkg/store.py": _PY_BASE_STORE, "generated/store.py": None},
        )
        moved = _vcs_file(
            repository,
            "generated/store.py",
            None,
            additions=0,
            status="renamed",
            previous="pkg/store.py",
        )
        diff_map = _diff_map(moved)
        graph = _load_hints("pkg/view.py")

    result = _select(repository, diff_map, graph, denied=_generated)

    # The base is never listed: a denied file is neither read nor named.
    assert repository.trees == [_HEAD]
    assert graph.reads == [(_REPOSITORY, ())]
    assert repository.read == []
    assert (result.places, result.omissions) == ((), ())
    assert (result.found, dict(result.dropped)) == (0, {})


@dataclass
class BaseTreeRaises(Repository):
    """A repository whose base listing raises ``error``."""

    error: Exception = field(default_factory=lambda: RuntimeError("base unavailable"))

    async def tree(self, revision: str) -> RevisionTree:
        if revision == _BASE:
            raise self.error
        return await super().tree(revision)


@pytest.mark.parametrize(
    "error",
    [
        RunCancelled("installation_unavailable"),
        RunFailure("deadline_exceeded", "the run deadline passed"),
        PolicyError(PolicyErrorCode.ACCESS_DENIED, "GitHub answered 403"),
    ],
    ids=["cancel", "deadline", "policy"],
)
def test_control_raised_by_the_base_listing_ends_the_selection(error: Exception) -> None:
    repository = BaseTreeRaises(
        {"pkg/view.py": _PY_VIEW}, base={"pkg/store.py": _PY_BASE_STORE}, error=error
    )

    with pytest.raises(type(error)) as caught:
        _select(repository, _diff_map(_deletion(repository)), _load_hints("pkg/view.py"))

    assert caught.value is error


def test_a_broken_reference_comes_before_a_reverse_edge() -> None:
    repository = Repository(
        {"pkg/store.py": _PY_STORE, "pkg/app.py": _PY_USER, "pkg/view.py": _PY_VIEW},
        base={"pkg/store.py": _PY_BASE_STORE},
    )
    diff_map = _diff_map(_vcs_file(repository, "pkg/store.py", _PY_REMOVAL_PATCH, deletions=4))
    graph = Graph(
        {
            _REPOSITORY: (
                _uses("pkg/app.py", "pkg/store.py", "save"),
                _uses("pkg/view.py", "pkg/store.py", "load"),
            )
        }
    )

    result = _select(repository, diff_map, graph)

    assert graph.reads == [
        (_REPOSITORY, (GraphTarget("pkg/store.py", "load"), GraphTarget("pkg/store.py", "save")))
    ]
    assert result.places == (
        _view_place(repository),
        RetrievedPlace(
            source=SourceRef(
                role=SourceRole.RETRIEVED_EXCERPT,
                repository_id=_REPOSITORY,
                revision=_HEAD,
                path="pkg/app.py",
                blob_sha=repository.sha("pkg/app.py"),
                origin_paths=(),
                side=DiffSide.RIGHT,
                ranges=(LineRange(1, 1), LineRange(4, 5)),
                reason="rag_reverse_edge",
            ),
            edge_kind=EdgeKind.USES_IMPORTED,
            index_commit_sha=_INDEX_COMMIT,
            signature=False,
            text="from .store import save\ndef run(item):\n    return save(item)",
            tokens=8,
        ),
    )
    assert (result.found, result.confirmed) == (2, 2)


def test_an_importer_of_a_removed_and_a_changed_export_shows_both() -> None:
    user = (
        "from .store import load, save\n\n\n"
        "def run(item):\n    return save(item)\n\n\n"
        "def show(key):\n    return load(key)\n"
    )
    repository = Repository(
        {"pkg/store.py": _PY_STORE, "pkg/app.py": user}, base={"pkg/store.py": _PY_BASE_STORE}
    )
    diff_map = _diff_map(_vcs_file(repository, "pkg/store.py", _PY_REMOVAL_PATCH, deletions=4))
    graph = Graph(
        {
            _REPOSITORY: (
                _uses("pkg/app.py", "pkg/store.py", "save"),
                _uses("pkg/app.py", "pkg/store.py", "load"),
            )
        }
    )

    result = _select(repository, diff_map, graph)

    # The broken use first, then the caller of the changed export, in one candidate.
    assert [
        (place.source.ranges, place.source.reason, len(place.definitions))
        for place in result.places
    ] == [
        ((LineRange(1, 1), LineRange(8, 9)), "rag_broken_reference", 1),
        ((LineRange(1, 1), LineRange(4, 5)), "rag_reverse_edge", 0),
    ]
    assert (result.found, result.confirmed) == (1, 1)


def test_a_broken_reference_shows_the_import_line_of_every_removed_binding() -> None:
    view = (
        "from .store import load\nimport os\nfrom .cache import fetch\n\n\n"
        "def show(key):\n    return fetch(load(key))\n"
    )
    repository = Repository(
        {"pkg/view.py": view},
        base={"pkg/store.py": _PY_BASE_STORE, "pkg/cache.py": "def fetch(key):\n    return key\n"},
    )
    diff_map = _diff_map(_deletion(repository), _deletion(repository, "pkg/cache.py"))
    graph = Graph(
        {
            _REPOSITORY: (
                _uses("pkg/view.py", "pkg/store.py", "load"),
                _uses("pkg/view.py", "pkg/cache.py", "fetch"),
            )
        }
    )

    result = _select(repository, diff_map, graph)

    # One place reads two removed bindings: it shows the import line of each.
    (place,) = result.places
    assert (place.source.reason, place.source.ranges) == (
        "rag_broken_reference",
        (LineRange(1, 1), LineRange(3, 3), LineRange(6, 7)),
    )
    assert {item.source.path for item in place.definitions} == {"pkg/cache.py", "pkg/store.py"}
    assert (result.found, result.confirmed) == (1, 1)


def test_a_broken_reference_that_also_calls_a_changed_export_shows_both_import_lines() -> None:
    view = (
        "from .store import load\nimport os\nfrom .util import helper\n\n\n"
        "def show(key):\n    return helper(load(key))\n"
    )
    util = "def helper(value):\n    value.check()\n    return value\n"
    repository = Repository(
        {"pkg/store.py": _PY_STORE, "pkg/util.py": util, "pkg/view.py": view},
        base={"pkg/store.py": _PY_BASE_STORE},
    )
    diff_map = _diff_map(
        _vcs_file(repository, "pkg/store.py", _PY_REMOVAL_PATCH, deletions=4),
        _vcs_file(
            repository,
            "pkg/util.py",
            "@@ -1,2 +1,3 @@\n def helper(value):\n+    value.check()\n     return value",
        ),
    )
    graph = Graph(
        {
            _REPOSITORY: (
                _uses("pkg/view.py", "pkg/store.py", "load"),
                _uses("pkg/view.py", "pkg/util.py", "helper"),
            )
        }
    )

    result = _select(repository, diff_map, graph)

    # One declaration is both: the broken place shows the import of every binding.
    (place,) = result.places
    assert (place.source.reason, place.source.ranges, len(place.definitions)) == (
        "rag_broken_reference",
        (LineRange(1, 1), LineRange(3, 3), LineRange(6, 7)),
        1,
    )
    assert (result.found, result.confirmed) == (1, 1)


def test_a_broken_reference_is_each_method_that_reads_the_name_not_its_class() -> None:
    view = (
        "from .store import load\n\n\n"
        "class View:\n"
        "    def show(self, key):\n        return load(key)\n\n"
        "    def other(self, key):\n        return load(key)\n"
    )
    repository = Repository({"pkg/view.py": view}, base={"pkg/store.py": _PY_BASE_STORE})

    result = _select(repository, _diff_map(_deletion(repository)), _load_hints("pkg/view.py"))

    # A class reads what its methods read: each method is a place, the class is not.
    assert [(place.source.ranges, place.source.reason) for place in result.places] == [
        ((LineRange(1, 1), LineRange(5, 6)), "rag_broken_reference"),
        ((LineRange(1, 1), LineRange(8, 9)), "rag_broken_reference"),
    ]
    assert (result.found, result.confirmed) == (1, 1)
