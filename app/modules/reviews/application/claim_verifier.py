"""Deterministic claim auditing against full context before publishing verified findings.

Scope and explicit boundaries (issue #445):
- Covers TypeScript, TSX, JavaScript (``.ts``, ``.tsx``, ``.js``, ``.jsx``, ``.mjs``, ``.cjs``)
  and Python (``.py``, ``.pyi``). Go files and unrecognized extensions skip AST-based checks.
- Audited claim classes:
  1. Missing-import claims (``X is not imported``, ``missing import for X``, or multi-symbol
     enumerations such as ``A and B are not imported``), excluding module-path complaints
     (``X is not imported from ...``) and wrong-source import hints. Multi-symbol claims are
     contradicted only when all claimed symbols are present in imported bindings; if any
     claimed symbol is genuinely missing, the finding is preserved as supported.
     Requires complete file source (``L3 full_file`` or un-truncated ``added`` diff).
  2. Missing ``try/catch`` (or Python ``try/except``) claims when the finding line sits inside
     the caught ``body`` or the ``try`` header line itself of a ``try`` block (excludes
     ``catch``/``except``, ``else``, and ``finally`` blocks, as well as ``try ... finally``
     without a catch handler). Requires complete file source.
  3. Cross-file comparative claims (``other tools/files use <param>``) checked against
     external ``evidence_refs`` and their referenced blocks in ``ReviewContext``.
     - "other tools use <param>" strictly requires external citations to originate from tool
       definitions (under ``*/tools/*``).
     - When a finding asserts a primary local defect (e.g. SSRF or security vulnerability), an
       incidental unverified comparative remark in the finding body does not drop the finding.
       Only findings whose primary thesis is cross-tool consistency are dropped when external
       evidence is missing.
     - Scoped to English regex keywords; paraphrased or non-English comparative claims fall
       outside deterministic AST audit scope.
- Parser safety and performance bounds:
  - Synchronous C-parsers (CPython ``ast.parse`` and Tree-sitter C extension) run in-process on
    already in-memory source buffers bounded by pre-flight ``MAX_SOURCE_BYTES`` (262_144 bytes,
    256 KiB) checks, returning fail-open with logging on memory or syntax errors.
  - In the Python AST branch, the source size limit is the sole bound; in the Tree-sitter branch,
    traversals are additionally bound by monotonic deadlines (``MAX_PARSE_CPU_SECONDS_PER_FILE``).
  - ``MAX_PARSER_MEMORY_BYTES`` governs the separate background ``ast_worker`` subprocess.
- Explicitly outside deterministic audit scope: logical assertions without single-block
  syntactic truth (such as ``"accepts at most one parameter"`` / ``"exactly one"``) and AST
  checks on modified files lacking an ``L3 full_file`` block.
- Limitations:
  - Primary defect heuristic: ``_PRIMARY_DEFECT_TITLE_RE`` uses title keywords (param, parameter,
    naming, typo, convention, etc.); comparative claims lacking these title keywords are not
    filtered by primary defect logic.
  - Single-line lambdas: single-line lambda definitions (e.g. ``fn = lambda x: bad(x)``) or arrow
    functions on a single line within a ``try`` block match the enclosing assignment/statement and
    are considered protected by ``try`` (verdict ``contradicted``), while multi-line forms correctly
    recognize nested function boundaries (verdict ``supported``).
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

_IDENTIFIER_ITEM = r"[`'\"]?[A-Za-z_$][A-Za-z0-9_$]*[`'\"]?"
_SEPARATOR = r"(?:,\s*(?:and|or)\s*|,\s*|\s+(?:and|or)\s+)"
_IDENTIFIERS_LIST = rf"{_IDENTIFIER_ITEM}(?:{_SEPARATOR}{_IDENTIFIER_ITEM})*"

_NOT_IMPORTED_PATTERN = re.compile(
    rf"(?:\b(?P<list1>{_IDENTIFIERS_LIST})\s+(?:is|are|isn't|aren't|was|were)\s+"
    r"(?:not|never)\s+imported\b)"
    rf"|(?:\bmissing\s+(?:an?\s+|the\s+)?import\s+(?:for\s+)?(?P<list2>{_IDENTIFIERS_LIST})\b)",
    re.IGNORECASE,
)

_QUALIFIED_NON_IN_TAIL = re.compile(
    r"^\s*(?:"
    r"from\b"
    r"|as\s+(?:a\s+)?values?\b"
    r"|at\s+runtime\b"
    r"|for\s+runtime\b"
    r"|in\s+runtime\b"
    r")",
    re.IGNORECASE,
)

_IN_TARGET_TAIL = re.compile(
    r"^\s*in\s+(?:the\s+)?[`'\"]?(?P<target>[a-zA-Z0-9_./\-]+(?:\s+(?:file|files|tool|tools|function|method|handler|endpoint))?)[`'\"]?",
    re.IGNORECASE,
)

_NO_TRY_CATCH_PATTERN = re.compile(
    r"\b(?:"
    r"not\s+(?:wrapped(?:\s+in)?|inside|in)\s+(?:(?:a|an)\s+)?(?:(?:surrounding|enclosing|outer|explicit|proper)\s+)?try\s*[/-]?\s*(?:catch|except)"
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


class TryCatchSpan(NamedTuple):
    in_try: bool
    try_start: int | None = None
    try_end: int | None = None


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
    ) -> TryCatchSpan | None: ...


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
    base_name = file_name.rsplit(".", 1)[0]
    this_file_targets = {
        file_path.lower(),
        file_name,
        base_name,
        "this",
        "this file",
        "current file",
        "this module",
        "current module",
    }
    source_text = source_bytes.decode("utf-8", errors="replace")
    for match in matches:
        tail = text[match.end() :]
        if _QUALIFIED_NON_IN_TAIL.search(tail):
            continue

        in_match = _IN_TARGET_TAIL.search(tail)
        if in_match:
            target = (in_match.group("target") or "").strip().lower()
            is_local = target in this_file_targets
            if not is_local:
                m_tool = re.match(r"^(?:the\s+)?([A-Za-z0-9_$]+)\s+tools?$", target)
                if m_tool:
                    tool_ident = m_tool.group(1)
                    if tool_ident not in {"other", "another", "each", "all"}:
                        is_local = bool(
                            re.search(rf"\b{re.escape(tool_ident)}\b", source_text, re.IGNORECASE)
                        )
            if not is_local:
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

        raw_list = match.group("list1") or match.group("list2") or ""
        tokens = re.findall(r"\b[A-Za-z_$][A-Za-z0-9_$]*\b", raw_list)
        for tok in tokens:
            if tok.lower() not in _IMPORT_STOPWORDS and tok.lower() not in {"and", "or", "for"}:
                claimed_symbols.append(tok)
    if not claimed_symbols:
        return ClaimAuditResult(status="supported")

    imported_symbols = _parse_imports(file_path, language, source_bytes, claim_syntax)
    if imported_symbols is None:
        return ClaimAuditResult(status="supported")

    if all(symbol in imported_symbols for symbol in claimed_symbols):
        symbols_str = ", ".join(f"'{s}'" for s in claimed_symbols)
        return ClaimAuditResult(
            status="contradicted",
            reason=f"Symbols {symbols_str} are already imported in {file_path}",
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

    result = _is_line_in_try_body(file_path, language, source_bytes, line, claim_syntax)
    if result is None:
        return ClaimAuditResult(status="supported")

    if result.in_try:
        span_label = (
            f"lines {result.try_start}-{result.try_end}"
            if result.try_start and result.try_end
            else "a try block"
        )
        return ClaimAuditResult(
            status="contradicted",
            reason=f"Line {line} is enclosed by {span_label} with catch/except in {file_path}",
        )
    return ClaimAuditResult(status="supported")


_PRIMARY_DEFECT_TITLE_PATTERNS = (
    "ssrf",
    "injection",
    "vulnerability",
    "vulnerable",
    "cve",
    "overflow",
    "unhandled",
    "crash",
    "leak",
    "dos",
    "rce",
    "auth",
    "authentication",
    "authorization",
    "security",
    "sanitization",
    "bypass",
)

_PRIMARY_DEFECT_TITLE_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(k) for k in sorted(_PRIMARY_DEFECT_TITLE_PATTERNS)) + r")\b",
    re.IGNORECASE,
)


def _is_tool_path(path: str | None) -> bool:
    if not path:
        return False
    norm = path.replace("\\", "/").lower()
    return "/tools/" in norm or norm.startswith("tools/")


def _is_primary_defect_finding(finding: ReviewFindingV2) -> bool:
    return _PRIMARY_DEFECT_TITLE_RE.search(finding.title) is not None


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
        if _is_primary_defect_finding(finding):
            _LOGGER.info(
                "Retaining primary defect candidate %s:%s "
                "despite unverified comparative remark '%s'",
                finding.path,
                finding.line,
                param,
            )
            return ClaimAuditResult(status="supported")
        return ClaimAuditResult(
            status="insufficient_context",
            reason=f"Claim asserts other tools/files use '{param}' without external block evidence",
        )

    if context_blocks is None:
        return ClaimAuditResult(status="supported")

    is_tool_claim = bool(re.search(r"\bother\s+tools\b", text, re.IGNORECASE))
    identifier_re = re.compile(rf"(?<![A-Za-z0-9_$]){re.escape(param)}(?![A-Za-z0-9_$])")
    matched_external = False
    has_tool_block = False
    for ref in external_refs:
        snippet = context_blocks.get(ref.block_id)
        if snippet is None:
            continue
        if _is_tool_path(snippet.path):
            has_tool_block = True
        if is_tool_claim and not _is_tool_path(snippet.path):
            continue
        if snippet.lines:
            cited = "\n".join(
                snippet.lines[ln]
                for ln in range(ref.start_line, ref.end_line + 1)
                if ln in snippet.lines
            )
            if cited and identifier_re.search(cited):
                matched_external = True
                break

    if matched_external:
        return ClaimAuditResult(status="supported")

    if is_tool_claim and not has_tool_block:
        reason = (
            f"Claim asserts other tools use '{param}' but cited external blocks are not tool files"
        )
    else:
        reason = f"Claim asserts other tools/files use '{param}' but cited external blocks omit it"
    return ClaimAuditResult(status="insufficient_context", reason=reason)


def _extract_comparative_param(text: str) -> str | None:
    for match in _COMPARATIVE_OTHER_TOOLS_PATTERN.finditer(text):
        param = match.group("param")
        quoted = bool(match.group("quoted"))
        keyword_suffix = bool(match.group("kw"))
        if param.lower() in _COMPARATIVE_STOPWORDS and not quoted:
            continue
        if quoted or keyword_suffix:
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
) -> TryCatchSpan | None:
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
        if isinstance(node, _PYTHON_FUNCTION_BOUNDARIES):
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


def _is_python_line_in_try_body(
    file_path: str,
    source_bytes: bytes,
    target_line: int,
) -> TryCatchSpan | None:
    tree = _parse_python_ast(file_path, source_bytes)
    if tree is None:
        return None

    lines = source_bytes.splitlines()
    if target_line < 1 or target_line > len(lines):
        return TryCatchSpan(in_try=False)

    target_raw_line = lines[target_line - 1]
    stripped = target_raw_line.lstrip()
    first_col = len(target_raw_line) - len(stripped)

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
        return TryCatchSpan(in_try=False)

    def _node_distance(node: ast.AST) -> tuple[int, int, int]:
        lineno = int(getattr(node, "lineno", 0) or 0)
        end_lineno = int(getattr(node, "end_lineno", None) or lineno)
        col_offset = int(getattr(node, "col_offset", 0) or 0)
        return (
            abs(lineno - target_line),
            abs(col_offset - first_col),
            end_lineno - lineno,
        )

    candidates.sort(key=_node_distance)

    curr: ast.AST | None = candidates[0]
    while curr is not None:
        if isinstance(curr, _PYTHON_FUNCTION_BOUNDARIES):
            return TryCatchSpan(in_try=False)
        if isinstance(curr, (ast.Try, ast.TryStar)) and curr.handlers and curr.body:
            body_end = max(stmt.end_lineno or stmt.lineno for stmt in curr.body)
            if curr.lineno <= target_line <= body_end:
                return TryCatchSpan(in_try=True, try_start=curr.lineno, try_end=body_end)
        curr = parents.get(curr)

    return TryCatchSpan(in_try=False)
