"""Review simulations of the rules playground (#311): pasted code reviewed under pasted rules.

Every refusal happens before a model call. The pasted code and rules are never logged or
quoted in an error: a rules diagnostic names a line and a column, never the text there.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from app.modules.reviews.application.diff_map import is_supported_path
from app.modules.reviews.application.policy_documents import missing_document, normalize_document
from app.modules.reviews.application.policy_errors import PolicyError, PolicyErrorCode
from app.modules.reviews.application.policy_snapshot import (
    AGENTS_DOCUMENT_PATH,
    DEFAULT_MAX_POLICY_TOKENS,
    RULES_DOCUMENT_PATH,
)
from app.modules.reviews.application.prompt_budget import (
    policy_document_tokens,
    token_counter_for,
)
from app.modules.reviews.application.review_ignore import parse_review_ignore

MAX_CONTENT_BYTES = 65_536
# The counter a fast run pins today (``app/worker.py``); the run that executes the
# simulation checks the budget again with the counter of its own profile.
SIMULATION_COUNTER_VERSION = "counter/v2"


class RulesSimulationMode(StrEnum):
    CODE = "code"
    DIFF = "diff"


@dataclass(frozen=True)
class RulesSimulationRequest:
    """``rules_md`` takes the role of ``.review/rules.md``; empty means service defaults only."""

    repository_id: UUID
    mode: RulesSimulationMode
    path: str
    content: str
    rules_md: str


@dataclass(frozen=True)
class RulesInputDiagnostic:
    """An RFS §9 diagnostic of the posted rules; the message has no location and no quote."""

    code: str
    message: str
    line: int | None
    column: int | None
    limit: int | None
    actual: int | None

    @classmethod
    def from_error(cls, error: PolicyError) -> RulesInputDiagnostic:
        return cls(
            code=error.code.value,
            message=error.safe_message,
            line=error.line,
            column=error.column,
            limit=error.limit,
            actual=error.actual,
        )


class RulesSimulationRejected(Exception):
    """A refused input; the exception text is the code alone."""

    def __init__(
        self,
        code: str,
        *,
        limit: int | None = None,
        actual: int | None = None,
        diagnostics: tuple[RulesInputDiagnostic, ...] = (),
    ) -> None:
        super().__init__(code)
        self.code = code
        self.limit = limit
        self.actual = actual
        self.diagnostics = diagnostics


def check_simulation_input(request: RulesSimulationRequest) -> None:
    """Refuse a path, code or rules the simulation cannot take (RULES_FORMAT_SPEC §3.3, §9)."""
    if not is_supported_path(request.path):
        raise RulesSimulationRejected("path_invalid")
    try:
        size = len(request.content.encode("utf-8"))
    except UnicodeEncodeError:
        raise RulesSimulationRejected("content_invalid") from None
    # PostgreSQL text cannot hold U+0000.
    if "\x00" in request.content:
        raise RulesSimulationRejected("content_invalid")
    if size > MAX_CONTENT_BYTES:
        raise RulesSimulationRejected("content_too_large", limit=MAX_CONTENT_BYTES, actual=size)
    if not request.rules_md:
        return
    try:
        _check_rules(request.rules_md)
    except PolicyError as error:
        # ``from None``: the error text of a mask quotes it, and the rules stay unquoted.
        raise RulesSimulationRejected(
            "rules_invalid", diagnostics=(RulesInputDiagnostic.from_error(error),)
        ) from None


def _check_rules(rules_md: str) -> None:
    try:
        raw = rules_md.encode("utf-8")
    except UnicodeEncodeError:
        raise PolicyError(
            PolicyErrorCode.DOCUMENT_DECODE_ERROR,
            "The policy document must be valid UTF-8",
            source_path=RULES_DOCUMENT_PATH,
        ) from None
    document = normalize_document(RULES_DOCUMENT_PATH, raw, blob_oid=_git_blob_oid(raw))
    parse_review_ignore(document.text)
    # The playground is detached from the repository: there is no AGENTS.md (#311 Scope).
    tokens = policy_document_tokens(
        document,
        missing_document(AGENTS_DOCUMENT_PATH),
        token_counter_for(SIMULATION_COUNTER_VERSION),
    )
    if tokens > DEFAULT_MAX_POLICY_TOKENS:
        raise PolicyError(
            PolicyErrorCode.PROMPT_BUDGET_EXCEEDED,
            "The rendered rules exceed the token budget of the policy documents",
            limit=DEFAULT_MAX_POLICY_TOKENS,
            actual=tokens,
        )


def _git_blob_oid(raw: bytes) -> str:
    """The Git object id the posted rules would have as a blob."""
    return hashlib.sha1(b"blob %d\0" % len(raw) + raw, usedforsecurity=False).hexdigest()
