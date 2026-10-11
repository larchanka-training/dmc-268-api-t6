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


class PolicyError(RunFailure):
    """A policy failure; the message is safe to show and never carries document content."""

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
