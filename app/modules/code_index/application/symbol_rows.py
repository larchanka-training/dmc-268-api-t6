"""Index rows of one parsed file (ADR 0001 §3, review #139 F4; #142).

A symbol is named along its `enclosing` chain; `replace_file` validates a whole file and
rejects all of it for one bad row, so `file_rows` drops every row that would not fit
before the write.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from app.modules.code_index.application.index_store import (
    ImportRecord,
    IndexedEdge,
    IndexedFile,
    IndexedSymbol,
)
from app.modules.code_index.domain.enums import EdgeKind
from app.modules.code_index.domain.keys import (
    MAX_INDEX_KEY_BYTES,
    MAX_NAME_CHARS,
    MAX_PATH_CHARS,
    assign_disambiguators,
)
from app.modules.reviews.application.ast_symbols import AstFacts, AstLanguage, SymbolKind

DEFAULT_EXPORT_NAME = "default"


def _key_fits(*values: str) -> bool:
    return sum(len(value.encode()) for value in values) <= MAX_INDEX_KEY_BYTES


def symbol_rows(path: str, facts: AstFacts) -> tuple[IndexedSymbol, ...]:
    """The keyed declarations of `facts`, in source order.

    An unnamed symbol is `default` when it is the default export; otherwise it is dropped
    together with its descendants. A Go method is `Recv.Name`.
    """
    by_index = {symbol.index: symbol for symbol in facts.symbols}
    default_exports = {index for name, index in facts.exports if name == DEFAULT_EXPORT_NAME}
    names: dict[int, str | None] = {}

    def qualified_name(index: int) -> str | None:
        if index in names:
            return names[index]
        symbol = by_index[index]
        own = symbol.name
        if own is None and index in default_exports:
            own = DEFAULT_EXPORT_NAME
        if (
            own is not None
            and facts.language is AstLanguage.GO
            and symbol.kind is SymbolKind.METHOD
            and symbol.declaration_kind
        ):
            own = f"{symbol.declaration_kind}.{own}"
        if own is not None and symbol.enclosing is not None:
            parent = qualified_name(symbol.enclosing)
            own = None if parent is None else f"{parent}.{own}"
        names[index] = own
        return own

    named = [
        (name, symbol)
        for symbol in facts.symbols
        if (name := qualified_name(symbol.index)) is not None
    ]
    keys = assign_disambiguators(path, ((name, symbol.kind) for name, symbol in named))
    return tuple(
        IndexedSymbol(key, symbol.declaration_range.start_line, symbol.declaration_range.end_line)
        for key, (_, symbol) in zip(keys, named, strict=True)
    )


def _symbol_fits(symbol: IndexedSymbol) -> bool:
    name = symbol.key.qualified_name
    return len(name) <= MAX_NAME_CHARS and _key_fits(symbol.key.path, name)


def _record_fits(record: ImportRecord) -> bool:
    target = record.target_path
    return (
        len(record.specifier) <= MAX_PATH_CHARS
        and _key_fits(record.path, record.specifier)
        and (target is None or (len(target) <= MAX_PATH_CHARS and _key_fits(target)))
    )


def _edge_fits(edge: IndexedEdge) -> bool:
    return (
        (edge.kind is EdgeKind.IMPORTS) == (edge.target_name == "")
        and len(edge.target_path) <= MAX_PATH_CHARS
        and len(edge.target_name) <= MAX_NAME_CHARS
        and _key_fits(edge.path, edge.target_path, edge.target_name)
    )


def file_rows(
    path: str,
    blob_sha: str,
    symbols: Iterable[IndexedSymbol],
    imports: Iterable[tuple[ImportRecord, Sequence[IndexedEdge]]],
) -> IndexedFile:
    """The rows of the file at `path`, an admitted path, without a row `replace_file`
    would reject.

    `imports` pairs each import record with the edges it produced. A record that does
    not fit drops all of its edges too: an edge without its record would escape the
    re-extraction rules, and an incremental build would then differ from a full one.
    Duplicate records and edges collapse as the store collapses them.
    """
    records: dict[tuple[str, str], ImportRecord] = {}
    edges: dict[IndexedEdge, None] = {}
    for record, produced in imports:
        if not _record_fits(record):
            continue
        records.setdefault((record.specifier, record.resolution), record)
        edges.update(dict.fromkeys(edge for edge in produced if _edge_fits(edge)))
    return IndexedFile(
        path=path,
        blob_sha=blob_sha,
        symbols=tuple(symbol for symbol in symbols if _symbol_fits(symbol)),
        edges=tuple(
            sorted(edges, key=lambda edge: (edge.kind, edge.target_path, edge.target_name))
        ),
        imports=tuple(records.values()),
    )
