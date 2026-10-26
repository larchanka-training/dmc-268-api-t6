"""Policy failures of a run and their diagnostics (RULES_FORMAT_SPEC §9)."""

from __future__ import annotations

import unicodedata
from datetime import timedelta
from enum import StrEnum

from app.modules.reviews.application.run_failures import RunFailure


class PolicyErrorCode(StrEnum):
    DOCUMENT_TOO_LARGE = "policy_document_too_large"
    DOCUMENT_DECODE_ERROR = "policy_document_decode_error"
    DOCUMENT_CONTROL_CHARACTER = "policy_document_control_character"
    DOCUMENT_TYPE_UNSUPPORTED = "policy_document_type_unsupported"
    IGNORE_BLOCK_DUPLICATE = "policy_ignore_block_duplicate"
    IGNORE_BLOCK_NESTED = "policy_ignore_block_nested"
    IGNORE_BLOCK_UNCLOSED = "policy_ignore_block_unclosed"
    IGNORE_INFO_INVALID = "policy_ignore_info_invalid"
    IGNORE_PATTERN_INVALID = "policy_ignore_pattern_invalid"
    IGNORE_LIMIT_EXCEEDED = "policy_ignore_limit_exceeded"
    PATH_INVALID = "policy_path_invalid"
    MATCH_BUDGET_EXCEEDED = "policy_match_budget_exceeded"
    PROMPT_BUDGET_EXCEEDED = "policy_prompt_budget_exceeded"
    ACCESS_DENIED = "policy_access_denied"
    REVISION_UNAVAILABLE = "policy_revision_unavailable"
    FETCH_TRANSIENT = "policy_fetch_transient"
    RATE_LIMITED = "policy_rate_limited"
    SNAPSHOT_INTEGRITY_ERROR = "policy_snapshot_integrity_error"
    CONTEXT_VIOLATION = "policy_context_violation"


RETRYABLE_POLICY_ERROR_CODES = frozenset(
    {PolicyErrorCode.FETCH_TRANSIENT, PolicyErrorCode.RATE_LIMITED}
)


# Separates the technical message from the author-facing location in a stored message.
POLICY_DETAIL_SEPARATOR = " | "


def policy_error_detail(error_message: str | None) -> str | None:
    """The author-facing location of a stored policy failure, when it has one."""
    if not error_message or POLICY_DETAIL_SEPARATOR not in error_message:
        return None
    return error_message.split(POLICY_DETAIL_SEPARATOR, 1)[1]


_MAX_EXCERPT_CHARS = 80


def _safe_excerpt(text: str) -> str:
    """A short quote of author-written text that cannot break the markup around it."""
    cleaned = "".join(
        "?" if unicodedata.category(char) == "Cc" else "'" if char == "`" else char for char in text
    )
    return cleaned if len(cleaned) <= _MAX_EXCERPT_CHARS else cleaned[:_MAX_EXCERPT_CHARS] + "..."


class PolicyError(RunFailure):
    """A policy failure; the message is safe to show and never carries document content.

    The message ends with where the problem is: the document, the line and position,
    the offending mask, and the limit with the actual value when they are known. The
    mask is the only author-written text quoted, shortened and made safe for markup.
    When the count stopped at the limit (``actual_is_lower_bound``), the message says
    only that the limit was exceeded and never shows the stopped count as exact.
    """

    def __init__(
        self,
        code: PolicyErrorCode,
        message: str,
        *,
        source_path: str | None = None,
        policy_base_sha: str | None = None,
        line: int | None = None,
        column: int | None = None,
        pattern_index: int | None = None,
        limit: int | None = None,
        actual: int | None = None,
        excerpt: str | None = None,
        retry_after: timedelta | None = None,
        unit: str | None = None,
        actual_is_lower_bound: bool = False,
    ) -> None:
        parts = []
        if source_path is not None:
            parts.append(f"файл `{source_path}`")
        if line is not None:
            parts.append(f"строка {line}")
        if column is not None:
            parts.append(f"позиция {column}")
        if excerpt is not None:
            parts.append(f"маска `{_safe_excerpt(excerpt)}`")
        allowed = f"допустимо {limit} {unit}" if unit else f"допустимо {limit}"
        if limit is not None and actual_is_lower_bound:
            parts.append(f"фактически более {limit}, {allowed}")
        elif actual is not None and limit is not None:
            parts.append(f"фактически {actual}, {allowed}")
        elif limit is not None:
            parts.append(allowed)
        detail = ", ".join(parts)
        safe_message = message
        if detail:
            message = f"{message}{POLICY_DETAIL_SEPARATOR}{detail}"
        super().__init__(code.value, message, retry_after=retry_after)
        self.code = code
        self.safe_message = safe_message
        self.source_path = source_path
        self.policy_base_sha = policy_base_sha
        self.line = line
        self.column = column
        self.pattern_index = pattern_index
        self.limit = limit
        self.actual = actual
        self.excerpt = excerpt
        self.unit = unit
        self.actual_is_lower_bound = actual_is_lower_bound

    @property
    def retryable(self) -> bool:
        return self.code in RETRYABLE_POLICY_ERROR_CODES

    def located(self, source_path: str) -> PolicyError:
        """The same failure in a document its raise site could not name (a blob read)."""
        return PolicyError(
            self.code,
            self.safe_message,
            source_path=source_path,
            policy_base_sha=self.policy_base_sha,
            line=self.line,
            column=self.column,
            pattern_index=self.pattern_index,
            limit=self.limit,
            actual=self.actual,
            excerpt=self.excerpt,
            retry_after=self.retry_after,
            unit=self.unit,
            actual_is_lower_bound=self.actual_is_lower_bound,
        )

    def diagnostic(self) -> dict[str, str | int | bool | None]:
        """The RFS §9 diagnostic for the trace; its message has no location, unknowns are null."""
        return {
            "code": self.code.value,
            "source_path": self.source_path,
            "policy_base_sha": self.policy_base_sha,
            "message": self.safe_message,
            "line": self.line,
            "column": self.column,
            "pattern_index": self.pattern_index,
            "limit": self.limit,
            "actual": self.actual,
            "retryable": self.retryable,
        }
