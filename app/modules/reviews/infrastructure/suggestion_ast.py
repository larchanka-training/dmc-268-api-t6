"""AST analysis and syntax verification of commit suggestions (#444).

Executes in the isolated ast_worker subprocess with memory and CPU bounds.
"""

from __future__ import annotations

import ast
import re

from tree_sitter import Node, Parser

from app.modules.reviews.application.ast_symbols import (
    AstLanguage,
    SuggestionRejectionReason,
    SuggestionValidationResult,
)
from app.modules.reviews.infrastructure.tree_sitter_facts import _grammar

_PARSERS: dict[AstLanguage, Parser] = {}
_COMMENT_NODES = frozenset({"comment", "line_comment", "block_comment"})


def _get_parser(language: AstLanguage) -> Parser:
    if language not in _PARSERS:
        _PARSERS[language] = Parser(_grammar(language))
    return _PARSERS[language]


def apply_suggestion_to_text(
    original_text: str,
    start_line: int | None,
    line: int,
    suggestion: str,
) -> str:
    """Emulate GitHub suggestion application.

    Replaces lines in range [start_line..line] (1-based, inclusive) with suggestion text.
    Splits strictly by '\\n' to match GitHub line accounting.
    """
    lines = original_text.split("\n")
    sugg_lines = suggestion.split("\n")
    start = start_line if start_line is not None else line

    if start < 1 or line < 1:
        raise ValueError(
            f"Line numbers must be 1-based positive integers: start={start}, line={line}"
        )
    if start > line:
        raise ValueError(f"start_line ({start}) must be <= line ({line})")

    total_lines = len(lines) - 1 if (lines and lines[-1] == "") else len(lines)
    if line > total_lines:
        raise ValueError(f"Target line {line} is out of range for file with {total_lines} lines")

    if "\r\n" in original_text:
        sugg_lines = [s if s.endswith("\r") else s + "\r" for s in sugg_lines]

    new_lines = lines[: start - 1] + sugg_lines + lines[line:]
    return "\n".join(new_lines)


def count_syntax_errors(root: Node) -> int:
    """Count total ERROR and MISSING nodes iteratively without stack recursion."""
    count = 0
    stack = [root]
    while stack:
        curr = stack.pop()
        if curr.is_error or curr.is_missing:
            count += 1
        stack.extend(reversed(curr.children))
    return count


def _has_outer_matching_parens(text: str) -> bool:
    if len(text) < 2 or not (text.startswith("(") and text.endswith(")")):
        return False
    depth = 0
    for idx, ch in enumerate(text):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0 and idx < len(text) - 1:
                return False
    return depth == 0


def _normalize_condition(node: Node | None) -> str:
    if node is None or node.text is None:
        return ""
    norm = re.sub(r"\s+", " ", node.text.decode("utf-8", errors="ignore").strip())
    while _has_outer_matching_parens(norm):
        norm = norm[1:-1].strip()
    return norm


def _first_non_comment_child(node: Node) -> Node | None:
    for ch in node.named_children:
        if ch.type not in _COMMENT_NODES:
            return ch
    return None


def _enclosing_if_for_first_statement(node: Node) -> Node | None:
    """Return the enclosing if/elif node when ``node`` is its immediate first statement."""
    parent = node.parent
    if parent is None:
        return None
    if parent.type in ("if_statement", "elif_clause"):
        consequence = parent.child_by_field_name("consequence")
        return parent if consequence is not None and consequence.id == node.id else None

    statement_container: Node | None = None
    block: Node | None = None
    if parent.type in ("block", "statement_block"):
        statement_container = parent
        block = parent
    elif (
        parent.type == "statement_list"
        and parent.parent is not None
        and parent.parent.type == "block"
    ):
        statement_container = parent
        block = parent.parent

    if statement_container is None or block is None:
        return None
    first = _first_non_comment_child(statement_container)
    if first is None or first.id != node.id:
        return None
    enclosing = block.parent
    if enclosing is None or enclosing.type not in ("if_statement", "elif_clause"):
        return None
    consequence = enclosing.child_by_field_name("consequence")
    return enclosing if consequence is not None and consequence.id == block.id else None


def count_duplicate_nested_conditions(root: Node) -> int:
    """Count nested if statements that duplicate their immediate enclosing if/elif condition."""
    count = 0
    stack = [root]
    while stack:
        curr = stack.pop()
        stack.extend(reversed(curr.children))
        if curr.type != "if_statement":
            continue
        enclosing = _enclosing_if_for_first_statement(curr)
        if enclosing is None:
            continue
        c1 = _normalize_condition(curr.child_by_field_name("condition"))
        c2 = _normalize_condition(enclosing.child_by_field_name("condition"))
        if not c1 or c1 != c2:
            continue
        init1 = _normalize_condition(curr.child_by_field_name("initializer"))
        init2 = _normalize_condition(enclosing.child_by_field_name("initializer"))
        if not init1 or init1 == init2:
            count += 1
    return count


_BEFORE_STATS_CACHE: dict[tuple[AstLanguage, bytes], tuple[int, int, bool]] = {}


def _get_before_stats(
    parser: Parser, language: AstLanguage, source_bytes: bytes, orig_text: str
) -> tuple[int, int, bool]:
    key = (language, source_bytes)
    cached = _BEFORE_STATS_CACHE.get(key)
    if cached is not None:
        return cached
    if len(_BEFORE_STATS_CACHE) >= 64:
        _BEFORE_STATS_CACHE.clear()
    tree = parser.parse(source_bytes)
    errors = count_syntax_errors(tree.root_node)
    dups = count_duplicate_nested_conditions(tree.root_node)
    orig_python_valid = True
    if language is AstLanguage.PYTHON:
        try:
            ast.parse(orig_text)
        except (SyntaxError, ValueError, MemoryError, RecursionError):
            orig_python_valid = False
    _BEFORE_STATS_CACHE[key] = (errors, dups, orig_python_valid)
    return errors, dups, orig_python_valid


def _check_python_syntax(new_text: str, *, orig_valid: bool) -> SuggestionValidationResult | None:
    """Reject Python suggestions introducing an ``ast.parse`` error on valid sources."""
    if not orig_valid:
        return None
    try:
        ast.parse(new_text)
    except (SyntaxError, ValueError) as exc:
        return SuggestionValidationResult(
            is_valid=False,
            reason=SuggestionRejectionReason.SYNTAX_ERROR,
            detail=f"Python syntax error after applying suggestion: {exc}",
        )
    return None


def _validate_tree_sitter_delta(
    parser: Parser,
    *,
    new_text: str,
    errors_before: int,
    dups_before: int,
    ast_confirms_valid: bool = False,
) -> SuggestionValidationResult:
    """Compare Tree-sitter error and duplicate-condition counts after substitution."""
    try:
        tree_after = parser.parse(new_text.encode("utf-8"))
    except Exception as exc:
        return SuggestionValidationResult(
            is_valid=True,
            reason=SuggestionRejectionReason.WORKER_ERROR,
            detail=f"Tree-sitter parse failed: {exc}",
        )

    errors_after = count_syntax_errors(tree_after.root_node)
    if errors_after > errors_before and not ast_confirms_valid:
        return SuggestionValidationResult(
            is_valid=False,
            reason=SuggestionRejectionReason.SYNTAX_ERROR,
            detail=(
                f"Suggestion introduces syntax errors: "
                f"{errors_after} errors after vs {errors_before} before"
            ),
        )

    dups_after = count_duplicate_nested_conditions(tree_after.root_node)
    if dups_after > dups_before:
        return SuggestionValidationResult(
            is_valid=False,
            reason=SuggestionRejectionReason.DUPLICATE_CONDITION,
            detail="Suggestion introduces duplicate nested condition",
        )

    return SuggestionValidationResult(is_valid=True)


def validate_suggestion_tree(
    language: AstLanguage,
    source_bytes: bytes,
    start_line: int | None,
    line: int,
    suggestion: str,
) -> SuggestionValidationResult:
    """Validate a suggestion using Tree-sitter and AST parsing in worker process."""
    try:
        orig_text = source_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return SuggestionValidationResult(
            is_valid=True,
            reason=SuggestionRejectionReason.NON_UTF8,
            detail="Source cannot be decoded as utf-8",
        )

    try:
        new_text = apply_suggestion_to_text(orig_text, start_line, line, suggestion)
    except ValueError as exc:
        return SuggestionValidationResult(
            is_valid=False,
            reason=SuggestionRejectionReason.APPLICATION_FAILED,
            detail=f"Failed to apply suggestion text: {exc}",
        )

    try:
        parser = _get_parser(language)
        errors_before, dups_before, orig_valid = _get_before_stats(
            parser, language, source_bytes, orig_text
        )
    except Exception as exc:
        return SuggestionValidationResult(
            is_valid=True,
            reason=SuggestionRejectionReason.WORKER_ERROR,
            detail=f"Tree-sitter parse failed: {exc}",
        )

    if language is AstLanguage.PYTHON:
        py_rejection = _check_python_syntax(new_text, orig_valid=orig_valid)
        if py_rejection is not None:
            return py_rejection

    return _validate_tree_sitter_delta(
        parser,
        new_text=new_text,
        errors_before=errors_before,
        dups_before=dups_before,
        ast_confirms_valid=language is AstLanguage.PYTHON and orig_valid,
    )
