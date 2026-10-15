"""Logical symbol identity of the graph index (ADR 0001 §3, review #139 F4)."""

from __future__ import annotations

from dataclasses import fields

from app.modules.code_index.domain.keys import SymbolKey, assign_disambiguators
from app.modules.reviews.application.ast_symbols import SymbolKind

FUNCTION = SymbolKind.FUNCTION
CLASS = SymbolKind.CLASS


def test_symbol_key_holds_no_revision_or_declaration_order() -> None:
    assert [field.name for field in fields(SymbolKey)] == [
        "path",
        "qualified_name",
        "kind",
        "disambiguator",
    ]


def test_unique_declarations_get_disambiguator_zero() -> None:
    assert assign_disambiguators("a.py", [("foo", FUNCTION), ("Bar", CLASS)]) == [
        SymbolKey("a.py", "foo", FUNCTION, 0),
        SymbolKey("a.py", "Bar", CLASS, 0),
    ]


def test_duplicates_are_numbered_in_source_order_per_name_and_kind() -> None:
    declarations = [
        ("foo", FUNCTION),
        ("foo", CLASS),
        ("bar", FUNCTION),
        ("foo", FUNCTION),
        ("foo", FUNCTION),
    ]
    assert assign_disambiguators("a.py", declarations) == [
        SymbolKey("a.py", "foo", FUNCTION, 0),
        SymbolKey("a.py", "foo", CLASS, 0),
        SymbolKey("a.py", "bar", FUNCTION, 0),
        SymbolKey("a.py", "foo", FUNCTION, 1),
        SymbolKey("a.py", "foo", FUNCTION, 2),
    ]


def test_reordering_and_adding_declarations_keeps_existing_keys() -> None:
    c1 = assign_disambiguators("a.py", [("foo", FUNCTION), ("bar", FUNCTION)])
    c2 = assign_disambiguators("a.py", [("bar", FUNCTION), ("baz", FUNCTION), ("foo", FUNCTION)])

    assert c1 == [SymbolKey("a.py", "foo", FUNCTION, 0), SymbolKey("a.py", "bar", FUNCTION, 0)]
    assert c2 == [
        SymbolKey("a.py", "bar", FUNCTION, 0),
        SymbolKey("a.py", "baz", FUNCTION, 0),
        SymbolKey("a.py", "foo", FUNCTION, 0),
    ]


def test_moving_other_declarations_around_duplicates_keeps_their_keys() -> None:
    c1 = assign_disambiguators("a.py", [("foo", FUNCTION), ("x", FUNCTION), ("foo", FUNCTION)])
    c2 = assign_disambiguators("a.py", [("y", CLASS), ("foo", FUNCTION), ("foo", FUNCTION)])

    duplicates = [SymbolKey("a.py", "foo", FUNCTION, 0), SymbolKey("a.py", "foo", FUNCTION, 1)]
    assert [key for key in c1 if key.qualified_name == "foo"] == duplicates
    assert [key for key in c2 if key.qualified_name == "foo"] == duplicates


def test_no_declarations_give_no_keys() -> None:
    assert assign_disambiguators("empty.py", []) == []
