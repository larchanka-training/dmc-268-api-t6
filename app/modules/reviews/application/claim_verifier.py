"""Deterministic claim auditing against full context before publishing verified findings.

Scope and explicit boundaries (issue #445):
- Covers TypeScript, TSX, JavaScript (``.ts``, ``.tsx``, ``.js``, ``.jsx``, ``.mjs``, ``.cjs``)
  and Python (``.py``, ``.pyi``). Go files and unrecognized extensions skip AST-based checks.
- Audited claim classes:
  1. Missing-import claims (``X is not imported``, ``missing import for X``), excluding
     module-path complaints (``X is not imported from ...``) and wrong-source import hints.
     Requires complete file source (``L3 full_file`` or un-truncated ``added`` diff).
  2. Missing ``try/catch`` (or Python ``try/except``) claims when the finding line sits inside
     the caught ``body`` of a ``try`` block (excludes ``catch``/``except``, ``else``, and
     ``finally`` blocks, as well as ``try ... finally`` without a catch handler).
     Requires complete file source.
  3. Cross-file comparative claims (``other tools/files use <param>``) checked against
     external ``evidence_refs`` and their referenced blocks in ``ReviewContext``.
- Explicitly outside deterministic audit scope: logical assertions without single-block
  syntactic truth (such as ``"accepts at most one parameter"`` / ``"exactly one"``) and AST
  checks on modified files lacking an ``L3 full_file`` block.
"""

from __future__ import annotations

import ast
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, NamedTuple, Protocol

from app.modules.reviews.application.ast_symbols import (
    MAX_SOURCE_BYTES,
    TS_JS_LANGUAGES,
    AstLanguage,
    language_for_path,
)
from app.modules.reviews.application.context_provenance import LineRange
from app.modules.reviews.application.prompt_builder import ChangedFile, ReviewContext, file_block_id
from app.modules.reviews.application.review_context import ContextBlock
from app.modules.reviews.application.review_output import ReviewFindingV2

_LOGGER = logging.getLogger(__name__)

type VerificationStatus = Literal["supported", "contradicted", "insufficient_context"]

_AUDITED_LANGUAGES = frozenset(
    {
        AstLanguage.PYTHON,
        AstLanguage.TYPESCRIPT,
        AstLanguage.TSX,
        AstLanguage.JAVASCRIPT,
    }
)
_IMPORT_STOPWORDS = frozenset(
    {
        "file",
        "it",
        "module",
        "symbol",
        "that",
        "them",
        "these",
        "they",
        "this",
        "those",
        "which",
    }
)
_COMPARATIVE_STOPWORDS = frozenset(
    {
        "a",
        "all",
        "an",
        "and",
        "async",
        "await",
        "consistent",
        "different",
        "error",
        "errors",
        "explicit",
        "generic",
        "http",
        "https",
        "implicit",
        "it",
        "json",
        "more",
        "most",
        "other",
        "proper",
        "same",
        "similar",
        "some",
        "specific",
        "standard",
        "strict",
        "such",
        "that",
        "the",
        "their",
        "them",
        "these",
        "this",
        "those",
        "timeout",
        "timeouts",
        "two",
        "validation",
    }
)

_NOT_IMPORTED_PATTERN = re.compile(
    r"(?:\b[`'\"]?(?P<id1>[A-Za-z_$][A-Za-z0-9_$]*)[`'\"]?\s+(?:is|are|isn't|aren't)\s+"
    r"(?:not|never)\s+imported\b)"
    r"|(?:\bmissing\s+(?:an?\s+|the\s+)?import\s+(?:for\s+)?[`'\"]?(?P<id2>[A-Za-z_$][A-Za-z0-9_$]*)[`'\"]?\b)"
    r"|(?:\b[`'\"]?(?P<id3>[A-Za-z_$][A-Za-z0-9_$]*)[`'\"]?\s+(?:was|were)\s+not\s+imported\b)",
    re.IGNORECASE,
)

_QUALIFIED_IMPORT_TAIL = re.compile(
    r"^\s*(?:"
    r"from\b"
    r"|as\s+(?:a\s+)?values?\b"
    r"|at\s+runtime\b"
    r"|for\s+runtime\b"
    r"|in\s+runtime\b"
    r"|in\s+(?:another|other)\s+files?\b"
    r"|in\s+[`'\"]?(?P<target>[a-zA-Z0-9_./\-]+\.[a-zA-Z0-9]+)[`'\"]?"
    r")",
    re.IGNORECASE,
)

_NO_TRY_CATCH_PATTERN = re.compile(
    r"\b(?:"
    r"not\s+(?:wrapped|inside|in)\s+(?:(?:a|an)\s+)?(?:(?:surrounding|enclosing|outer|explicit|proper)\s+)?try\s*[/-]?\s*(?:catch|except)"
    r"|without\s+(?:(?:a|an|any)\s+)?(?:(?:surrounding|enclosing|outer|explicit|proper)\s+)?try\s*[/-]?\s*(?:catch|except)"
    r"|missing\s+(?:(?:a|an)\s+)?(?:(?:surrounding|enclosing|outer|explicit|proper)\s+)?try\s*[/-]?\s*(?:catch|except)"
    r"|no\s+(?:(?:surrounding|enclosing|outer|explicit|proper)\s+)?try\s*[/-]?\s*(?:catch|except)(?:\s+block)?"
    r"|lacks?\s+(?:(?:a|an)\s+)?(?:(?:surrounding|enclosing|outer|explicit|proper)\s+)?try\s*[/-]?\s*(?:catch|except)"
    r")\b",
    re.IGNORECASE,
)

_COMPARATIVE_OTHER_TOOLS_PATTERN = re.compile(
    r"\bother\s+(?:tools|files|handlers|endpoints|methods|implementations)"
    r"(?:\s+\w+){0,4}?\s+(?:use|accept|expect|take|support|follow)\s+"
    r"(?:the\s+)?(?:(?P<quoted>[`'\"]?)(?P<param>[A-Za-z_$][A-Za-z0-9_$]*)(?P=quoted))"
    r"(?:\s+(?P<kw>parameter|param|argument|arg|field|option|prop)\b)?",
    re.IGNORECASE,
)


class ClaimSyntaxInspector(Protocol):
    """Port for syntax inspection of non-Python source files."""

    def parse_imports(
        self,
        path: str,
        language: AstLanguage,
        source: bytes,
    ) -> set[str] | None: ...

    def is_line_in_try_body(
        self,
        path: str,
        language: AstLanguage,
        source: bytes,
        target_line: int,
    ) -> tuple[bool, int | None, int | None] | None: ...


class ClaimAuditResult(NamedTuple):
    """Deterministic audit verdict for a verifier-supported candidate finding."""

    status: VerificationStatus = "supported"
    reason: str | None = None


@dataclass(frozen=True)
class ContextBlockSnippet:
    """Indexed prompt block text for validating external comparative evidence."""

    path: str | None
    lines: Mapping[int, str]
    text: str


def extract_full_source_bytes(context: ReviewContext, file_path: str) -> bytes | None:
    """Return complete 1-based UTF-8 file bytes for ``file_path`` from ``context``, or ``None``.

    Only returns source when exact 1-based line numbers from line 1 are guaranteed:
    either an untruncated ``L3`` ``full_file`` block or an untruncated ``added`` diff whose
    right-side lines are contiguous ``1..N``. Modified diff hunks without an ``L3`` full
    file return ``None`` rather than stitching sparse hunks into shifted line numbers.
    """
    for block in context.context_blocks:
        source_bytes = _full_file_block_bytes(block, file_path)
        if source_bytes is not None:
            return source_bytes

    for changed_file in context.changed_files:
        source_bytes = _added_changed_file_bytes(changed_file, file_path)
        if source_bytes is not None:
            return source_bytes

    return None


def collect_context_blocks(context: ReviewContext) -> dict[str, ContextBlockSnippet]:
    """Index all prompt blocks in ``context`` by ``block_id`` for evidence checking."""
    snippets: dict[str, ContextBlockSnippet] = {}
    for changed_file in context.changed_files:
        kept = [line for line in changed_file.lines if line.type != "removed"]
        snippets[file_block_id(changed_file.path)] = ContextBlockSnippet(
            path=changed_file.path,
            lines={line.number: line.content for line in kept},
            text="\n".join(line.content for line in kept),
        )
    for block in context.context_blocks:
        snippets[block.block_id] = ContextBlockSnippet(
            path=block.source.path,
            lines=_map_block_lines(block.text, block.source.ranges),
            text=block.text,
        )
    for retrieved in context.retrieved:
        snippets[retrieved.block_id] = ContextBlockSnippet(
            path=retrieved.source.path,
            lines=_map_block_lines(retrieved.text, retrieved.source.ranges),
            text=retrieved.text,
        )
    return snippets


def audit_candidate_claim(
    finding: ReviewFindingV2,
    source_bytes: bytes | None = None,
    *,
    claim_syntax: ClaimSyntaxInspector | None = None,
    context_blocks: Mapping[str, ContextBlockSnippet] | None = None,
) -> ClaimAuditResult:
    """Deterministically verify factual claims before publishing a verified finding."""
    text = f"{finding.title}\n{finding.body}"
    language = language_for_path(finding.path)

    if source_bytes is not None and language in _AUDITED_LANGUAGES:
        import_result = _audit_import_claim(
            text=text,
            file_path=finding.path,
            language=language,
            source_bytes=source_bytes,
            claim_syntax=claim_syntax,
        )
        if import_result.status != "supported":
            return import_result

        try_result = _audit_try_catch_claim(
            text=text,
            file_path=finding.path,
            language=language,
            line=finding.line,
            source_bytes=source_bytes,
            claim_syntax=claim_syntax,
        )
        if try_result.status != "supported":
            return try_result

    return _audit_comparative_external_claim(
        text=text,
        finding=finding,
        context_blocks=context_blocks,
    )


def _full_file_block_bytes(block: ContextBlock, file_path: str) -> bytes | None:
    if block.source.path != file_path or block.source.reason != "full_file" or block.truncated:
        return None
    ranges = block.source.ranges
    if len(ranges) != 1 or ranges[0].start_line != 1:
        return None
    line_count = len(block.text.split("\n"))
    if line_count not in {ranges[0].end_line, ranges[0].end_line + 1}:
        return None
    normalized = block.text if block.text.endswith("\n") else f"{block.text}\n"
    return normalized.encode("utf-8")


def _added_changed_file_bytes(changed_file: ChangedFile, file_path: str) -> bytes | None:
    if changed_file.path != file_path or changed_file.status != "added":
        return None
    if changed_file.total_lines is not None or not changed_file.lines:
        return None
    kept = [line for line in changed_file.lines if line.type != "removed"]
    if not kept or [line.number for line in kept] != list(range(1, len(kept) + 1)):
        return None
    return ("\n".join(line.content for line in kept) + "\n").encode("utf-8")


def _map_block_lines(text: str, ranges: tuple[LineRange, ...]) -> dict[int, str]:
    if not ranges:
        return {}
    numbers = [num for item in ranges for num in range(item.start_line, item.end_line + 1)]
    raw_lines = text.split("\n")
    if len(raw_lines) == len(numbers) + 1 and raw_lines[-1] == "":
        raw_lines = raw_lines[:-1]
    if len(raw_lines) != len(numbers):
        return {}
    return dict(zip(numbers, raw_lines, strict=True))


def _audit_import_claim(
    *,
    text: str,
    file_path: str,
    language: AstLanguage,
    source_bytes: bytes,
    claim_syntax: ClaimSyntaxInspector | None,
) -> ClaimAuditResult:
    matches = list(_NOT_IMPORTED_PATTERN.finditer(text))
    if not matches:
        return ClaimAuditResult(status="supported")

    claimed_symbols: list[str] = []
    file_name = file_path.rsplit("/", 1)[-1].lower()
    for match in matches:
        tail = text[match.end():]
        q = _QUALIFIED_IMPORT_TAIL.search(tail)
        if q:
            target = q.groupdict().get("target")
            if target and target.lower() in {file_path.lower(), file_name}:
                pass
            else:
                continue

        line_start = text.rfind("\n", 0, match.start())
        line_start = 0 if line_start == -1 else line_start + 1
        line_end = text.find("\n", match.end())
        line_end = len(text) if line_end == -1 else line_end
        line_text = text[line_start:line_end]
        if re.search(
            r"\b(?:as\s+(?:a\s+)?values?|at\s+runtime|in\s+runtime)\b",
            line_text,
            re.IGNORECASE,
        ):
            continue

        symbol = next((val for val in match.groupdict().values() if val is not None), None)
        if symbol and symbol.lower() not in _IMPORT_STOPWORDS:
            claimed_symbols.append(symbol)
    if not claimed_symbols:
        return ClaimAuditResult(status="supported")

    imported_symbols = _parse_imports(file_path, language, source_bytes, claim_syntax)
    if imported_symbols is None:
        return ClaimAuditResult(status="supported")

    for symbol in claimed_symbols:
        if symbol in imported_symbols:
            return ClaimAuditResult(
                status="contradicted",
                reason=f"Symbol '{symbol}' is already imported in {file_path}",
            )
    return ClaimAuditResult(status="supported")


def _audit_try_catch_claim(
    *,
    text: str,
    file_path: str,
    language: AstLanguage,
    line: int,
    source_bytes: bytes,
    claim_syntax: ClaimSyntaxInspector | None,
) -> ClaimAuditResult:
    if not _NO_TRY_CATCH_PATTERN.search(text):
        return ClaimAuditResult(status="supported")

    in_try_result = _is_line_in_try_body(file_path, language, source_bytes, line, claim_syntax)
    if in_try_result is None:
        return ClaimAuditResult(status="supported")

    in_try, start_line, end_line = in_try_result
    if in_try:
        span_label = f"lines {start_line}-{end_line}" if start_line and end_line else "a try block"
        return ClaimAuditResult(
            status="contradicted",
            reason=f"Line {line} is enclosed by {span_label} with catch/except in {file_path}",
        )
    return ClaimAuditResult(status="supported")


def _audit_comparative_external_claim(
    *,
    text: str,
    finding: ReviewFindingV2,
    context_blocks: Mapping[str, ContextBlockSnippet] | None,
) -> ClaimAuditResult:
    param = _extract_comparative_param(text)
    if param is None:
        return ClaimAuditResult(status="supported")

    same_file_bid = file_block_id(finding.path)
    external_refs = []
    for ref in finding.evidence_refs:
        if ref.block_id == same_file_bid:
            continue
        if context_blocks is not None:
            snippet = context_blocks.get(ref.block_id)
            if snippet is not None and snippet.path == finding.path:
                continue
        external_refs.append(ref)

    if not external_refs:
        return ClaimAuditResult(
            status="insufficient_context",
            reason=f"Claim asserts other tools/files use '{param}' without external block evidence",
        )

    if context_blocks is None:
        return ClaimAuditResult(status="supported")

    identifier_re = re.compile(rf"(?<![A-Za-z0-9_$]){re.escape(param)}(?![A-Za-z0-9_$])")
    for ref in external_refs:
        snippet = context_blocks.get(ref.block_id)
        if snippet is None:
            continue
        if snippet.lines:
            cited = "\n".join(
                snippet.lines[ln]
                for ln in range(ref.start_line, ref.end_line + 1)
                if ln in snippet.lines
            )
            if cited and identifier_re.search(cited):
                return ClaimAuditResult(status="supported")
        elif identifier_re.search(snippet.text):
            return ClaimAuditResult(status="supported")

    return ClaimAuditResult(
        status="insufficient_context",
        reason=f"Claim asserts other tools/files use '{param}' but cited external blocks omit it",
    )


def _extract_comparative_param(text: str) -> str | None:
    for match in _COMPARATIVE_OTHER_TOOLS_PATTERN.finditer(text):
        param = match.group("param")
        quoted = bool(match.group("quoted"))
        keyword_suffix = bool(match.group("kw"))
        if param.lower() in _COMPARATIVE_STOPWORDS and not quoted:
            continue
        if (
            quoted
            or keyword_suffix
            or any(ch.isupper() or ch.isdigit() or ch in {"_", "$"} for ch in param)
        ):
            return param
    return None


def _parse_imports(
    file_path: str,
    language: AstLanguage,
    source_bytes: bytes,
    claim_syntax: ClaimSyntaxInspector | None,
) -> set[str] | None:
    if len(source_bytes) > MAX_SOURCE_BYTES:
        _LOGGER.warning(
            "Skipping import claim audit for %s: %d bytes exceeds %d limit",
            file_path,
            len(source_bytes),
            MAX_SOURCE_BYTES,
        )
        return None
    if language == AstLanguage.PYTHON:
        return _parse_python_imports(file_path, source_bytes)
    if language in TS_JS_LANGUAGES and claim_syntax is not None:
        return claim_syntax.parse_imports(file_path, language, source_bytes)
    return None


def _is_line_in_try_body(
    file_path: str,
    language: AstLanguage,
    source_bytes: bytes,
    target_line: int,
    claim_syntax: ClaimSyntaxInspector | None,
) -> tuple[bool, int | None, int | None] | None:
    if len(source_bytes) > MAX_SOURCE_BYTES or target_line < 1:
        return None
    if language == AstLanguage.PYTHON:
        return _is_python_line_in_try_body(file_path, source_bytes, target_line)
    if language in TS_JS_LANGUAGES and claim_syntax is not None:
        return claim_syntax.is_line_in_try_body(file_path, language, source_bytes, target_line)
    return None


def _parse_python_ast(file_path: str, source_bytes: bytes) -> ast.AST | None:
    try:
        text = source_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        _LOGGER.warning("Skipping non-UTF-8 Python claim audit for %s: %s", file_path, exc)
        return None
    try:
        return ast.parse(text, filename=file_path)
    except (SyntaxError, ValueError, RecursionError, MemoryError) as exc:
        _LOGGER.warning("Python AST parse failed during claim audit for %s: %s", file_path, exc)
        return None


_PYTHON_FUNCTION_BOUNDARIES = (
    ast.FunctionDef,
    ast.AsyncFunctionDef,
    ast.Lambda,
)


def _is_type_checking_guard(node: ast.AST) -> bool:
    if isinstance(node, ast.If):
        test = node.test
        if isinstance(test, ast.Name) and test.id == "TYPE_CHECKING":
            return True
        if isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING":
            return True
    return False


def _parse_python_imports(file_path: str, source_bytes: bytes) -> set[str] | None:
    tree = _parse_python_ast(file_path, source_bytes)
    if tree is None:
        return None
    symbols: set[str] = set()
    stack: list[ast.AST] = list(ast.iter_child_nodes(tree))
    while stack:
        node = stack.pop()
        if _is_type_checking_guard(node):
            continue
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        if isinstance(node, ast.Import):
            for alias in node.names:
                binding = alias.asname or alias.name.split(".", maxsplit=1)[0]
                if binding:
                    symbols.add(binding)
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == "*":
                    continue
                binding = alias.asname or alias.name
                if binding:
                    symbols.add(binding)
        else:
            children = list(ast.iter_child_nodes(node))
            if children:
                stack.extend(reversed(children))
    return symbols


def _python_node_span(node: ast.AST) -> tuple[int, int]:
    lineno = int(getattr(node, "lineno", 0) or 0)
    end_lineno = int(getattr(node, "end_lineno", None) or lineno)
    col_offset = int(getattr(node, "col_offset", 0) or 0)
    return (end_lineno - lineno, -col_offset)


def _is_python_line_in_try_body(
    file_path: str,
    source_bytes: bytes,
    target_line: int,
) -> tuple[bool, int | None, int | None] | None:
    tree = _parse_python_ast(file_path, source_bytes)
    if tree is None:
        return None

    parents: dict[ast.AST, ast.AST] = {}
    candidates: list[ast.AST] = []
    stack: list[ast.AST] = [tree]
    while stack:
        node = stack.pop()
        for child in ast.iter_child_nodes(node):
            parents[child] = node
            if (
                hasattr(child, "lineno")
                and hasattr(child, "end_lineno")
                and child.end_lineno is not None
                and child.lineno <= target_line <= child.end_lineno
            ):
                candidates.append(child)
            stack.append(child)

    if not candidates:
        return (False, None, None)

    candidates.sort(key=_python_node_span)

    curr: ast.AST | None = candidates[0]
    while curr is not None:
        if isinstance(curr, _PYTHON_FUNCTION_BOUNDARIES):
            return (False, None, None)
        if (
            isinstance(curr, (ast.Try, ast.TryStar))
            and curr.handlers
            and curr.body
        ):
            body_start = curr.body[0].lineno
            body_end = max(stmt.end_lineno or stmt.lineno for stmt in curr.body)
            if body_start <= target_line <= body_end:
                return (True, curr.lineno, body_end)
        curr = parents.get(curr)

    return (False, None, None)
