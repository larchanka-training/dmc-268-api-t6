"""AST facts of Python, TypeScript/TSX, JavaScript/JSX and Go sources."""

from __future__ import annotations

import pytest

from app.modules.reviews.application.ast_symbols import (
    AstFacts,
    AstLanguage,
    AstSymbol,
    ImportForm,
    ImportKind,
    SymbolKind,
    facts_from_json,
    facts_to_json,
    language_for_path,
)
from app.modules.reviews.infrastructure.tree_sitter_facts import ParseTimeout, extract_facts

_PYTHON = b'''"""Module doc."""
import os.path
import numpy as np
from .models import User as U, Role
from . import helpers
from pkg.sub import *

LIMIT = 10

@decorator
def handler(req: Request, limit=LIMIT) -> U:
    """Handle."""
    user = helpers.load(req)
    return user

class Service(Base):
    """Doc."""

    def run(self, x):
        return np.sum(x)
'''

_TYPESCRIPT = b"""import def, { a as b, c } from "./mod";
import * as ns from "../x";
import "./side";
/** Doc. */
export const handler = async (req: Req): Promise<void> => {
  const local = b(req);
};
export default class Foo extends Base {
  // method doc
  bar(x: number): ns.Type { return ns.make(x); }
}
export interface I { x: number }
type U = string | number;
const lazy = () => import("./lazy");
const cjs = require("./cjs");
function plain<T>(a: T): T { return a }
export { plain as renamed, U };
export * from "./re";
export declare function amb(x: number): void;
"""

_GO = b"""//go:build linux
// Package srv doc.
package srv

import (
	"fmt"
	al "example.com/m/pkg/util"
	_ "embed"
	. "math"
)

// Run does things.
// Second line.
func (s *Server) Run(ctx context.Context) error {
	x := al.Do()
	return fmt.Errorf("%v", x)
}

type (
	// Server doc.
	Server struct{ A int }
	Doer   interface{ Do() }
)
type ID = string
const Max, min = 3, 1
var count int
"""


def _facts(language: AstLanguage, source: bytes) -> AstFacts:
    return extract_facts(language, source, cpu_seconds=5.0)


def _symbol(facts: AstFacts, name: str | None) -> AstSymbol:
    return next(item for item in facts.symbols if item.name == name)


def _lines(symbol: AstSymbol) -> tuple[tuple[int, int], tuple[int, int], list[tuple[int, int]]]:
    declaration, signature = symbol.declaration_range, symbol.signature_ranges[0]
    return (
        (declaration.start_line, declaration.end_line),
        (signature.start_line, signature.end_line),
        [(item.start_line, item.end_line) for item in symbol.docstring_ranges],
    )


def test_language_is_chosen_by_extension() -> None:
    expected = {
        "a.py": AstLanguage.PYTHON,
        "a.pyi": AstLanguage.PYTHON,
        "src/a.ts": AstLanguage.TYPESCRIPT,
        "src/a.d.ts": AstLanguage.TYPESCRIPT,
        "a.tsx": AstLanguage.TSX,
        "a.js": AstLanguage.JAVASCRIPT,
        "a.jsx": AstLanguage.JAVASCRIPT,
        "a.mjs": AstLanguage.JAVASCRIPT,
        "a.cjs": AstLanguage.JAVASCRIPT,
        "cmd/main.go": AstLanguage.GO,
    }
    assert {path: language_for_path(path) for path in expected} == expected
    for path in ("README.md", "Makefile", ".py", "a.rb", "dir.py/file"):
        assert language_for_path(path) is None


def test_python_declarations_with_one_based_inclusive_lines() -> None:
    facts = _facts(AstLanguage.PYTHON, _PYTHON)

    assert [(item.kind, item.name, item.enclosing) for item in facts.symbols] == [
        (SymbolKind.VARIABLE, "LIMIT", None),
        (SymbolKind.FUNCTION, "handler", None),
        (SymbolKind.CLASS, "Service", None),
        (SymbolKind.METHOD, "run", 2),
    ]
    # The decorator belongs to the declaration and to its signature.
    assert _lines(_symbol(facts, "handler")) == ((10, 14), (10, 11), [(12, 12)])
    assert _lines(_symbol(facts, "Service")) == ((16, 20), (16, 16), [(17, 17)])
    assert _lines(_symbol(facts, "run")) == ((19, 20), (19, 19), [])
    handler = _symbol(facts, "handler")
    assert ("helpers", "load") in handler.member_references
    assert {"Request", "LIMIT", "U", "decorator"} <= set(handler.references)
    assert handler.local_bindings == ("limit", "req", "user")
    assert facts.has_errors is False


def test_python_imports_keep_module_names_and_local_bindings() -> None:
    facts = _facts(AstLanguage.PYTHON, _PYTHON)

    assert [
        (item.module, [(n.kind, n.name, n.alias) for n in item.names], item.line)
        for item in facts.imports
    ] == [
        ("os.path", [(ImportKind.MODULE, None, "os")], 2),
        ("numpy", [(ImportKind.MODULE, None, "np")], 3),
        (
            ".models",
            [(ImportKind.NAMED, "User", "U"), (ImportKind.NAMED, "Role", "Role")],
            4,
        ),
        (".", [(ImportKind.NAMED, "helpers", "helpers")], 5),
        ("pkg.sub", [(ImportKind.WILDCARD, None, None)], 6),
    ]


def test_typescript_declarations_exports_and_import_forms() -> None:
    facts = _facts(AstLanguage.TYPESCRIPT, _TYPESCRIPT)

    assert [(item.kind, item.name, item.declaration_kind) for item in facts.symbols] == [
        (SymbolKind.FUNCTION, "handler", "binding"),
        (SymbolKind.CLASS, "Foo", None),
        (SymbolKind.METHOD, "bar", None),
        (SymbolKind.TYPE, "I", "interface"),
        (SymbolKind.TYPE, "U", "type"),
        (SymbolKind.FUNCTION, "lazy", "binding"),
        (SymbolKind.VARIABLE, "cjs", "const"),
        (SymbolKind.FUNCTION, "plain", None),
        (SymbolKind.FUNCTION, "amb", None),
    ]
    assert _lines(_symbol(facts, "handler")) == ((5, 7), (5, 5), [(4, 4)])
    assert _lines(_symbol(facts, "bar")) == ((10, 10), (10, 10), [(9, 9)])
    assert dict(facts.exports) == {
        "handler": 0,
        "default": 1,
        "I": 3,
        "U": 4,
        "renamed": 7,
        "amb": 8,
    }
    assert ("ns", "Type") in _symbol(facts, "bar").member_references
    assert [(item.module, item.form) for item in facts.imports] == [
        ("./mod", ImportForm.STATIC),
        ("../x", ImportForm.STATIC),
        ("./side", ImportForm.STATIC),
        ("./re", ImportForm.REEXPORT),
        ("./lazy", ImportForm.DYNAMIC),
        ("./cjs", ImportForm.COMMONJS),
    ]
    assert [(n.kind, n.name, n.alias) for n in facts.imports[0].names] == [
        (ImportKind.DEFAULT, "default", "def"),
        (ImportKind.NAMED, "a", "b"),
        (ImportKind.NAMED, "c", "c"),
    ]
    assert facts.imports[1].names[0].kind is ImportKind.NAMESPACE
    assert facts.imports[2].names[0].kind is ImportKind.SIDE_EFFECT


def test_javascript_jsx_and_an_anonymous_default_export() -> None:
    source = b"""import util from "./util.js";
export default function () { return util.go(); }
export function Named(props) { return <div/>; }
const helper = function (a) { return a; };
module.exports = { helper };
"""
    facts = _facts(AstLanguage.JAVASCRIPT, source)

    assert [(item.kind, item.name) for item in facts.symbols] == [
        (SymbolKind.FUNCTION, None),
        (SymbolKind.FUNCTION, "Named"),
        (SymbolKind.FUNCTION, "helper"),
    ]
    assert dict(facts.exports) == {"default": 0, "Named": 1}
    assert facts.has_errors is False


def test_tsx_is_parsed_with_its_own_grammar() -> None:
    facts = _facts(AstLanguage.TSX, b"export const C = (): JSX.Element => <div/>;\n")

    assert [(item.kind, item.name) for item in facts.symbols] == [(SymbolKind.FUNCTION, "C")]
    assert facts.has_errors is False


def test_javascript_signatures_end_where_the_body_starts() -> None:
    source = b"""// Adds two numbers.
export function add(
  a,
  b
) {
  return a + b;
}

/** A counter. */
export class Counter {
  // Increments by a step.
  increment(
    step
  ) {
    this.value += step;
  }
}
"""
    facts = _facts(AstLanguage.JAVASCRIPT, source)

    assert facts.has_errors is False
    assert _lines(_symbol(facts, "add")) == ((2, 7), (2, 5), [(1, 1)])
    assert _lines(_symbol(facts, "Counter")) == ((10, 17), (10, 10), [(9, 9)])
    assert _lines(_symbol(facts, "increment")) == ((12, 16), (12, 14), [(11, 11)])


def test_tsx_component_signature_keeps_its_typed_props() -> None:
    source = b"""type Props = { label: string };

// Renders a button.
export function Button({
  label,
}: Props): JSX.Element {
  return <button>{label}</button>;
}
"""
    facts = _facts(AstLanguage.TSX, source)

    assert facts.has_errors is False
    assert _lines(_symbol(facts, "Button")) == ((4, 8), (4, 6), [(3, 3)])


def test_go_declarations_package_and_imports() -> None:
    facts = _facts(AstLanguage.GO, _GO)

    assert [
        (item.kind, item.name, item.declaration_kind, item.exported) for item in facts.symbols
    ] == [
        (SymbolKind.METHOD, "Run", "Server", True),
        (SymbolKind.TYPE, "Server", "struct", True),
        (SymbolKind.TYPE, "Doer", "interface", True),
        (SymbolKind.TYPE, "ID", "alias", True),
        (SymbolKind.VARIABLE, "Max", "const", True),
        (SymbolKind.VARIABLE, "min", "const", False),
        (SymbolKind.VARIABLE, "count", "var", False),
    ]
    assert facts.package_name == "srv"
    assert _lines(_symbol(facts, "Run")) == ((14, 17), (14, 14), [(12, 13)])
    assert _lines(_symbol(facts, "Server")) == ((21, 21), (21, 21), [(20, 20)])
    assert ("al", "Do") in _symbol(facts, "Run").member_references
    assert _symbol(facts, "Run").local_bindings == ("ctx", "s", "x")
    assert [(item.module, item.names[0].kind, item.names[0].alias) for item in facts.imports] == [
        ("fmt", ImportKind.MODULE, None),
        ("example.com/m/pkg/util", ImportKind.MODULE, "al"),
        ("embed", ImportKind.SIDE_EFFECT, None),
        ("math", ImportKind.WILDCARD, None),
    ]


def test_go_build_constraints_and_cgo_are_flagged() -> None:
    plain = b"// Package a.\npackage a\n\nfunc F() {}\n"

    assert _facts(AstLanguage.GO, plain).build_constrained is False
    assert _facts(AstLanguage.GO, _GO).build_constrained is True
    assert _facts(AstLanguage.GO, b"// +build linux\n\npackage a\n").build_constrained is True
    assert _facts(AstLanguage.GO, b'package a\n\nimport "C"\n').build_constrained is True


def test_syntax_error_marks_only_the_overlapping_symbol_incomplete() -> None:
    facts = _facts(AstLanguage.PYTHON, b"def ok():\n    return 1\n\ndef broken(:\n    pass\n")

    assert facts.has_errors is True
    assert _symbol(facts, "ok").complete is True
    assert _symbol(facts, "broken").complete is False


def test_facts_survive_the_json_round_trip() -> None:
    for language, source in (
        (AstLanguage.PYTHON, _PYTHON),
        (AstLanguage.TYPESCRIPT, _TYPESCRIPT),
        (AstLanguage.GO, _GO),
    ):
        facts = _facts(language, source)
        assert facts_from_json(facts_to_json(facts)) == facts
        assert facts.profile_version.startswith("tree-sitter=")


def test_spent_budget_stops_the_walk() -> None:
    with pytest.raises(ParseTimeout):
        extract_facts(AstLanguage.PYTHON, b"def f(): pass\n" * 2000, cpu_seconds=0.0)
