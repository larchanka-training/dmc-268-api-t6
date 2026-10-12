"""Policy failures of a run and their diagnostics (RULES_FORMAT_SPEC §9)."""

from __future__ import annotations

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


class PolicyError(RunFailure):
    """A policy failure; the message is safe to show and never carries document content.

    The message ends with where the problem is: the document, the line and position,
    and the limit with the actual value when they are known.
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
        retry_after: timedelta | None = None,
    ) -> None:
        parts = []
        if source_path is not None:
            parts.append(f"файл `{source_path}`")
        if line is not None:
            parts.append(f"строка {line}")
        if column is not None:
            parts.append(f"позиция {column}")
        if actual is not None and limit is not None:
            parts.append(f"фактически {actual}, допустимо {limit}")
        elif limit is not None:
            parts.append(f"допустимо {limit}")
        detail = ", ".join(parts)
        if detail:
            message = f"{message}{POLICY_DETAIL_SEPARATOR}{detail}"
        super().__init__(code.value, message, retry_after=retry_after)
        self.code = code
        self.source_path = source_path
        self.policy_base_sha = policy_base_sha
        self.line = line
        self.column = column
        self.pattern_index = pattern_index
        self.limit = limit
        self.actual = actual

    @property
    def retryable(self) -> bool:
        return self.code in RETRYABLE_POLICY_ERROR_CODES
