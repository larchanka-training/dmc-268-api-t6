"""AST analysis and syntax verification of commit suggestions (#444).

Executes in the isolated ast_worker subprocess with memory and CPU bounds.
"""

from __future__ import annotations

import ast
import re
from typing import Any

from tree_sitter import Node, Parser

from app.modules.reviews.application.ast_symbols import (
    AstLanguage,
    SuggestionRejectionReason,
    SuggestionValidationResult,
)
from app.modules.reviews.infrastructure.tree_sitter_facts import _grammar

_PARSERS: dict[AstLanguage, Parser] = {}


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
        if curr.is_error or curr.is_missing or curr.type in ("ERROR", "MISSING"):
            count += 1
        stack.extend(reversed(curr.children))
    return count


def _is_ancestor(parent: Node, child: Node) -> bool:
    curr = child.parent
    while curr is not None:
        if curr.id == parent.id:
            return True
        curr = curr.parent
    return False


def _normalize_condition(node: Node | None) -> str:
    if node is None or node.text is None:
        return ""
    text = node.text.decode("utf-8", errors="ignore")
    norm = re.sub(r"\s+", " ", text.strip())
    if norm.startswith("(") and norm.endswith(")"):
        norm = norm[1:-1].strip()
    return norm


def check_duplicate_nested_condition(root: Node, sugg_start: int, sugg_end: int) -> bool:
    """Detect if the suggestion introduces a nested if with condition duplicate to enclosing if.

    Only applies to if_statements overlapping the replacement range where no intervening
    statements precede the nested if inside the enclosing block.
    """
    stack = [root]
    while stack:
        curr = stack.pop()
        if curr.type in ("if_statement", "if_expression"):
            node_start_line = curr.start_point.row + 1
            if sugg_start <= node_start_line <= sugg_end:
                p = curr.parent
                enclosing_if = None
                while p is not None:
                    if p.type in ("if_statement", "if_expression"):
                        enclosing_if = p
                        break
                    p = p.parent

                if enclosing_if is not None:
                    c1 = _normalize_condition(curr.child_by_field_name("condition"))
                    c2 = _normalize_condition(enclosing_if.child_by_field_name("condition"))
                    if c1 and c1 == c2:
                        consequence = enclosing_if.child_by_field_name("consequence")
                        if consequence is not None:
                            block = consequence
                            if block.type in ("block", "statement_block"):
                                statement_list = block
                            else:
                                child_block = next(
                                    (
                                        c
                                        for c in block.children
                                        if c.type in ("block", "statement_block", "statement_list")
                                    ),
                                    None,
                                )
                                statement_list = child_block if child_block is not None else block

                            statements_before = False
                            for ch in statement_list.named_children:
                                if ch.id == curr.id or _is_ancestor(ch, curr):
                                    break
                                if ch.type not in ("comment", "line_comment", "block_comment"):
                                    statements_before = True
                                    break
                            if not statements_before:
                                return True
        stack.extend(reversed(curr.children))
    return False


def check_invalid_scoped_import(root_node: Any, start_line: int, end_line: int) -> bool:
    """Check if suggestion inserts a static import declaration inside a function body."""
    stack = [root_node]
    function_types = {
        "function_declaration",
        "function_expression",
        "arrow_function",
        "method_definition",
        "generator_function_declaration",
    }
    while stack:
        curr = stack.pop()
        if curr.type == "import_statement":
            imp_start = curr.start_point.row + 1
            imp_end = curr.end_point.row + 1
            if not (imp_end < start_line or imp_start > end_line):
                p = curr.parent
                while p is not None:
                    if p.type in function_types:
                        return True
                    p = p.parent
        stack.extend(reversed(curr.children))
    return False


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
    except Exception as exc:
        return SuggestionValidationResult(
            is_valid=False,
            reason=SuggestionRejectionReason.APPLICATION_FAILED,
            detail=f"Failed to apply suggestion text: {exc}",
        )

    # 1. Python AST parse check
    if language is AstLanguage.PYTHON:
        orig_valid = True
        try:
            ast.parse(orig_text)
        except (SyntaxError, MemoryError, RecursionError):
            orig_valid = False
        except Exception:
            orig_valid = False

        try:
            ast.parse(new_text)
        except (SyntaxError, MemoryError, RecursionError) as exc:
            if orig_valid:
                return SuggestionValidationResult(
                    is_valid=False,
                    reason=SuggestionRejectionReason.SYNTAX_ERROR,
                    detail=f"Python syntax error after applying suggestion: {exc}",
                )
        except Exception as exc:
            if orig_valid:
                return SuggestionValidationResult(
                    is_valid=False,
                    reason=SuggestionRejectionReason.SYNTAX_ERROR,
                    detail=f"Python parser error after applying suggestion: {exc}",
                )

    # 2. Tree-sitter parse validation
    try:
        parser = _get_parser(language)
        new_bytes = new_text.encode("utf-8")
        tree_before = parser.parse(source_bytes)
        tree_after = parser.parse(new_bytes)
    except Exception as exc:
        return SuggestionValidationResult(
            is_valid=True,
            reason=SuggestionRejectionReason.WORKER_ERROR,
            detail=f"Tree-sitter parse failed: {exc}",
        )

    errors_before = count_syntax_errors(tree_before.root_node)
    errors_after = count_syntax_errors(tree_after.root_node)

    if errors_after > errors_before:
        return SuggestionValidationResult(
            is_valid=False,
            reason=SuggestionRejectionReason.SYNTAX_ERROR,
            detail=(
                f"Suggestion introduces syntax errors: "
                f"{errors_after} errors after vs {errors_before} before"
            ),
        )

    # 3. Duplicate condition check
    start = start_line if start_line is not None else line
    sugg_lines_count = len(suggestion.split("\n"))
    sugg_end = start + sugg_lines_count - 1

    if check_duplicate_nested_condition(tree_after.root_node, start, sugg_end):
        return SuggestionValidationResult(
            is_valid=False,
            reason=SuggestionRejectionReason.DUPLICATE_CONDITION,
            detail="Suggestion introduces duplicate nested condition",
        )

    # 4. Scoped static import check (JS / TS)
    if language in (
        AstLanguage.TYPESCRIPT,
        AstLanguage.JAVASCRIPT,
    ) and check_invalid_scoped_import(tree_after.root_node, start, sugg_end):
        return SuggestionValidationResult(
            is_valid=False,
            reason=SuggestionRejectionReason.SYNTAX_ERROR,
            detail="Suggestion introduces static import declaration inside function body",
        )

    return SuggestionValidationResult(is_valid=True)
