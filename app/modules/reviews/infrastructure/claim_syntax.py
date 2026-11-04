"""Tree-sitter backed syntax inspection for deterministic claim auditing."""

from __future__ import annotations

import logging
import time

from tree_sitter import Node, Parser

from app.modules.reviews.application.ast_symbols import (
    MAX_PARSE_CPU_SECONDS_PER_FILE,
    MAX_SOURCE_BYTES,
    TS_JS_LANGUAGES,
    AstLanguage,
)
from app.modules.reviews.infrastructure.tree_sitter_facts import (
    _grammar,
)

_LOGGER = logging.getLogger(__name__)
_FUNCTION_BOUNDARY_TYPES = frozenset(
    {
        "function_declaration",
        "arrow_function",
        "function_expression",
        "method_definition",
        "generator_function_declaration",
        "generator_function",
    }
)


class TreeSitterClaimSyntaxInspector:
    """Bounded Tree-sitter inspector for TS/TSX/JS imports and try/catch structure."""

    def __init__(
        self,
        *,
        max_source_bytes: int = MAX_SOURCE_BYTES,
        cpu_seconds: float = MAX_PARSE_CPU_SECONDS_PER_FILE,
    ) -> None:
        self._max_source_bytes = max_source_bytes
        self._cpu_seconds = cpu_seconds

    def parse_imports(
        self,
        path: str,
        language: AstLanguage,
        source: bytes,
    ) -> set[str] | None:
        """Extract local runtime value imported symbols for TS/TSX/JS, skipping type-only."""
        if language not in TS_JS_LANGUAGES:
            return None
        if not self._within_source_bounds(path, source):
            return None
        try:
            parser = Parser(_grammar(language))
            tree = parser.parse(source)
        except (MemoryError, ValueError, RuntimeError) as exc:
            _LOGGER.warning("Tree-sitter import parse skipped for %s: %s", path, exc)
            return None

        bindings: set[str] = set()
        for node in tree.root_node.named_children:
            if node.type == "import_statement":
                # Skip `import type { ... }` or `import type Foo from '...'`
                if any(child.type == "type" for child in node.children):
                    continue
                clause = next(
                    (child for child in node.named_children if child.type == "import_clause"), None
                )
                if clause is None:
                    continue
                for child in clause.named_children:
                    if child.type == "identifier":
                        txt = _node_text(child)
                        if txt is not None:
                            bindings.add(txt)
                    elif child.type == "namespace_import":
                        alias = next(
                            (item for item in child.named_children if item.type == "identifier"),
                            None,
                        )
                        txt = _node_text(alias)
                        if txt is not None:
                            bindings.add(txt)
                    elif child.type == "named_imports":
                        for spec in child.named_children:
                            if spec.type == "import_specifier":
                                # Skip type-only specifiers `import { type Foo } from '...'`
                                if any(c.type == "type" for c in spec.children):
                                    continue
                                name = (
                                    spec.child_by_field_name("alias")
                                    or spec.child_by_field_name("name")
                                )
                                txt = _node_text(name)
                                if txt is not None:
                                    bindings.add(txt)
            elif node.type in {"lexical_declaration", "variable_declaration"}:
                # CommonJS require: const x = require('...')
                for declarator in node.named_children:
                    if declarator.type == "variable_declarator":
                        name_node = declarator.child_by_field_name("name")
                        init_node = declarator.child_by_field_name("value")
                        if (
                            name_node is not None
                            and name_node.type == "identifier"
                            and init_node is not None
                            and init_node.type == "call_expression"
                        ):
                            fn = init_node.child_by_field_name("function")
                            if fn is not None and fn.text == b"require":
                                txt = _node_text(name_node)
                                if txt is not None:
                                    bindings.add(txt)
        return bindings

    def is_line_in_try_body(
        self,
        path: str,
        language: AstLanguage,
        source: bytes,
        target_line: int,
    ) -> tuple[bool, int | None, int | None] | None:
        """Check whether ``target_line`` is in a caught ``try`` body, bounded by functions."""
        if language not in TS_JS_LANGUAGES or target_line < 1:
            return None
        if not self._within_source_bounds(path, source):
            return None

        try:
            parser = Parser(_grammar(language))
            tree = parser.parse(source)
        except (MemoryError, ValueError, RuntimeError) as exc:
            _LOGGER.warning("Tree-sitter AST parse skipped for %s: %s", path, exc)
            return None

        deadline = time.process_time() + self._cpu_seconds
        target_row = target_line - 1
        lines = source.splitlines()
        if target_row < 0 or target_row >= len(lines):
            return (False, None, None)

        line = lines[target_row]
        col = len(line) - len(line.lstrip())
        if col < len(line):
            node = tree.root_node.named_descendant_for_point_range(
                (target_row, col), (target_row, len(line))
            )
        else:
            node = tree.root_node.named_descendant_for_point_range((target_row, 0), (target_row, 0))

        if node is None:
            node = tree.root_node.descendant_for_point_range((target_row, 0), (target_row, 0))

        curr = node
        while curr is not None:
            if time.process_time() > deadline:
                _LOGGER.warning("Tree-sitter try/catch walk timed out for %s", path)
                return None
            if curr.type in _FUNCTION_BOUNDARY_TYPES:
                # Code inside nested function/callback/lambda is not caught by outer try block
                return (False, None, None)
            if curr.type == "try_statement":
                span = _matched_try_span(curr, target_row)
                if span is not None:
                    return (True, span[0], span[1])
            curr = curr.parent

        return (False, None, None)

    def _within_source_bounds(self, path: str, source: bytes) -> bool:
        if len(source) > self._max_source_bytes:
            _LOGGER.warning(
                "Skipping Tree-sitter claim audit for %s: %d bytes exceeds %d limit",
                path,
                len(source),
                self._max_source_bytes,
            )
            return False
        try:
            source.decode("utf-8")
        except UnicodeDecodeError as exc:
            _LOGGER.warning("Skipping non-UTF-8 claim audit for %s: %s", path, exc)
            return False
        return True


def _node_text(node: Node | None) -> str | None:
    if node is None or node.text is None:
        return None
    return node.text.decode("utf-8")


def _matched_try_span(node: Node, target_row: int) -> tuple[int, int] | None:
    """Return 1-based ``(start, end)`` of caught ``try_statement`` containing ``target_row``."""
    body = node.child_by_field_name("body")
    handler = node.child_by_field_name("handler")
    if body is None or handler is None:
        return None
    if not (body.start_point.row <= target_row <= body.end_point.row):
        return None
    # On `} catch (err) {` lines, exclude catch header unless a body child sits on that row.
    if (
        body.start_point.row < body.end_point.row
        and target_row == body.end_point.row
        and handler.start_point.row == body.end_point.row
        and not any(
            child.start_point.row <= target_row <= child.end_point.row
            for child in body.named_children
        )
    ):
        return None
    return (node.start_point.row + 1, node.end_point.row + 1)
