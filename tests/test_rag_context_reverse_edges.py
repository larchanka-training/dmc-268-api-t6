"""Graph RAG context: the reverse-edge candidates selected from the graph and confirmed at the
PR head (ADR 0001 §8.2, §9)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from uuid import UUID

import pytest

from app.modules.code_index.domain.enums import EdgeKind, IndexStatus, RagIndexStatus
from app.modules.reviews.application.context_provenance import LineRange, SourceRef, SourceRole
from app.modules.reviews.application.diff_map import (
    DiffMap,
    DiffSide,
)
from app.modules.reviews.application.graph_retriever import GraphHint, GraphTarget
from app.modules.reviews.application.rag_context import (
    CachedBlobSource,
    RagOmission,
    RetrievedPlace,
)
from tests.rag_context_fakes import (
    _HEAD,
    _INDEX_COMMIT,
    _PY_STORE,
    _PY_STORE_PATCH,
    _PY_USER,
    _REPOSITORY,
    BlobCacheUow,
    BlobStore,
    Graph,
    Repository,
    SharedBlobCache,
    _diff_map,
    _select,
    _store_hints,
    _uses,
    _vcs_file,
)


@dataclass(frozen=True)
class Language:
    """A PR that changes the exported ``save`` of one file, in one language, and the
    importer sources of the F6 cases."""

    changed: str
    source: str
    patch: str
    name: str
    candidate: str
    others: dict[str, str]
    importers: dict[str, str]


_PYTHON = Language(
    changed="pkg/store.py",
    source="def save(item):\n    item.check()\n    return item\n",
    patch="@@ -1,2 +1,3 @@\n def save(item):\n+    item.check()\n     return item",
    name="save",
    candidate="pkg/app.py",
    others={"pkg/other.py": "def save(item):\n    return item\n"},
    importers={
        "used": "from .store import save\n\n\ndef run(item):\n    return save(item)\n",
        "other_module": "from .other import save\n\n\ndef run(item):\n    return save(item)\n",
        "parameter": "from .store import save\n\n\ndef run(save):\n    return save(1)\n",
        "rebinding": (
            "from .store import save\n\n\ndef run(item):\n    save = print\n    return save(item)\n"
        ),
        "receiver": (
            "from .store import save\n\n\ndef run(repository):\n    return repository.save()\n"
        ),
    },
)


_TYPESCRIPT = Language(
    changed="src/store.ts",
    source="export function save(item: number): number {\n  check(item);\n  return item;\n}\n",
    patch=(
        "@@ -1,3 +1,4 @@\n export function save(item: number): number {\n"
        "+  check(item);\n   return item;\n }"
    ),
    name="save",
    candidate="src/app.ts",
    others={"src/other.ts": "export function save(item: number): number {\n  return item;\n}\n"},
    importers={
        "used": (
            'import { save } from "./store";\n\n'
            "export function run(item: number): number {\n  return save(item);\n}\n"
        ),
        "other_module": (
            'import { save } from "./other";\n\n'
            "export function run(item: number): number {\n  return save(item);\n}\n"
        ),
        "parameter": (
            'import { save } from "./store";\n\n'
            "export function run(save: Saver): number {\n  return save(1);\n}\n"
        ),
        "rebinding": (
            'import { save } from "./store";\n\n'
            "export function run(item: number): number {\n"
            "  const save = check;\n  return save(item);\n}\n"
        ),
        "receiver": (
            'import { save } from "./store";\n\n'
            "export function run(repository: Repository): number {\n"
            "  return repository.save();\n}\n"
        ),
    },
)


_GO = Language(
    changed="pkg/store/store.go",
    source="package store\n\nfunc Save(item int) int {\n\tcheck(item)\n\treturn item\n}\n",
    patch="@@ -3,3 +3,4 @@\n func Save(item int) int {\n+\tcheck(item)\n \treturn item\n }",
    name="Save",
    candidate="cmd/app/app.go",
    others={
        "go.mod": "module example.com/m\n\ngo 1.22\n",
        "pkg/other/other.go": "package other\n\nfunc Save(item int) int {\n\treturn item\n}\n",
    },
    importers={
        "used": (
            'package app\n\nimport "example.com/m/pkg/store"\n\n'
            "func Run(item int) int {\n\treturn store.Save(item)\n}\n"
        ),
        "other_module": (
            'package app\n\nimport "example.com/m/pkg/other"\n\n'
            "func Run(item int) int {\n\treturn other.Save(item)\n}\n"
        ),
        "parameter": (
            'package app\n\nimport "example.com/m/pkg/store"\n\n'
            "func Run(store Saver) int {\n\treturn store.Save(1)\n}\n"
        ),
        "rebinding": (
            'package app\n\nimport "example.com/m/pkg/store"\n\n'
            "func Run(item int) int {\n\tstore := open()\n\treturn store.Save(item)\n}\n"
        ),
        "receiver": (
            'package app\n\nimport "example.com/m/pkg/store"\n\n'
            "func Run(repository Saver) int {\n\treturn repository.Save()\n}\n"
        ),
    },
)


_LANGUAGES = {"python": _PYTHON, "typescript": _TYPESCRIPT, "go": _GO}


def _language_case(
    language: Language, importer: str, repository_id: UUID = _REPOSITORY
) -> tuple[Repository, DiffMap, Graph]:
    repository = Repository(
        {
            language.changed: language.source,
            language.candidate: language.importers[importer],
            **language.others,
        }
    )
    diff_map = _diff_map(
        _vcs_file(repository, language.changed, language.patch), repository_id=repository_id
    )
    graph = Graph({repository_id: (_uses(language.candidate, language.changed, language.name),)})
    return repository, diff_map, graph


@pytest.mark.parametrize(
    ("language", "ranges", "text"),
    [
        (
            "python",
            (LineRange(1, 1), LineRange(4, 5)),
            "from .store import save\ndef run(item):\n    return save(item)",
        ),
        (
            "typescript",
            (LineRange(1, 1), LineRange(3, 5)),
            'import { save } from "./store";\n'
            "export function run(item: number): number {\n  return save(item);\n}",
        ),
        (
            "go",
            (LineRange(3, 3), LineRange(5, 7)),
            'import "example.com/m/pkg/store"\n'
            "func Run(item int) int {\n\treturn store.Save(item)\n}",
        ),
    ],
)
def test_a_reverse_edge_confirmed_at_head_is_used(
    language: str, ranges: tuple[LineRange, ...], text: str
) -> None:
    case = _LANGUAGES[language]
    repository, diff_map, graph = _language_case(case, "used")

    result = _select(repository, diff_map, graph)

    assert graph.reads == [(_REPOSITORY, (GraphTarget(case.changed, case.name),))]
    assert result.places == (
        RetrievedPlace(
            source=SourceRef(
                role=SourceRole.RETRIEVED_EXCERPT,
                repository_id=_REPOSITORY,
                revision=_HEAD,
                path=case.candidate,
                blob_sha=repository.sha(case.candidate),
                origin_paths=(),
                side=DiffSide.RIGHT,
                ranges=ranges,
                reason="rag_reverse_edge",
            ),
            edge_kind=EdgeKind.USES_IMPORTED,
            index_commit_sha=_INDEX_COMMIT,
            signature=False,
            text=text,
            tokens=len(text.split()),
        ),
    )
    assert (result.found, result.confirmed, dict(result.dropped)) == (1, 1, {})
    assert result.omissions == ()
    # The changed file was loaded by L4: only the candidate is a RAG read.
    assert repository.read == [case.candidate]
    # The PR only adds lines, so it removes no export: the base is never listed.
    assert repository.trees == [_HEAD]


@pytest.mark.parametrize(
    ("language", "importer"),
    [
        (language, importer)
        for language in ("python", "typescript", "go")
        for importer in ("other_module", "parameter", "rebinding", "receiver")
    ],
)
def test_f6_a_name_the_head_does_not_bind_to_the_change_is_no_candidate(
    language: str, importer: str
) -> None:
    # The graph still names the edge; at head the name comes from another module, is
    # shadowed by a parameter, rebound locally, or read only through a receiver.
    case = _LANGUAGES[language]
    repository, diff_map, graph = _language_case(case, importer)

    result = _select(repository, diff_map, graph)

    assert result.places == ()
    assert result.omissions == (RagOmission("rag_edge_unconfirmed", case.candidate),)
    assert (result.found, result.confirmed) == (1, 0)
    assert dict(result.dropped) == {"rag_edge_unconfirmed": 1}


def _python_repository(**importers: str) -> tuple[Repository, DiffMap]:
    repository = Repository({"pkg/store.py": _PY_STORE, **importers})
    return repository, _diff_map(_vcs_file(repository, "pkg/store.py", _PY_STORE_PATCH))


def test_a_pr_file_and_a_definition_l4_shows_are_not_repeated() -> None:
    repository = Repository(
        {"pkg/store.py": _PY_STORE, "pkg/api.py": _PY_USER, "pkg/app.py": _PY_USER}
    )
    diff_map = _diff_map(
        _vcs_file(repository, "pkg/store.py", _PY_STORE_PATCH),
        _vcs_file(
            repository,
            "pkg/api.py",
            "@@ -1,1 +1,1 @@\n-from .store import x\n+from .store import save",
            deletions=1,
        ),
    )

    result = _select(
        repository,
        diff_map,
        _store_hints("pkg/api.py", "pkg/app.py"),
        # L4 already shows `run` of pkg/app.py: the changed code imports it.
        shown=frozenset({"aaaaaaaaaaaa:pkg/app.py#0"}),
    )

    assert result.places == ()
    assert result.omissions == ()
    # The PR file is no candidate at all; the importer whose place L4 shows is confirmed.
    assert (result.found, result.confirmed, dict(result.dropped)) == (1, 1, {})


def test_an_ignored_candidate_is_never_read_and_only_counted() -> None:
    repository, diff_map = _python_repository(
        **{"generated/app.py": _PY_USER, "pkg/app.py": _PY_USER}
    )

    result = _select(
        repository,
        diff_map,
        _store_hints("generated/app.py", "pkg/app.py"),
        denied=lambda path: path.startswith("generated/"),
    )

    assert repository.read == ["pkg/app.py"]
    assert [place.source.path for place in result.places] == ["pkg/app.py"]
    assert result.omissions == ()
    assert (result.found, result.confirmed, dict(result.dropped)) == (2, 1, {"excluded": 1})


def test_a_candidate_absent_from_the_head_tree_is_missing() -> None:
    repository, diff_map = _python_repository()

    result = _select(repository, diff_map, _store_hints("pkg/gone.py"))

    assert result.places == ()
    assert result.omissions == (RagOmission("rag_candidate_missing", "pkg/gone.py"),)
    assert dict(result.dropped) == {"rag_candidate_missing": 1}
    assert repository.read == []


def test_a_candidate_whose_blob_read_fails_is_source_read_failed() -> None:
    repository, diff_map = _python_repository(**{"pkg/app.py": _PY_USER})
    repository.failing = frozenset({"pkg/app.py"})

    result = _select(repository, diff_map, _store_hints("pkg/app.py"))

    assert result.places == ()
    assert result.omissions == (RagOmission("source_read_failed", "pkg/app.py"),)
    assert dict(result.dropped) == {"source_read_failed": 1}


def test_the_eleventh_file_is_over_the_limit_and_never_read() -> None:
    paths = [f"pkg/user_{number:02d}.py" for number in range(11)]
    repository, diff_map = _python_repository(**{path: _PY_USER for path in paths})

    result = _select(repository, diff_map, _store_hints(*paths))

    assert [place.source.path for place in result.places] == paths[:10]
    assert result.omissions == (RagOmission("rag_limit", "pkg/user_10.py"),)
    assert (result.found, result.confirmed, dict(result.dropped)) == (11, 10, {"rag_limit": 1})
    assert "pkg/user_10.py" not in repository.read


def test_the_fourth_place_of_a_file_is_over_the_limit() -> None:
    user = "from .store import save\n" + "".join(
        f"\n\ndef run_{number}(item):\n    return save(item)\n" for number in range(4)
    )
    repository, diff_map = _python_repository(**{"pkg/app.py": user})

    result = _select(repository, diff_map, _store_hints("pkg/app.py"))

    assert [place.source.ranges for place in result.places] == [
        (LineRange(1, 1), LineRange(4, 5)),
        (LineRange(1, 1), LineRange(8, 9)),
        (LineRange(1, 1), LineRange(12, 13)),
    ]
    assert result.omissions == (RagOmission("rag_limit", "pkg/app.py"),)
    assert (result.found, result.confirmed, dict(result.dropped)) == (1, 1, {})


def test_f5_the_graph_points_at_new_utils_but_head_imports_old_utils() -> None:
    repository = Repository(
        {
            "pkg/new_utils.py": "def helper(value):\n    value.check()\n    return value\n",
            "pkg/old_utils.py": "def helper(value):\n    return value\n",
            "pkg/app.py": (
                "from .old_utils import helper\n\n\ndef run(item):\n    return helper(item)\n"
            ),
        }
    )
    diff_map = _diff_map(
        _vcs_file(
            repository,
            "pkg/new_utils.py",
            "@@ -1,2 +1,3 @@\n def helper(value):\n+    value.check()\n     return value",
        )
    )
    graph = Graph({_REPOSITORY: (_uses("pkg/app.py", "pkg/new_utils.py", "helper"),)})

    result = _select(repository, diff_map, graph)

    assert result.places == ()
    assert result.omissions == (RagOmission("rag_edge_unconfirmed", "pkg/app.py"),)


def test_f5_the_ranges_are_the_head_lines_of_a_moved_declaration() -> None:
    # On the default branch `run` came first; at head another function precedes it.
    user = (
        "from .store import save\n\n\n"
        "def first(item):\n    return item\n\n\n"
        "def run(item):\n    return save(item)\n"
    )
    repository, diff_map = _python_repository(**{"pkg/app.py": user})

    result = _select(repository, diff_map, _store_hints("pkg/app.py"))

    assert [place.source.ranges for place in result.places] == [(LineRange(1, 1), LineRange(8, 9))]
    assert result.places[0].text == "from .store import save\ndef run(item):\n    return save(item)"


def test_a_declaration_too_large_to_show_whole_gives_its_signature() -> None:
    body = "".join(f"    step_{number} = save(item)\n" for number in range(200))
    user = f"from .store import save\n\n\ndef run(item):\n{body}    return item\n"
    repository, diff_map = _python_repository(**{"pkg/app.py": user})

    result = _select(repository, diff_map, _store_hints("pkg/app.py"))

    assert [(place.source.ranges, place.signature) for place in result.places] == [
        ((LineRange(1, 1), LineRange(4, 4)), True)
    ]
    assert result.places[0].text == "from .store import save\ndef run(item):"


def test_importers_of_a_changed_symbol_come_before_importers_of_a_changed_module() -> None:
    store = (
        "import os\n\n\ndef save(item):\n    item.check()\n    return item\n\n\n"
        "def load(key):\n    return key\n"
    )
    patch = (
        "@@ -1,3 +1,7 @@\n+import os\n+\n+\n def save(item):\n+    item.check()\n"
        "     return item\n "
    )
    repository = Repository(
        {
            "pkg/store.py": store,
            "pkg/app.py": _PY_USER,
            "pkg/view.py": "from .store import load\n\n\ndef show(key):\n    return load(key)\n",
        }
    )
    diff_map = _diff_map(_vcs_file(repository, "pkg/store.py", patch, additions=4))
    graph = Graph(
        {
            _REPOSITORY: (
                GraphHint("pkg/view.py", EdgeKind.IMPORTS, "pkg/store.py", ""),
                GraphHint("pkg/app.py", EdgeKind.IMPORTS, "pkg/store.py", ""),
                _uses("pkg/app.py", "pkg/store.py", "save"),
            )
        }
    )

    result = _select(repository, diff_map, graph)

    assert graph.reads == [
        (_REPOSITORY, (GraphTarget("pkg/store.py", "save"), GraphTarget("pkg/store.py", "")))
    ]
    assert [(place.source.path, place.edge_kind) for place in result.places] == [
        ("pkg/app.py", EdgeKind.USES_IMPORTED),
        ("pkg/view.py", EdgeKind.IMPORTS),
    ]
    assert [place.source.ranges for place in result.places] == [
        (LineRange(1, 1), LineRange(4, 5)),
        (LineRange(1, 1), LineRange(4, 5)),
    ]
    assert (result.found, result.confirmed) == (2, 2)


def test_a_python_submodule_binding_is_confirmed() -> None:
    repository = Repository(
        {
            "pkg/__init__.py": "",
            "pkg/store.py": _PY_STORE,
            "app/main.py": (
                "from pkg import store\n\n\ndef run(item):\n    return store.save(item)\n"
            ),
        }
    )
    diff_map = _diff_map(_vcs_file(repository, "pkg/store.py", _PY_STORE_PATCH))
    graph = Graph({_REPOSITORY: (_uses("app/main.py", "pkg/store.py", "save"),)})

    result = _select(repository, diff_map, graph)

    assert [(place.source.path, place.source.ranges) for place in result.places] == [
        ("app/main.py", (LineRange(1, 1), LineRange(4, 5)))
    ]


def test_a_python_submodule_import_is_confirmed_on_an_imports_edge() -> None:
    # The indexer gives `from pkg import store` an `imports` edge to the submodule.
    store = "import os\n\n\ndef save(item):\n    return item\n"
    repository = Repository(
        {
            "pkg/__init__.py": "",
            "pkg/store.py": store,
            "app/main.py": (
                "from pkg import store\n\n\ndef run(item):\n    return store.save(item)\n"
            ),
        }
    )
    diff_map = _diff_map(_vcs_file(repository, "pkg/store.py", "@@ -1,1 +1,2 @@\n+import os\n \n"))
    graph = Graph({_REPOSITORY: (GraphHint("app/main.py", EdgeKind.IMPORTS, "pkg/store.py", ""),)})

    result = _select(repository, diff_map, graph)

    assert graph.reads == [(_REPOSITORY, (GraphTarget("pkg/store.py", ""),))]
    assert [(place.source.path, place.source.ranges) for place in result.places] == [
        ("app/main.py", (LineRange(1, 1), LineRange(4, 5)))
    ]
    assert (result.found, result.confirmed, dict(result.dropped)) == (1, 1, {})


def _submodule_import(
    init: str, kind: EdgeKind = EdgeKind.IMPORTS, files: dict[str, str] | None = None
) -> tuple[Repository, DiffMap, Graph]:
    """``app/main.py`` reads ``store.save`` through ``from pkg import store``; the PR
    changes ``pkg/store.py`` at module level (an ``imports`` edge) or in ``save``."""
    module = kind is EdgeKind.IMPORTS
    repository = Repository(
        {
            "pkg/__init__.py": init,
            "pkg/store.py": "import os\n\n\ndef save(item):\n    return item\n"
            if module
            else _PY_STORE,
            "app/main.py": (
                "from pkg import store\n\n\ndef run(item):\n    return store.save(item)\n"
            ),
            **(files or {}),
        }
    )
    patch = "@@ -1,1 +1,2 @@\n+import os\n \n" if module else _PY_STORE_PATCH
    graph = Graph(
        {_REPOSITORY: (GraphHint("app/main.py", kind, "pkg/store.py", "" if module else "save"),)}
    )
    return repository, _diff_map(_vcs_file(repository, "pkg/store.py", patch)), graph


@pytest.mark.parametrize(
    ("kind", "init"),
    [
        (EdgeKind.IMPORTS, "def store(item):\n    return item\n"),
        (EdgeKind.IMPORTS, "store = None\n"),
        (EdgeKind.IMPORTS, "from .other import store\n"),
        (EdgeKind.IMPORTS, "from .other import x as store\n"),
        (EdgeKind.USES_IMPORTED, "from .other import store\n"),
        # Another module bound to the name (M3).
        (EdgeKind.IMPORTS, "import pkg.other as store\n"),
        (EdgeKind.IMPORTS, "import pkg as store\n"),
        (EdgeKind.IMPORTS, "import pkg.sub.store as store\n"),
        # A target the resolver does not resolve (M4).
        (EdgeKind.IMPORTS, "from external_lib import store\n"),
        (EdgeKind.IMPORTS, "import external_lib as store\n"),
        (EdgeKind.IMPORTS, "from .. import store\n"),
        # A submodule of another package, or another submodule (M5).
        (EdgeKind.IMPORTS, "from pkg2 import store\n"),
        (EdgeKind.IMPORTS, "from . import other as store\n"),
        (EdgeKind.IMPORTS, "from .sub import store\n"),
    ],
    ids=[
        "imports-def",
        "imports-assign",
        "imports-reimport",
        "imports-alias",
        "uses-reimport",
        "module-other",
        "module-package",
        "module-nested",
        "external-from",
        "external-module",
        "parent-relative",
        "other-package",
        "other-submodule",
        "nested-package",
    ],
)
def test_a_package_that_binds_the_name_itself_does_not_confirm_the_submodule(
    kind: EdgeKind, init: str
) -> None:
    repository, diff_map, graph = _submodule_import(
        init,
        kind,
        {
            "pkg/other.py": "def store(item):\n    return item\n",
            "pkg/sub/__init__.py": "",
            "pkg/sub/store.py": "def save(item):\n    return item\n",
            "pkg2/__init__.py": "",
            "pkg2/store.py": "def save(item):\n    return item\n",
        },
    )

    result = _select(repository, diff_map, graph)

    # `from pkg import store` binds what the package names `store`, not the submodule.
    assert result.places == ()
    assert result.omissions == (RagOmission("rag_edge_unconfirmed", "app/main.py"),)
    assert (result.found, result.confirmed) == (1, 0)


def test_a_method_named_like_the_submodule_is_no_rebind() -> None:
    repository, diff_map, graph = _submodule_import(
        "class Registry:\n    def store(self):\n        return 1\n"
    )

    result = _select(repository, diff_map, graph)

    # Only a top-level name of the package can stand in for the submodule.
    assert [place.source.path for place in result.places] == ["app/main.py"]
    assert (result.found, result.confirmed) == (1, 1)


@pytest.mark.parametrize(
    "main",
    [
        "from pkg import store\n\n\ndef run(item):\n    return store.helper(item)\n",
        "from pkg.store import helper\n\n\ndef run(item):\n    return helper(item)\n",
    ],
    ids=["submodule", "named"],
)
def test_a_uses_imported_edge_needs_the_changed_export_itself(main: str) -> None:
    store = (
        "def save(item):\n    item.check()\n    return item\n\n\n"
        "def helper(item):\n    return item\n"
    )
    repository, diff_map, graph = _submodule_import(
        "", EdgeKind.USES_IMPORTED, {"pkg/store.py": store, "app/main.py": main}
    )

    result = _select(repository, diff_map, graph)

    # The edge is about `save`: a read of `helper` from the same module confirms nothing.
    assert result.places == ()
    assert result.omissions == (RagOmission("rag_edge_unconfirmed", "app/main.py"),)
    assert (result.found, result.confirmed) == (1, 0)


def test_a_package_the_ignore_policy_denies_does_not_confirm_the_submodule() -> None:
    repository, diff_map, graph = _submodule_import("")

    result = _select(repository, diff_map, graph, denied=lambda path: path == "pkg/__init__.py")

    assert result.places == ()
    assert result.omissions == (RagOmission("rag_edge_unconfirmed", "app/main.py"),)
    assert "pkg/__init__.py" not in repository.read


def test_a_package_past_the_read_limit_does_not_confirm_the_submodule() -> None:
    fillers = {f"pkg/filler_{number:02d}.py": "" for number in range(19)}
    repository, diff_map, graph = _submodule_import("", files=fillers)
    store = BlobStore()
    reader = CachedBlobSource(
        repository_id=_REPOSITORY,
        source=repository,
        cache=SharedBlobCache(store),
        uow_factory=lambda: BlobCacheUow(store),
    )

    # The run's reader spent 19 reads: the candidate is the 20th, the package would be
    # the 21st.
    async def spend() -> None:
        for path in fillers:
            await reader.text(repository.sha(path))

    asyncio.run(spend())

    result = _select(repository, diff_map, graph, reader=reader)

    assert result.places == ()
    assert result.omissions == (RagOmission("rag_limit", "app/main.py"),)
    assert dict(result.dropped) == {"rag_limit": 1}
    assert "pkg/__init__.py" not in repository.read


_OTHER = {"pkg/other.py": "def store(item):\n    return item\n"}


@pytest.mark.parametrize(
    "init",
    [
        "from . import store\n",
        "from . import store as store\n",
        "from pkg import store\n",
        "import pkg.store as store\n",
        "from .other import store as s2\n",
    ],
    ids=["relative", "relative-alias", "absolute", "module-alias", "other-name"],
)
def test_a_package_that_imports_its_own_submodule_still_binds_it(init: str) -> None:
    repository, diff_map, graph = _submodule_import(init, files=_OTHER)

    result = _select(repository, diff_map, graph)

    # The package binds `store` to the submodule itself, or binds another name.
    assert [place.source.path for place in result.places] == ["app/main.py"]
    assert (result.found, result.confirmed) == (1, 1)


@pytest.mark.parametrize(
    "form",
    [
        "from . import store\n",
        "from . import store as store\n",
        "from pkg import store\n",
        "import pkg.store as store\n",
    ],
    ids=["relative", "relative-alias", "absolute", "module-alias"],
)
def test_a_package_init_that_reads_its_own_submodule_is_confirmed(form: str) -> None:
    init = f"{form}\n\ndef run(item):\n    return store.save(item)\n"
    repository = Repository(
        {
            "pkg/__init__.py": init,
            "pkg/store.py": "import os\n\n\ndef save(item):\n    return item\n",
        }
    )
    diff_map = _diff_map(_vcs_file(repository, "pkg/store.py", "@@ -1,1 +1,2 @@\n+import os\n \n"))
    graph = Graph(
        {_REPOSITORY: (GraphHint("pkg/__init__.py", EdgeKind.IMPORTS, "pkg/store.py", ""),)}
    )

    result = _select(repository, diff_map, graph)

    assert [place.source.ranges for place in result.places] == [(LineRange(1, 1), LineRange(4, 5))]
    assert (result.found, result.confirmed) == (1, 1)


@pytest.mark.parametrize(
    ("main", "store"),
    [
        ("from pkg import store\n\n\ndef run(item):\n    return register(store)\n", None),
        (
            "from pkg import store\n\n\ndef run(item):\n    return store.save(item)\n",
            "import os\n\nfrom .impl import save\n",
        ),
        ("from pkg import store\n\n\ndef run(item):\n    return store.helper(item)\n", None),
        ("from pkg import store as st\n\n\ndef run(item):\n    return st.save(item)\n", None),
    ],
    ids=["bare-module", "reexported-member", "undeclared-member", "importer-alias"],
)
def test_an_imports_edge_to_a_submodule_is_confirmed_by_the_module_it_binds(
    main: str, store: str | None
) -> None:
    files = {"app/main.py": main, "pkg/impl.py": "def save(item):\n    return item\n"}
    if store is not None:
        files["pkg/store.py"] = store
    repository, diff_map, graph = _submodule_import("", files=files)

    result = _select(repository, diff_map, graph)

    # An `imports` edge is about the module: no member has to resolve in it.
    assert [place.source.path for place in result.places] == ["app/main.py"]
    assert (result.found, result.confirmed) == (1, 1)


def test_a_namespace_package_without_an_init_reaches_no_submodule() -> None:
    repository, diff_map, graph = _submodule_import("")
    del repository.files["pkg/__init__.py"]

    result = _select(repository, diff_map, graph)

    # Without `__init__.py` the resolver resolves no `pkg`: the edge stays unconfirmed.
    assert result.places == ()
    assert result.omissions == (RagOmission("rag_edge_unconfirmed", "app/main.py"),)
    assert repository.read == ["app/main.py"]


_MODULE_EDGES = {
    "python-module": (
        {
            "pkg/store.py": "import os\n\n\ndef save(item):\n    return item\n",
            "app/main.py": (
                "import pkg.store\n\n\ndef run(item):\n    return pkg.store.save(item)\n"
            ),
        },
        "pkg/store.py",
        "@@ -1,1 +1,2 @@\n+import os\n \n",
        "app/main.py",
    ),
    "python-named": (
        {
            "pkg/store.py": "import os\n\n\ndef save(item):\n    return item\n",
            "pkg/app.py": "from .store import save\n\n\ndef run(item):\n    return save(item)\n",
        },
        "pkg/store.py",
        "@@ -1,1 +1,2 @@\n+import os\n \n",
        "pkg/app.py",
    ),
    "python-facade": (
        {
            "pkg/store.py": "import os\n\nfrom .impl import save\n",
            "pkg/impl.py": "def save(item):\n    return item\n",
            "pkg/app.py": "from .store import save\n\n\ndef run(item):\n    return save(item)\n",
        },
        "pkg/store.py",
        "@@ -1,1 +1,2 @@\n+import os\n \n",
        "pkg/app.py",
    ),
    "typescript-default": (
        {
            "src/store.ts": (
                'import { check } from "./check";\n\n'
                "export default function store(item: number): number {\n  return item;\n}\n"
            ),
            "src/app.ts": (
                'import store from "./store";\n\n'
                "export function run(item: number): number {\n  return store(item);\n}\n"
            ),
        },
        "src/store.ts",
        '@@ -1,1 +1,2 @@\n+import { check } from "./check";\n \n',
        "src/app.ts",
    ),
    "go-package": (
        {
            "go.mod": "module example.com/m\n\ngo 1.22\n",
            "pkg/store/store.go": (
                'package store\n\nimport "os"\n\nfunc Save(item int) int {\n\treturn item\n}\n'
            ),
            "cmd/app/app.go": (
                'package app\n\nimport "example.com/m/pkg/store"\n\n'
                "func Run(item int) int {\n\treturn store.Save(item)\n}\n"
            ),
        },
        "pkg/store/store.go",
        '@@ -1,2 +1,4 @@\n package store\n \n+import "os"\n+',
        "cmd/app/app.go",
    ),
}


@pytest.mark.parametrize("case", sorted(_MODULE_EDGES))
def test_an_imports_edge_to_the_module_itself_is_confirmed_by_its_path(case: str) -> None:
    files, changed, patch, candidate = _MODULE_EDGES[case]
    repository = Repository(dict(files))
    diff_map = _diff_map(_vcs_file(repository, changed, patch))
    graph = Graph({_REPOSITORY: (GraphHint(candidate, EdgeKind.IMPORTS, changed, ""),)})

    result = _select(repository, diff_map, graph)

    assert graph.reads == [(_REPOSITORY, (GraphTarget(changed, ""),))]
    assert [place.source.path for place in result.places] == [candidate]
    assert (result.found, result.confirmed) == (1, 1)


def test_the_graph_is_asked_under_the_default_branch_path_of_a_renamed_file() -> None:
    repository = Repository({"pkg/store.py": _PY_STORE})
    diff_map = _diff_map(
        _vcs_file(
            repository,
            "pkg/store.py",
            _PY_STORE_PATCH,
            status="renamed",
            previous="pkg/old_store.py",
        )
    )
    graph = Graph()

    _select(repository, diff_map, graph)

    assert graph.reads == [(_REPOSITORY, (GraphTarget("pkg/old_store.py", "save"),))]


def test_targets_past_two_hundred_are_not_asked_and_count_as_rag_limit() -> None:
    source = "".join(f"def f{number}():\n    return {number}\n" for number in range(201))
    lines = source.rstrip("\n").split("\n")
    patch = f"@@ -0,0 +1,{len(lines)} @@\n" + "\n".join(f"+{line}" for line in lines)
    repository = Repository({"pkg/many.py": source})
    diff_map = _diff_map(
        _vcs_file(repository, "pkg/many.py", patch, additions=len(lines), status="added")
    )
    graph = Graph()

    result = _select(repository, diff_map, graph)

    [(_, targets)] = graph.reads
    assert len(targets) == 200
    assert (targets[0], targets[-1]) == (
        GraphTarget("pkg/many.py", "f0"),
        GraphTarget("pkg/many.py", "f199"),
    )
    assert result.omissions == (RagOmission("rag_limit", None, 1),)
    assert (result.found, dict(result.dropped)) == (1, {"rag_limit": 1})


def test_an_index_not_usable_in_the_graph_read_selects_and_reads_nothing() -> None:
    repository, diff_map = _python_repository(**{"pkg/app.py": _PY_USER})
    graph = _store_hints("pkg/app.py")
    graph.status = IndexStatus.EMPTY

    result = _select(repository, diff_map, graph)

    assert result.index.status is RagIndexStatus.EMPTY
    assert (result.places, result.omissions, result.found) == ((), (), 0)
    assert repository.read == []


def test_two_repositories_with_the_same_paths_stay_apart() -> None:
    first, second = UUID(int=11), UUID(int=12)
    graph = Graph(
        {
            first: (_uses("pkg/app.py", "pkg/store.py", "save"),),
            second: (_uses("pkg/tool.py", "pkg/store.py", "save"),),
        }
    )
    results = []
    stores = []
    for repository_id in (first, second):
        repository = Repository(
            {"pkg/store.py": _PY_STORE, "pkg/app.py": _PY_USER, "pkg/tool.py": _PY_USER}
        )
        diff_map = _diff_map(
            _vcs_file(repository, "pkg/store.py", _PY_STORE_PATCH), repository_id=repository_id
        )
        store = BlobStore()
        stores.append(store)
        results.append(_select(repository, diff_map, graph, store=store))

    assert [reads[0] for reads in graph.reads] == [first, second]
    assert [
        [(place.source.repository_id, place.source.path) for place in result.places]
        for result in results
    ] == [[(first, "pkg/app.py")], [(second, "pkg/tool.py")]]
    assert [[key.repository_id for key, _, _ in store.puts] for store in stores] == [
        [first],
        [second],
    ]


def test_a_place_is_the_innermost_declaration_that_reads_the_name() -> None:
    # A class reads what its methods read: the method is the place, not the class.
    user = (
        "from .store import save\n\n\n"
        "class Service:\n"
        "    def run(self, item):\n        return save(item)\n\n"
        "    def other(self):\n        return 0\n"
    )
    repository, diff_map = _python_repository(**{"pkg/app.py": user})

    result = _select(repository, diff_map, _store_hints("pkg/app.py"))

    assert [place.source.ranges for place in result.places] == [(LineRange(1, 1), LineRange(5, 6))]


_PY_BROKEN = "\n\ndef broken(:\n    pass\n"


_GO_BROKEN = {"pkg/store/broken.go": "package store\n\nfunc Broken( {\n}\n"}


_GO_GENERATED = "pkg/store/gen.go"


_PY_NAMED, _, _PY_MODULE_PATCH, _ = _MODULE_EDGES["python-named"]


_PY_DOTTED = _MODULE_EDGES["python-module"][0]


_GO_PACKAGE, _, _GO_MODULE_PATCH, _ = _MODULE_EDGES["go-package"]


_GO_USED = {_GO.changed: _GO.source, _GO.candidate: _GO.importers["used"], **_GO.others}


_REFUSED_TARGETS: dict[str, tuple[dict[str, str], str, str, str, str, EdgeKind]] = {
    # The changed module itself has a syntax error, also where no resolution reads it.
    "python-imports": (
        {**_PY_NAMED, "pkg/store.py": _PY_NAMED["pkg/store.py"] + _PY_BROKEN},
        "pkg/store.py",
        _PY_MODULE_PATCH,
        "pkg/app.py",
        "",
        EdgeKind.IMPORTS,
    ),
    "python-imports-dotted": (
        {**_PY_DOTTED, "pkg/store.py": _PY_DOTTED["pkg/store.py"] + _PY_BROKEN},
        "pkg/store.py",
        _PY_MODULE_PATCH,
        "app/main.py",
        "",
        EdgeKind.IMPORTS,
    ),
    "python-uses-imported": (
        {"pkg/store.py": _PY_STORE + _PY_BROKEN, "pkg/app.py": _PY_USER},
        "pkg/store.py",
        _PY_STORE_PATCH,
        "pkg/app.py",
        "save",
        EdgeKind.USES_IMPORTED,
    ),
    # Another member of the changed Go package has one, or is excluded.
    "go-imports": (
        {**_GO_PACKAGE, **_GO_BROKEN},
        "pkg/store/store.go",
        _GO_MODULE_PATCH,
        "cmd/app/app.go",
        "",
        EdgeKind.IMPORTS,
    ),
    "go-uses-imported": (
        {**_GO_USED, **_GO_BROKEN},
        _GO.changed,
        _GO.patch,
        _GO.candidate,
        _GO.name,
        EdgeKind.USES_IMPORTED,
    ),
    "go-excluded-member": (
        {**_GO_PACKAGE, _GO_GENERATED: "package store\n\nfunc Gen() int {\n\treturn 1\n}\n"},
        "pkg/store/store.go",
        _GO_MODULE_PATCH,
        "cmd/app/app.go",
        "",
        EdgeKind.IMPORTS,
    ),
}


@pytest.mark.parametrize("case", sorted(_REFUSED_TARGETS))
def test_a_syntax_error_or_a_refused_package_confirms_no_edge(case: str) -> None:
    files, changed, patch, candidate, name, kind = _REFUSED_TARGETS[case]
    repository = Repository(dict(files))
    diff_map = _diff_map(_vcs_file(repository, changed, patch))
    graph = Graph({_REPOSITORY: (GraphHint(candidate, kind, changed, name),)})

    result = _select(repository, diff_map, graph, denied=lambda path: path == _GO_GENERATED)

    assert graph.reads == [(_REPOSITORY, (GraphTarget(changed, name),))]
    assert result.places == ()
    assert result.omissions == (RagOmission("rag_edge_unconfirmed", candidate),)
    assert (result.found, result.confirmed) == (1, 0)


@pytest.mark.parametrize(
    ("files", "changed", "patch", "candidate"),
    [
        (
            {
                "pkg/store.py": "import os\n\n\ndef save(item):\n    return item\n",
                "pkg/app.py": "from .store import *\n\n\ndef run(item):\n    return save(item)\n",
            },
            "pkg/store.py",
            "@@ -1,1 +1,2 @@\n+import os\n \n",
            "pkg/app.py",
        ),
        (
            {
                **_GO_PACKAGE,
                "cmd/app/app.go": (
                    'package app\n\nimport . "example.com/m/pkg/store"\n\n'
                    "func Run(item int) int {\n\treturn Save(item)\n}\n"
                ),
            },
            "pkg/store/store.go",
            _GO_MODULE_PATCH,
            "cmd/app/app.go",
        ),
    ],
    ids=["python-wildcard", "go-dot-import"],
)
def test_a_wildcard_or_dot_import_confirms_no_imports_edge(
    files: dict[str, str], changed: str, patch: str, candidate: str
) -> None:
    # The resolver reports such a use by a guess, which is no evidence of the edge.
    repository = Repository(dict(files))
    diff_map = _diff_map(_vcs_file(repository, changed, patch))
    graph = Graph({_REPOSITORY: (GraphHint(candidate, EdgeKind.IMPORTS, changed, ""),)})

    result = _select(repository, diff_map, graph)

    assert result.places == ()
    assert result.omissions == (RagOmission("rag_edge_unconfirmed", candidate),)
    assert (result.found, result.confirmed) == (1, 0)


def test_an_importer_of_a_changed_module_that_rebinds_the_name_is_unconfirmed() -> None:
    store = "import os\n\n\ndef load(key):\n    return key\n"
    repository = Repository(
        {
            "pkg/store.py": store,
            "pkg/view.py": "from .store import load\n\n\ndef show(load):\n    return load(1)\n",
        }
    )
    diff_map = _diff_map(_vcs_file(repository, "pkg/store.py", "@@ -1,1 +1,2 @@\n+import os\n \n"))
    graph = Graph({_REPOSITORY: (GraphHint("pkg/view.py", EdgeKind.IMPORTS, "pkg/store.py", ""),)})

    result = _select(repository, diff_map, graph)

    assert graph.reads == [(_REPOSITORY, (GraphTarget("pkg/store.py", ""),))]
    assert result.places == ()
    assert result.omissions == (RagOmission("rag_edge_unconfirmed", "pkg/view.py"),)


def test_a_module_the_edge_is_not_about_is_never_read() -> None:
    user = (
        "from .store import save\nfrom .text import shout\n\n\n"
        "def run(item):\n    return shout(save(item))\n"
    )
    repository, diff_map = _python_repository(
        **{"pkg/app.py": user, "pkg/text.py": "def shout(value):\n    return value\n"}
    )

    result = _select(repository, diff_map, _store_hints("pkg/app.py"))

    assert [place.source.ranges for place in result.places] == [(LineRange(1, 1), LineRange(5, 6))]
    assert repository.read == ["pkg/app.py"]


def test_a_place_shows_the_import_line_of_every_confirmed_binding() -> None:
    util = "def helper(value):\n    value.check()\n    return value\n"
    user = (
        "from .store import save\nimport os\nfrom .util import helper\n\n\n"
        "def run(item):\n    return helper(save(item))\n"
    )
    repository = Repository({"pkg/store.py": _PY_STORE, "pkg/util.py": util, "pkg/app.py": user})
    diff_map = _diff_map(
        _vcs_file(repository, "pkg/store.py", _PY_STORE_PATCH),
        _vcs_file(
            repository,
            "pkg/util.py",
            "@@ -1,2 +1,3 @@\n def helper(value):\n+    value.check()\n     return value",
        ),
    )
    graph = Graph(
        {
            _REPOSITORY: (
                _uses("pkg/app.py", "pkg/store.py", "save"),
                _uses("pkg/app.py", "pkg/util.py", "helper"),
            )
        }
    )

    result = _select(repository, diff_map, graph)

    # One place reads two changed bindings: it shows the import line of each.
    (place,) = result.places
    assert place.source.ranges == (LineRange(1, 1), LineRange(3, 3), LineRange(6, 7))
    assert place.text == (
        "from .store import save\nfrom .util import helper\n"
        "def run(item):\n    return helper(save(item))"
    )
    assert (result.found, result.confirmed) == (1, 1)


def test_a_resolution_stopped_by_the_read_limit_is_rag_limit() -> None:
    # 19 unconfirmed candidates spend 19 reads; the 20th read is the candidate itself,
    # and its import needs `pkg/__init__.py`, which would be the 21st.
    receivers = [f"pkg/user_{number:02d}.py" for number in range(19)]
    receiver = "from .store import save\n\n\ndef run(repository):\n    return repository.save()\n"
    repository = Repository(
        {
            "pkg/__init__.py": "",
            "pkg/store.py": _PY_STORE,
            "app/main.py": (
                "from pkg import store\n\n\ndef run(item):\n    return store.save(item)\n"
            ),
            **{path: receiver for path in receivers},
        }
    )
    diff_map = _diff_map(_vcs_file(repository, "pkg/store.py", _PY_STORE_PATCH))
    graph = Graph(
        {
            _REPOSITORY: tuple(
                _uses(path, "pkg/store.py", "save") for path in [*receivers, "app/main.py"]
            )
        }
    )

    result = _select(repository, diff_map, graph)

    assert result.places == ()
    assert result.omissions[-1] == RagOmission("rag_limit", "app/main.py")
    assert dict(result.dropped) == {"rag_edge_unconfirmed": 19, "rag_limit": 1}
    assert len(repository.read) == 20
    assert "pkg/__init__.py" not in repository.read


def test_a_candidate_past_the_read_limit_is_rag_limit_with_its_path() -> None:
    # 20 unconfirmed candidates take no file slot but spend the 20 reads of the run.
    paths = [f"pkg/user_{number:02d}.py" for number in range(21)]
    receiver = "from .store import save\n\n\ndef run(repository):\n    return repository.save()\n"
    repository, diff_map = _python_repository(**{path: receiver for path in paths})

    result = _select(repository, diff_map, _store_hints(*paths))

    assert result.places == ()
    assert result.omissions[-1] == RagOmission("rag_limit", "pkg/user_20.py")
    assert dict(result.dropped) == {"rag_edge_unconfirmed": 20, "rag_limit": 1}
    assert "pkg/user_20.py" not in repository.read
