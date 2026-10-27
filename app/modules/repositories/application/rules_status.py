"""Status of ``.review/rules.md`` on the default branch of a repository (#150).

RULES_FORMAT_SPEC §10. The API process holds no GitHub App key (docs/SECRETS.md), so
``webhook-worker`` reads the document and stores one result per repository; the API
only shows the stored result and asks for a new read. A read happens on onboarding, on a
push to the default branch, and when the API finds the stored result too old or failed
with a retryable error. A request is a durable row marker, so a lost wake-up only delays
it until the next sweep of the worker.

The read reuses the run loader: the default branch is resolved to a full commit SHA,
the document is found in the tree of that SHA and its blob is read by id, then
normalized and its ``review-ignore`` block parsed. A read that cannot prove the path is
absent (an ambiguous 404, a truncated tree) is ``unavailable``, never ``missing``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from app.common.application.unit_of_work import UnitOfWork
from app.modules.reviews.application.check_runs import failure_reason
from app.modules.reviews.application.policy_documents import (
    LoadPolicyDocuments,
    PolicyDocumentSource,
)
from app.modules.reviews.application.policy_errors import (
    PolicyError,
    PolicyErrorCode,
    policy_error_detail,
)
from app.modules.reviews.application.policy_snapshot import RULES_DOCUMENT_PATH
from app.modules.reviews.application.review_ignore import parse_review_ignore

_LOGGER = logging.getLogger(__name__)

# A stored result older than this is read again on the next GET.
RULES_STATUS_STALE_AFTER = timedelta(minutes=10)
# A retryable failure is read again on a GET only after this pause, or after the
# ``Retry-After`` of GitHub when it is longer, so that a page polled during a GitHub
# outage does not turn into one read per request. The age above still applies first, so
# a result older than 10 minutes is read again whatever ``Retry-After`` asked.
RULES_STATUS_RETRY_AFTER = timedelta(seconds=30)
# The longest ``Retry-After`` that is stored, as for the code index snapshot source.
RULES_STATUS_MAX_RETRY_AFTER = timedelta(days=1)
# How long a worker may hold a request before another one takes it over.
RULES_REFRESH_LEASE = timedelta(minutes=5)
RULES_REFRESH_BATCH = 20

# The run texts of these codes speak of a run; the settings page shows no run.
_STATUS_TEXTS = {
    PolicyErrorCode.DOCUMENT_TOO_LARGE: (
        "Документ правил превышает 64 КиБ. Ревью с ним не выполнится; уменьшите документ."
    ),
    PolicyErrorCode.REVISION_UNAVAILABLE: (
        "Ревизия default branch недоступна: GitHub не подтвердил, есть ли файл правил."
    ),
}
# Errors of the document itself; every other policy error means the read proved nothing.
_DOCUMENT_ERRORS = frozenset(
    {
        PolicyErrorCode.DOCUMENT_TOO_LARGE,
        PolicyErrorCode.DOCUMENT_DECODE_ERROR,
        PolicyErrorCode.DOCUMENT_CONTROL_CHARACTER,
        PolicyErrorCode.DOCUMENT_TYPE_UNSUPPORTED,
        PolicyErrorCode.IGNORE_BLOCK_DUPLICATE,
        PolicyErrorCode.IGNORE_BLOCK_NESTED,
        PolicyErrorCode.IGNORE_BLOCK_UNCLOSED,
        PolicyErrorCode.IGNORE_INFO_INVALID,
        PolicyErrorCode.IGNORE_PATTERN_INVALID,
        PolicyErrorCode.IGNORE_LIMIT_EXCEEDED,
    }
)


class RulesStatusKind(StrEnum):
    PENDING = "pending"
    PRESENT = "present"
    MISSING = "missing"
    INVALID = "invalid"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class RulesDiagnostic:
    """One RULES_FORMAT_SPEC §9 diagnostic; the message is the author-facing text."""

    code: str
    message: str
    line: int | None
    column: int | None
    retryable: bool
    limit: int | None = None
    actual: int | None = None
    # ``actual`` is only a lower bound (§9), such as the bytes of a stopped blob read.
    actual_is_lower_bound: bool = False
    pattern_index: int | None = None
    # The ``Retry-After`` of a rate limit; stored for the next GET, never shown.
    retry_after: timedelta | None = None

    @classmethod
    def from_error(cls, error: PolicyError) -> RulesDiagnostic:
        detail = policy_error_detail(error.message)
        text = _STATUS_TEXTS.get(error.code)
        return cls(
            code=error.code.value,
            message=(
                failure_reason(error.code.value, detail)
                if text is None
                else (f"{text} Где: {detail}." if detail else text)
            ),
            line=error.line,
            column=error.column,
            retryable=error.retryable,
            limit=error.limit,
            actual=error.actual,
            actual_is_lower_bound=error.actual_is_lower_bound,
            pattern_index=error.pattern_index,
            retry_after=(
                None
                if error.retry_after is None
                else min(error.retry_after, RULES_STATUS_MAX_RETRY_AFTER)
            ),
        )


@dataclass(frozen=True)
class RulesReading:
    """The result of one read by the worker; ``sha`` is None when no revision was resolved."""

    status: RulesStatusKind
    branch: str
    sha: str | None
    text: str | None = None
    checksum: str | None = None
    ignore_masks: tuple[str, ...] | None = None
    diagnostics: tuple[RulesDiagnostic, ...] = ()


@dataclass(frozen=True)
class RulesStatus:
    """What the portal shows; ``pending`` has no read yet, so no SHA and no time."""

    status: RulesStatusKind
    branch: str
    sha: str | None
    checked_at: datetime | None
    text: str | None = None
    checksum: str | None = None
    ignore_masks: tuple[str, ...] | None = None
    diagnostics: tuple[RulesDiagnostic, ...] = ()
    defaults_version: str | None = None
    path: str = RULES_DOCUMENT_PATH

    @property
    def retryable(self) -> bool:
        return any(item.retryable for item in self.diagnostics)

    @property
    def retry_pause(self) -> timedelta:
        """How long a retryable failure waits for a new read: 30 s or GitHub's ``Retry-After``."""
        delays = [item.retry_after for item in self.diagnostics if item.retry_after is not None]
        return max([RULES_STATUS_RETRY_AFTER, *delays])


@dataclass(frozen=True)
class StoredRulesStatus:
    """A visible repository with its stored result; ``status`` is None before the first read.

    ``installation_available`` is that of ``GET /api/repos/{repo_id}``: false once the App
    has lost the repository, which the worker then no longer reads. It has no default, so a
    store cannot report a lost repository as readable by leaving it out.
    """

    default_branch: str
    status: RulesStatus | None
    refresh_requested: bool
    installation_available: bool


def needs_refresh(stored: StoredRulesStatus, now: datetime) -> bool:
    """Whether a GET asks the worker for a new read; a pending request is not repeated."""
    if stored.refresh_requested:
        return False
    status = stored.status
    if status is None or status.checked_at is None:
        return True
    if status.checked_at <= now - RULES_STATUS_STALE_AFTER:
        return True
    return status.retryable and status.checked_at <= now - status.retry_pause


class RulesStatusStore(Protocol):
    """Only repositories of the caller's Workspaces are visible; flushes, never commits."""

    async def get(self, repository_id: UUID) -> StoredRulesStatus | None: ...

    async def request_refresh(self, repository_id: UUID) -> None:
        """Mark the repository for a new read and wake the worker on commit."""
        ...


class RulesStatusUnitOfWork(UnitOfWork, Protocol):
    @property
    def rules_status(self) -> RulesStatusStore: ...


def _no_installation_access(branch: str, now: datetime) -> RulesStatus:
    # The App cannot read the repository any more, so no read is requested: it would never
    # be answered and the page would stay ``pending``.
    error = PolicyError(
        PolicyErrorCode.ACCESS_DENIED, "The GitHub App has no access to the repository"
    )
    return RulesStatus(
        status=RulesStatusKind.UNAVAILABLE,
        branch=branch,
        sha=None,
        checked_at=now,
        diagnostics=(RulesDiagnostic.from_error(error),),
    )


class GetRulesStatus:
    """Return the stored status and, when it is due, request a new read in the same commit.

    A repository the App has lost (``installation_available`` false) is ``unavailable``
    with ``policy_access_denied`` and requests nothing.
    """

    def __init__(
        self,
        uow_factory: Callable[[], RulesStatusUnitOfWork],
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._uow_factory = uow_factory
        self._now = now

    async def execute(self, repository_id: UUID) -> RulesStatus | None:
        async with self._uow_factory() as uow:
            stored = await uow.rules_status.get(repository_id)
            if stored is None:
                return None
            if not stored.installation_available:
                return _no_installation_access(stored.default_branch, self._now())
            if needs_refresh(stored, self._now()):
                await uow.rules_status.request_refresh(repository_id)
                await uow.commit()
        if stored.status is not None:
            return stored.status
        return RulesStatus(
            status=RulesStatusKind.PENDING,
            branch=stored.default_branch,
            sha=None,
            checked_at=None,
        )


@dataclass(frozen=True)
class RulesRefreshTarget:
    """A claimed request with what the worker needs to read the repository."""

    repository_id: UUID
    installation_external_id: int
    repository_full_name: str
    # The stored default branch: shown when GitHub does not name the current one.
    default_branch: str
    requested_at: datetime
    # The lease end written by this claim; ``save`` checks it, so a read whose claim was
    # taken over after the lease cannot overwrite the newer read.
    claimed_until: datetime


@dataclass(frozen=True)
class BranchHead:
    branch: str
    sha: str


class RulesRefreshStore(Protocol):
    """Flush-only operations of the worker; repositories of every Workspace are visible."""

    async def claim(
        self, *, now: datetime, until: datetime, limit: int
    ) -> tuple[RulesRefreshTarget, ...]: ...

    async def save(
        self, target: RulesRefreshTarget, reading: RulesReading, *, checked_at: datetime
    ) -> bool:
        """Store the result and release the claim; False when the claim is no longer ours.

        The request is cleared only if it is still the one that was claimed, so a request
        made during the read survives and is answered by the next read.
        """
        ...

    async def request_for_provider_repository(
        self, installation_external_id: int, repository_external_id: int
    ) -> bool:
        """Mark a connected repository for a new read; False when there is none."""
        ...


class RulesRefreshUnitOfWork(UnitOfWork, Protocol):
    @property
    def rules(self) -> RulesRefreshStore: ...


class DefaultBranchHeadSource(Protocol):
    async def resolve(self, target: RulesRefreshTarget) -> BranchHead:
        """The current default branch and its full commit SHA; failures are ``PolicyError``."""
        ...


class RepositoryPolicySourceFactory(Protocol):
    def __call__(self, target: RulesRefreshTarget, sha: str) -> PolicyDocumentSource: ...


class ReadRulesDocument:
    """Read ``.review/rules.md`` of the default branch head without a database transaction."""

    def __init__(
        self, *, heads: DefaultBranchHeadSource, sources: RepositoryPolicySourceFactory
    ) -> None:
        self._heads = heads
        self._sources = sources

    async def read(self, target: RulesRefreshTarget) -> RulesReading:
        try:
            head = await self._heads.resolve(target)
        except PolicyError as error:
            return _unavailable(target.default_branch, None, error)
        loader = LoadPolicyDocuments(self._sources(target, head.sha), policy_base_sha=head.sha)
        try:
            loaded = await loader.load_document(RULES_DOCUMENT_PATH)
        except PolicyError as error:
            if error.code in _DOCUMENT_ERRORS:
                return _invalid(head, None, error)
            return _unavailable(head.branch, head.sha, error)
        document = loaded.document
        if loaded.raw is None or document.text is None:
            return RulesReading(RulesStatusKind.MISSING, head.branch, head.sha)
        try:
            policy = parse_review_ignore(document.text, policy_base_sha=head.sha)
        except PolicyError as error:
            return _invalid(head, document.text, error)
        return RulesReading(
            RulesStatusKind.PRESENT,
            head.branch,
            head.sha,
            text=document.text,
            checksum=document.raw_sha256,
            ignore_masks=policy.patterns,
        )


def _invalid(head: BranchHead, text: str | None, error: PolicyError) -> RulesReading:
    return RulesReading(
        RulesStatusKind.INVALID,
        head.branch,
        head.sha,
        text=text,
        diagnostics=(RulesDiagnostic.from_error(error),),
    )


def _unavailable(branch: str, sha: str | None, error: PolicyError) -> RulesReading:
    return RulesReading(
        RulesStatusKind.UNAVAILABLE,
        branch,
        sha,
        diagnostics=(RulesDiagnostic.from_error(error),),
    )


def _unexpected_failure(target: RulesRefreshTarget) -> RulesReading:
    error = PolicyError(PolicyErrorCode.FETCH_TRANSIENT, "The rules read failed unexpectedly")
    return _unavailable(target.default_branch, None, error)


class RulesDocumentReader(Protocol):
    async def read(self, target: RulesRefreshTarget) -> RulesReading: ...


class RefreshRulesStatuses:
    """Answer the pending read requests; called by the ``webhook-worker`` sweep.

    Claim, read and save are three steps: no transaction spans a GitHub call. A worker
    that dies after its claim leaves the request to the next sweep once the lease ends.
    """

    def __init__(
        self,
        *,
        uow_factory: Callable[[], RulesRefreshUnitOfWork],
        reader: RulesDocumentReader,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._uow_factory = uow_factory
        self._reader = reader
        self._now = now

    async def execute(self, *, limit: int = RULES_REFRESH_BATCH) -> int:
        now = self._now()
        async with self._uow_factory() as uow:
            targets = await uow.rules.claim(now=now, until=now + RULES_REFRESH_LEASE, limit=limit)
            await uow.commit()
        for target in targets:
            try:
                reading = await self._reader.read(target)
            except Exception:
                # A bug or an unexpected answer: shown as a temporary failure, so the page
                # does not keep an old result without an error and a GET asks again.
                _LOGGER.exception(
                    "Rules status read failed for repository %s", target.repository_id
                )
                reading = _unexpected_failure(target)
            try:
                async with self._uow_factory() as uow:
                    saved = await uow.rules.save(target, reading, checked_at=self._now())
                    await uow.commit()
            except Exception:
                # The claim ends with its lease and the next sweep tries again.
                _LOGGER.exception(
                    "Rules status save failed for repository %s", target.repository_id
                )
                continue
            if not saved:
                _LOGGER.warning(
                    "Rules status read of repository %s dropped: its claim was taken over",
                    target.repository_id,
                )
                continue
            # Status and code only: the document text never reaches the log.
            _LOGGER.info(
                "Rules status of repository %s: %s%s",
                target.repository_id,
                reading.status.value,
                f" {reading.diagnostics[0].code}" if reading.diagnostics else "",
            )
        return len(targets)


class RequestRulesRefreshOnPush:
    """Request a new read after a push to the default branch of a connected repository."""

    def __init__(self, uow_factory: Callable[[], RulesRefreshUnitOfWork]) -> None:
        self._uow_factory = uow_factory

    async def execute(self, installation_external_id: int, repository_external_id: int) -> bool:
        async with self._uow_factory() as uow:
            requested = await uow.rules.request_for_provider_repository(
                installation_external_id, repository_external_id
            )
            await uow.commit()
        return requested
