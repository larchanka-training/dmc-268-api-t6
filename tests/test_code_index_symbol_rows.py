"""Index rows of one parsed file: names, disambiguators and the row filter (#142)."""

from __future__ import annotations

import pytest

from app.modules.code_index.application.index_store import (
    ImportRecord,
    IndexedEdge,
    IndexedFile,
    IndexedSymbol,
)
from app.modules.code_index.application.symbol_rows import file_rows, symbol_rows
from app.modules.code_index.domain.enums import EdgeKind
from app.modules.code_index.domain.keys import (
    MAX_INDEX_KEY_BYTES,
    MAX_NAME_CHARS,
    MAX_PATH_CHARS,
    SymbolKey,
)
from app.modules.code_index.infrastructure.index_store import _validate
from app.modules.code_index.infrastructure.models import (
    CodeIndexEdge,
    CodeIndexFile,
    CodeIndexImport,
    CodeIndexSymbol,
)
from app.modules.reviews.application.ast_resolution import ImportResolution
from app.modules.reviews.application.ast_symbols import (
    AstFacts,
    AstLanguage,
    AstSymbol,
    SymbolKind,
    language_for_path,
)
from app.modules.reviews.application.context_provenance import LineRange
from app.modules.reviews.infrastructure.tree_sitter_facts import extract_facts

_SHA = "b" * 40
RESOLVED = ImportResolution.RESOLVED


def _facts(path: str, source: str) -> AstFacts:
    language = language_for_path(path)
    assert language is not None
    return extract_facts(language, source.encode(), cpu_seconds=5.0)


def _names(path: str, source: str) -> list[tuple[str, SymbolKind, int]]:
    return [
        (row.key.qualified_name, row.key.kind, row.key.disambiguator)
        for row in symbol_rows(path, _facts(path, source))
    ]


def test_python_names_follow_the_enclosing_chain() -> None:
    source = (
        "class Outer:\n"
        "    class Inner:\n"
        "        def run(self):\n"
        "            pass\n"
        "\n"
        "    def run(self):\n"
        "        pass\n"
        "\n"
        "def helper():\n"
        "    pass\n"
    )

    # The extractor does not descend into a nested class body.
    assert _names("pkg/a.py", source) == [
        ("Outer", SymbolKind.CLASS, 0),
        ("Outer.Inner", SymbolKind.CLASS, 0),
        ("Outer.run", SymbolKind.METHOD, 0),
        ("helper", SymbolKind.FUNCTION, 0),
    ]


def test_duplicate_declarations_get_disambiguators_in_source_order() -> None:
    source = "def f():\n    return 1\n\nclass f:\n    pass\n\ndef f():\n    return 2\n"

    assert _names("a.py", source) == [
        ("f", SymbolKind.FUNCTION, 0),
        ("f", SymbolKind.CLASS, 0),
        ("f", SymbolKind.FUNCTION, 1),
    ]
    rows = symbol_rows("a.py", _facts("a.py", source))
    assert [(row.start_line, row.end_line) for row in rows] == [(1, 2), (4, 5), (7, 8)]
    assert rows[0].key == SymbolKey("a.py", "f", SymbolKind.FUNCTION, 0)


def test_go_method_is_named_by_its_receiver() -> None:
    source = (
        "package srv\n\n"
        "type Server struct{}\n\n"
        "func (s *Server) Run() error { return nil }\n\n"
        "func (c Client) Run() {}\n\n"
        "func Run() {}\n"
    )

    assert _names("srv/server.go", source) == [
        ("Server", SymbolKind.TYPE, 0),
        ("Server.Run", SymbolKind.METHOD, 0),
        ("Client.Run", SymbolKind.METHOD, 0),
        ("Run", SymbolKind.FUNCTION, 0),
    ]


def test_an_anonymous_default_export_is_named_default() -> None:
    source = "export default class {\n  run() { return 1 }\n}\nexport function named() {}\n"

    assert _names("src/a.ts", source) == [
        ("default", SymbolKind.CLASS, 0),
        ("default.run", SymbolKind.METHOD, 0),
        ("named", SymbolKind.FUNCTION, 0),
    ]
    assert _names("src/b.js", "export default function () { return 2 }\n") == [
        ("default", SymbolKind.FUNCTION, 0)
    ]
    # A named default export keeps its own name.
    assert _names("src/c.ts", "export default function main() {}\n") == [
        ("main", SymbolKind.FUNCTION, 0)
    ]


def _symbol(index: int, name: str | None, kind: SymbolKind, enclosing: int | None) -> AstSymbol:
    return AstSymbol(
        index=index,
        kind=kind,
        name=name,
        declaration_kind=None,
        declaration_range=LineRange(index + 1, index + 1),
        signature_ranges=(),
        docstring_ranges=(),
        enclosing=enclosing,
        exported=False,
        complete=True,
        references=(),
        member_references=(),
        local_bindings=(),
    )


def test_an_unnamed_symbol_outside_a_default_export_is_dropped_with_its_descendants() -> None:
    facts = AstFacts(
        language=AstLanguage.TYPESCRIPT,
        profile_version="p",
        symbols=(
            _symbol(0, None, SymbolKind.CLASS, None),
            _symbol(1, "inner", SymbolKind.METHOD, 0),
            _symbol(2, "deeper", SymbolKind.METHOD, 1),
            _symbol(3, "Kept", SymbolKind.CLASS, None),
            _symbol(4, "inner", SymbolKind.CLASS, 3),
            _symbol(5, "deeper", SymbolKind.METHOD, 4),
        ),
        imports=(),
        exports=(),
        package_name=None,
        build_constrained=False,
        has_errors=False,
    )

    assert symbol_rows("a.ts", facts) == (
        IndexedSymbol(SymbolKey("a.ts", "Kept", SymbolKind.CLASS, 0), 4, 4),
        IndexedSymbol(SymbolKey("a.ts", "Kept.inner", SymbolKind.CLASS, 0), 5, 5),
        IndexedSymbol(SymbolKey("a.ts", "Kept.inner.deeper", SymbolKind.METHOD, 0), 6, 6),
    )


def test_text_limits_match_the_model_columns() -> None:
    assert MAX_PATH_CHARS == 1024
    assert MAX_NAME_CHARS == 512
    assert MAX_INDEX_KEY_BYTES == 2000
    for column in (
        CodeIndexFile.path,
        CodeIndexSymbol.path,
        CodeIndexEdge.path,
        CodeIndexEdge.target_path,
        CodeIndexImport.path,
        CodeIndexImport.specifier,
        CodeIndexImport.target_path,
    ):
        assert column.type.length == MAX_PATH_CHARS
    for column in (CodeIndexSymbol.qualified_name, CodeIndexEdge.target_name):
        assert column.type.length == MAX_NAME_CHARS


_PATH = "src/" + "п" * 498 + ".ts"  # 1003 UTF-8 bytes, 505 characters
_WIDE = "ж" * 500  # 1000 UTF-8 bytes, 500 characters


def _row(name: str, kind: SymbolKind = SymbolKind.FUNCTION) -> IndexedSymbol:
    return IndexedSymbol(SymbolKey(_PATH, name, kind, 0), 1, 1)


def _edge(target_path: str, target_name: str = "x") -> IndexedEdge:
    kind = EdgeKind.IMPORTS if target_name == "" else EdgeKind.USES_IMPORTED
    return IndexedEdge(_PATH, kind, target_path, target_name)


def _record(specifier: str, target_path: str | None = "src/t.ts") -> ImportRecord:
    return ImportRecord(_PATH, specifier, RESOLVED, target_path)


def test_the_row_filter_drops_every_row_validate_would_reject() -> None:
    fits = _row("a" * 512)
    symbols = (
        fits,
        _row("b" * 513),  # over 512 characters
        _row(_WIDE),  # 1003 + 1000 bytes: over the key limit
        _row("ж" * 498),  # 1003 + 996 bytes: fits
    )
    kept_edges = (_edge("src/t.ts"), _edge("src/t.ts", ""))
    imports = (
        (_record("./t"), kept_edges + (_edge("src/t.ts", "n" * 513),)),
        # A too-long specifier drops the import with every edge it produced.
        (_record("./" + "s" * 1023), (_edge("src/long.ts"), _edge("src/long.ts", ""))),
        (_record("./" + "щ" * 499), (_edge("src/wide.ts"),)),  # key 1003 + 1000 bytes
        (_record("./u", "src/" + "т" * 999), (_edge("src/u.ts"),)),  # target over 2000 bytes
        (_record("./v"), (_edge("src/" + "ю" * 497),)),  # edge key over 2000 bytes
        (_record("./w", None), ()),
    )
    unfiltered = IndexedFile(
        _PATH,
        _SHA,
        symbols,
        tuple(edge for _, edges in imports for edge in edges),
        tuple(record for record, _ in imports),
    )
    with pytest.raises(ValueError):
        _validate(unfiltered)

    file = file_rows(_PATH, _SHA, symbols, imports)

    _validate(file)
    assert file.symbols == (fits, _row("ж" * 498))
    assert file.imports == (_record("./t"), _record("./v"), _record("./w", None))
    assert file.edges == (_edge("src/t.ts", ""), _edge("src/t.ts"))


def test_duplicate_edges_and_imports_collapse_like_the_store() -> None:
    edge = _edge("src/t.ts")
    file = file_rows(
        _PATH,
        _SHA,
        (),
        ((_record("./t"), (edge, _edge("src/t.ts", ""))), (_record("./t"), (edge,))),
    )

    assert file.imports == (_record("./t"),)
    assert file.edges == (_edge("src/t.ts", ""), edge)
