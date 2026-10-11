"""AST facts of one source file (CONTEXT_AND_VERIFICATION_SPEC §3.3).

Facts depend on the file content only, so they are cached by blob id. They select
context: they are neither a type checker nor evidence of runtime behaviour. Line numbers
are 1-based source lines.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from app.modules.reviews.application.context_provenance import LineRange

MAX_SOURCE_BYTES = 262_144
MAX_PARSE_CPU_SECONDS_PER_FILE = 1.0
MAX_PARSE_CPU_SECONDS_PER_ATTEMPT = 5.0
MAX_PARSER_MEMORY_BYTES = 134_217_728
MAX_REFERENCES_PER_SYMBOL = 400


class AstLanguage(StrEnum):
    PYTHON = "python"
    TYPESCRIPT = "typescript"
    TSX = "tsx"
    JAVASCRIPT = "javascript"
    GO = "go"


_LANGUAGE_BY_SUFFIX = {
    ".py": AstLanguage.PYTHON,
    ".pyi": AstLanguage.PYTHON,
    ".ts": AstLanguage.TYPESCRIPT,
    ".tsx": AstLanguage.TSX,
    ".js": AstLanguage.JAVASCRIPT,
    ".jsx": AstLanguage.JAVASCRIPT,
    ".mjs": AstLanguage.JAVASCRIPT,
    ".cjs": AstLanguage.JAVASCRIPT,
    ".go": AstLanguage.GO,
}


def language_for_path(path: str) -> AstLanguage | None:
    """The AST language of a path by its extension; other files keep line-based context."""
    name = path.rsplit("/", 1)[-1]
    dot = name.rfind(".")
    return None if dot <= 0 else _LANGUAGE_BY_SUFFIX.get(name[dot:])


class SymbolKind(StrEnum):
    FUNCTION = "function"
    METHOD = "method"
    CLASS = "class"
    VARIABLE = "variable"
    TYPE = "type"


class ImportKind(StrEnum):
    NAMED = "named"
    DEFAULT = "default"
    NAMESPACE = "namespace"
    MODULE = "module"
    WILDCARD = "wildcard"
    SIDE_EFFECT = "side_effect"


class ImportForm(StrEnum):
    STATIC = "static"
    DYNAMIC = "dynamic"
    COMMONJS = "commonjs"
    REEXPORT = "reexport"


class AstFailureReason(StrEnum):
    SOURCE_TOO_LARGE = "source_too_large"
    SOURCE_DECODE_FAILED = "source_decode_failed"
    AST_TIMEOUT = "ast_timeout"
    AST_RESOURCE_LIMIT = "ast_resource_limit"
    AST_LIMIT = "ast_limit"
    AST_FAILED = "ast_failed"


@dataclass(frozen=True)
class AstSymbol:
    """One declaration; ``complete`` is false when a syntax error overlaps it."""

    index: int
    kind: SymbolKind
    name: str | None
    declaration_kind: str | None
    declaration_range: LineRange
    signature_ranges: tuple[LineRange, ...]
    docstring_ranges: tuple[LineRange, ...]
    enclosing: int | None
    exported: bool
    complete: bool
    references: tuple[str, ...]
    member_references: tuple[tuple[str, str], ...]
    local_bindings: tuple[str, ...]


@dataclass(frozen=True)
class AstImportName:
    """One imported binding: ``name`` in the target module, ``alias`` in this file."""

    kind: ImportKind
    name: str | None
    alias: str | None


@dataclass(frozen=True)
class AstImport:
    module: str
    names: tuple[AstImportName, ...]
    form: ImportForm
    line: int


@dataclass(frozen=True)
class AstFacts:
    language: AstLanguage
    profile_version: str
    symbols: tuple[AstSymbol, ...]
    imports: tuple[AstImport, ...]
    # Exported name -> index of the local symbol; "default" is the default export.
    exports: tuple[tuple[str, int], ...]
    package_name: str | None
    build_constrained: bool
    has_errors: bool

    def symbol_named(self, name: str) -> tuple[AstSymbol, ...]:
        return tuple(item for item in self.symbols if item.name == name and item.enclosing is None)


@dataclass(frozen=True)
class ParseRequest:
    key: str
    language: AstLanguage
    source: bytes


class SourceParser(Protocol):
    """Parses sources in an isolated worker; a file never fails the whole batch."""

    async def parse_many(
        self, requests: tuple[ParseRequest, ...]
    ) -> dict[str, AstFacts | AstFailureReason]: ...


def _range(value: Any) -> LineRange:
    return LineRange(int(value[0]), int(value[1]))


def facts_to_json(facts: AstFacts) -> dict[str, Any]:
    """A JSON form for the worker pipe and the cache."""
    return {
        "language": facts.language.value,
        "profile_version": facts.profile_version,
        "symbols": [
            {
                "index": item.index,
                "kind": item.kind.value,
                "name": item.name,
                "declaration_kind": item.declaration_kind,
                "declaration_range": [
                    item.declaration_range.start_line,
                    item.declaration_range.end_line,
                ],
                "signature_ranges": [[r.start_line, r.end_line] for r in item.signature_ranges],
                "docstring_ranges": [[r.start_line, r.end_line] for r in item.docstring_ranges],
                "enclosing": item.enclosing,
                "exported": item.exported,
                "complete": item.complete,
                "references": list(item.references),
                "member_references": [list(pair) for pair in item.member_references],
                "local_bindings": list(item.local_bindings),
            }
            for item in facts.symbols
        ],
        "imports": [
            {
                "module": item.module,
                "names": [[name.kind.value, name.name, name.alias] for name in item.names],
                "form": item.form.value,
                "line": item.line,
            }
            for item in facts.imports
        ],
        "exports": [list(pair) for pair in facts.exports],
        "package_name": facts.package_name,
        "build_constrained": facts.build_constrained,
        "has_errors": facts.has_errors,
    }


def facts_from_json(value: dict[str, Any]) -> AstFacts:
    return AstFacts(
        language=AstLanguage(value["language"]),
        profile_version=str(value["profile_version"]),
        symbols=tuple(
            AstSymbol(
                index=int(item["index"]),
                kind=SymbolKind(item["kind"]),
                name=item["name"],
                declaration_kind=item["declaration_kind"],
                declaration_range=_range(item["declaration_range"]),
                signature_ranges=tuple(_range(r) for r in item["signature_ranges"]),
                docstring_ranges=tuple(_range(r) for r in item["docstring_ranges"]),
                enclosing=item["enclosing"],
                exported=bool(item["exported"]),
                complete=bool(item["complete"]),
                references=tuple(item["references"]),
                member_references=tuple((pair[0], pair[1]) for pair in item["member_references"]),
                local_bindings=tuple(item["local_bindings"]),
            )
            for item in value["symbols"]
        ),
        imports=tuple(
            AstImport(
                module=str(item["module"]),
                names=tuple(
                    AstImportName(ImportKind(name[0]), name[1], name[2]) for name in item["names"]
                ),
                form=ImportForm(item["form"]),
                line=int(item["line"]),
            )
            for item in value["imports"]
        ),
        exports=tuple((str(pair[0]), int(pair[1])) for pair in value["exports"]),
        package_name=value["package_name"],
        build_constrained=bool(value["build_constrained"]),
        has_errors=bool(value["has_errors"]),
    )
