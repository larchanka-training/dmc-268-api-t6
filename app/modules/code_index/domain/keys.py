"""Logical keys of graph index rows (ADR 0001 §3, review #139 F4)."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from app.modules.reviews.application.ast_symbols import SymbolKind

# A PostgreSQL btree entry is capped at ~2.7 KB: the UTF-8 bytes of a row's text key
# columns must stay within this bound, or the row is not written (#141, Task 4).
MAX_INDEX_KEY_BYTES = 2000


@dataclass(frozen=True)
class SymbolKey:
    """A symbol's identity within its repository, independent of commit and of the
    order of other declarations; `symbol_id` of #136 is never stored."""

    path: str
    qualified_name: str
    kind: SymbolKind
    # 0-based ordinal among duplicates of (path, qualified_name, kind); unique is 0.
    disambiguator: int


def assign_disambiguators(
    path: str, declarations: Iterable[tuple[str, SymbolKind]]
) -> list[SymbolKey]:
    """Key each `(qualified_name, kind)` declaration of a file, given in source order."""
    seen: dict[tuple[str, SymbolKind], int] = {}
    keys = []
    for qualified_name, kind in declarations:
        ordinal = seen.get((qualified_name, kind), 0)
        seen[(qualified_name, kind)] = ordinal + 1
        keys.append(SymbolKey(path, qualified_name, kind, ordinal))
    return keys
