"""Factual audit of candidate findings against visible code context (#445).

Guards against LLM verifier hallucinations where the model marks candidate findings
as 'supported' despite their factual premises being demonstrably contradicted by the
code context or lacking required external evidence.
"""

from __future__ import annotations

import ast
import os
import re
from typing import TYPE_CHECKING, NamedTuple

from tree_sitter import Node, Parser

from app.modules.reviews.application.ast_symbols import AstLanguage
from app.modules.reviews.application.prompt_builder import file_block_id
from app.modules.reviews.infrastructure.tree_sitter_facts import _grammar

_EXT_TO_LANGUAGE: dict[str, AstLanguage] = {
    ".py": AstLanguage.PYTHON,
    ".ts": AstLanguage.TYPESCRIPT,
    ".tsx": AstLanguage.TSX,
    ".js": AstLanguage.JAVASCRIPT,
    ".mjs": AstLanguage.JAVASCRIPT,
    ".cjs": AstLanguage.JAVASCRIPT,
    ".go": AstLanguage.GO,
}

_PARSERS: dict[AstLanguage, Parser] = {}


def _get_parser(lang: AstLanguage) -> Parser:
    if lang not in _PARSERS:
        grammar = _grammar(lang)
        parser = Parser(grammar)
        _PARSERS[lang] = parser
    return _PARSERS[lang]


def detect_language(file_path: str) -> AstLanguage | None:
    _, ext = os.path.splitext(file_path.lower())
    return _EXT_TO_LANGUAGE.get(ext)


if TYPE_CHECKING:
    from app.modules.reviews.application.reflection_pass import (
        CandidateFindingItem,
        VerificationStatus,
    )


class ClaimAuditResult(NamedTuple):
    """Result of auditing a candidate finding against concrete file facts."""

    is_valid: bool
    status: VerificationStatus = "supported"
    reason: str | None = None


_NOT_IMPORTED_PATTERN = re.compile(
    r"(?:(?P<id1>[A-Za-z0-9_$]+)\s+(?:is|are)\s+not\s+imported\b)|"
    r"(?:missing\s+import\s+(?:for\s+)?(?P<id2>[A-Za-z0-9_$]+)\b)|"
    r"(?:import\s+(?:them|it|(?P<id3>[A-Za-z0-9_$]+))\s+from\s+['\"][^'\"]+['\"])",
    re.IGNORECASE,
)

_TRY_CATCH_PATTERN = re.compile(
    r"(?:without|lacks?|missing)\s+(?:a\s+)?(?:surrounding\s+)?try\s*/\s*catch\b|"
    r"(?:without|lacks?|missing)\s+(?:a\s+)?try\s*-\s*catch\b",
    re.IGNORECASE,
)

_COMPARATIVE_OTHER_TOOLS_PATTERN = re.compile(
    r"\b(?:other\s+tools|other\s+files|other\s+methods)\s+(?:in\s+the\s+codebase\s+)?use\s+[`'\"]?(?P<param>\w+)[`'\"]?",
    re.IGNORECASE,
)


def _extract_imported_symbols_ts(root_node: Node) -> set[str]:
    """Extract all imported identifier names from TS/JS AST."""
    imported: set[str] = set()
    for child in root_node.children:
        if child.type == "import_statement":
            for clause in child.children:
                if clause.type in ("import_clause", "named_imports", "namespace_import"):

                    def collect_ids(n: Node) -> None:
                        if n.type == "identifier" and n.text is not None:
                            imported.add(n.text.decode("utf-8", errors="ignore"))
                        for sub in n.children:
                            collect_ids(sub)

                    collect_ids(clause)
    return imported


def _extract_imported_symbols_py(source_text: str) -> set[str]:
    """Extract all imported names from Python source using AST."""
    try:
        tree = ast.parse(source_text)
    except SyntaxError:
        return set()
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                names.add(alias.asname or alias.name)
    return names


def _is_line_in_try_catch(root_node: Node, target_line: int) -> tuple[bool, int | None, int | None]:
    """Check if target_line (1-based) is enclosed in a try statement with a catch clause."""
    row = target_line - 1

    def find_deepest(node: Node) -> Node | None:
        if node.start_point.row <= row <= node.end_point.row:
            for child in node.children:
                res = find_deepest(child)
                if res is not None:
                    return res
            return node
        return None

    node = find_deepest(root_node)
    curr = node
    while curr is not None:
        if curr.type in ("try_statement", "try_expression"):
            has_catch = any(
                child.type in ("catch_clause", "except_clause") for child in curr.children
            )
            if has_catch:
                return True, curr.start_point.row + 1, curr.end_point.row + 1
        curr = curr.parent
    return False, None, None


def audit_candidate_claim(
    cand: CandidateFindingItem,
    file_bytes: bytes | None,
    file_path: str,
) -> ClaimAuditResult:
    """Audit whether a candidate's factual assertions are contradicted or ungrounded."""
    finding = cand.finding
    text = f"{finding.title} {finding.body}"

    # 1. Audit comparative assertions about external codebase files
    m_comp = _COMPARATIVE_OTHER_TOOLS_PATTERN.search(text)
    if m_comp:
        param = m_comp.group("param")
        target_block_id = file_block_id(file_path)
        has_external_evidence = any(
            ref.block_id and ref.block_id != target_block_id for ref in finding.evidence_refs
        )
        if not has_external_evidence:
            return ClaimAuditResult(
                is_valid=False,
                status="insufficient_context",
                reason=(
                    f"Comparative claim that other tools use '{param}' "
                    "lacks evidence references in visible context"
                ),
            )

    if file_bytes is None:
        return ClaimAuditResult(is_valid=True)

    try:
        source_text = file_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return ClaimAuditResult(is_valid=True)

    lang = detect_language(file_path)

    # 2. Audit missing import claims
    for m in _NOT_IMPORTED_PATTERN.finditer(text):
        symbol = m.group("id1") or m.group("id2") or m.group("id3")
        if not symbol or len(symbol) <= 1:
            continue

        imported_symbols: set[str] = set()
        if lang is AstLanguage.PYTHON:
            imported_symbols = _extract_imported_symbols_py(source_text)
        elif lang in (AstLanguage.TYPESCRIPT, AstLanguage.TSX, AstLanguage.JAVASCRIPT):
            parser = _get_parser(lang)
            tree = parser.parse(file_bytes)
            imported_symbols = _extract_imported_symbols_ts(tree.root_node)

        if symbol in imported_symbols:
            return ClaimAuditResult(
                is_valid=False,
                status="contradicted",
                reason=(
                    f"Claim that '{symbol}' is not imported is contradicted: "
                    f"'{symbol}' is imported in {file_path}"
                ),
            )

    # 3. Audit missing try/catch claims
    if _TRY_CATCH_PATTERN.search(text) and lang is not None:
        parser = _get_parser(lang)
        tree = parser.parse(file_bytes)
        in_try, start_line, end_line = _is_line_in_try_catch(tree.root_node, finding.line)
        if in_try:
            return ClaimAuditResult(
                is_valid=False,
                status="contradicted",
                reason=(
                    f"Claim of missing try/catch at line {finding.line} is contradicted: "
                    f"enclosed in try/catch statement at lines {start_line}-{end_line}"
                ),
            )

    return ClaimAuditResult(is_valid=True)
