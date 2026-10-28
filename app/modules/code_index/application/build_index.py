"""Use case `BuildRepositoryIndex`: one job of the graph indexer (ADR 0001 §6-§7, #142).

The `index.repo` message only wakes the job: it reads the default-branch HEAD at its
start and indexes that commit, in full or incrementally from `indexed_commit_sha`.

- Every store call is a short transaction of its own, and no network call runs inside
  one (AC7); a lost lease stops the job without a write and without a retry (AC12).
- Only a limit or an unsafe archive fails the index. Any other terminal failure releases
  the lease with `last_error_code` and keeps the last publication (ADR §6.1, §7).
- The archive passes and the exclusion scan run in threads that check the monotonic job
  deadline themselves; a parse checks it before its parser call and after its result,
  and the job checks it again before each write and before the publication. The importer
  scan runs in a thread without a check of its own. A network call is cut at the
  deadline too; a parser call is not, so one file's parser wait bounds the overshoot.
- Go package members and package `__init__.py` files are read in a pass of their own
  before the parse pass, because an importer can come before its target in the archive;
  memory holds their fact summaries, never content. `parsed_files` bounds the paths both
  passes parse, each once, and `symbols` the symbols the summaries hold.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import posixpath
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import timedelta
from enum import StrEnum
from types import MappingProxyType
from uuid import UUID

from app.modules.code_index.application.exclusions import (
    INDEX_EXCLUSIONS_VERSION,
    ExclusionClass,
    IndexExclusions,
)
from app.modules.code_index.application.index_resolution import (
    IndexImportResolver,
    affected_importers,
)
from app.modules.code_index.application.index_store import (
    ClaimRefusal,
    ClaimRefused,
    CodeIndexUnitOfWork,
    ImportRecord,
    IndexAdmission,
    IndexBuildStats,
    IndexCounts,
    IndexedFile,
    IndexFence,
    IndexLeaseLost,
    IndexProfile,
    IndexState,
    IndexStorage,
    LeaseClaim,
)
from app.modules.code_index.application.ports import (
    IndexedRepository,
    IndexRepositoryReader,
    IndexWakeups,
)
from app.modules.code_index.application.symbol_rows import file_rows, symbol_rows
from app.modules.code_index.application.vcs import (
    Ancestry,
    ArchiveEntry,
    ArchiveReader,
    ArchiveScan,
    ArchiveUnsafe,
    BranchHead,
    ChangeStatus,
    Comparison,
    IndexLimitExceeded,
    RepositorySnapshotSource,
    SourceArchive,
    VcsAccessDenied,
    VcsUnavailable,
)
from app.modules.code_index.domain.codes import INTERNAL_ERROR
from app.modules.code_index.domain.limits import IndexLimits
from app.modules.reviews.application.ast_resolution import (
    BINARY_SOURCE,
    FileUnavailable,
    RevisionTree,
    go_module_path,
)
from app.modules.reviews.application.ast_symbols import (
    AstFacts,
    AstFailureReason,
    AstLanguage,
    ParseRequest,
    SourceParser,
    language_for_path,
)
from app.modules.reviews.application.policy_errors import PolicyError
from app.modules.reviews.application.review_ignore import (
    BUILTIN_EXCLUSIONS_VERSION,
    IgnoreMatcher,
    parse_review_ignore,
)
from app.modules.reviews.application.vcs_diff import is_binary_content

_LOGGER = logging.getLogger(__name__)

_GO_MOD = "go.mod"
_GO_MODULE_FILES = frozenset({_GO_MOD, "go.work"})
_DOCUMENTS = frozenset({_GO_MOD})
# Statuses that change the path set; a modified file keeps its path.
_SAME_PATH = frozenset({ChangeStatus.MODIFIED, ChangeStatus.UNCHANGED})


def limits_profile_version(limits: IndexLimits) -> str:
    """`limits/v1:` and a digest of what changes the content of an index: the read and
    parse limits and the versions of both exclusion sets. It fits `String(64)`."""
    inputs = {
        "read_bytes": limits.read_bytes,
        "parse_bytes": limits.parse_bytes,
        "builtin_exclusions": BUILTIN_EXCLUSIONS_VERSION,
        "index_exclusions": INDEX_EXCLUSIONS_VERSION,
    }
    digest = hashlib.sha256(json.dumps(inputs, sort_keys=True).encode()).hexdigest()
    return f"limits/v1:{digest[:32]}"


def parser_profile_version(profiles: Iterable[str]) -> str:
    """The sha256 of the sorted `profile_version` of every parser language."""
    return hashlib.sha256("\n".join(sorted(profiles)).encode()).hexdigest()


@dataclass(frozen=True)
class IndexVersions:
    """The versions an index is built with; the composition root computes them."""

    extractor_version: str
    resolver_version: str
    parser_profile_version: str
    limits_profile_version: str

    def profile(self, default_branch: str, exclusions_digest: str) -> IndexProfile:
        return IndexProfile(
            self.extractor_version,
            self.resolver_version,
            self.parser_profile_version,
            self.limits_profile_version,
            default_branch,
            exclusions_digest,
        )

    def built(self, profile: IndexProfile) -> bool:
        """Whether `profile` was built with these versions."""
        return self.profile(profile.default_branch, profile.exclusions_digest) == profile


@dataclass(frozen=True)
class IndexBuildSettings:
    """How one job holds its lease and sizes its transactions."""

    admission: IndexAdmission
    lease_seconds: int = 1_200
    # Discarding abandoned rows longer than this releases the lease and wakes the job.
    soft_cutoff_seconds: int = 300
    discard_batch_rows: int = 10_000
    # Files written per transaction, and files read per archive batch.
    batch_files: int = 50
    # Rows per read of the stored files and import records.
    read_page_rows: int = 1_000


class BuildStatus(StrEnum):
    """How a job ended; PR 3 maps each status to its queue action."""

    PUBLISHED = "published"
    UNCHANGED = "unchanged"
    EMPTY = "empty"
    STALE = "stale"
    LEASED = "leased"
    BUSY = "busy"
    # `failed` was written: a limit, an unsafe archive or a storage quota at the claim.
    FAILED = "failed"
    # The lease was released with a terminal `last_error_code` (access, policy).
    RELEASED = "released"
    # The lease was released with a code a later attempt can heal.
    TRANSIENT = "transient"
    LEASE_LOST = "lease_lost"
    # The job woke the repository again: the soft cutoff or a publication miss.
    REWAKE = "rewake"


class BuildMode(StrEnum):
    FULL = "full"
    INCREMENTAL = "incremental"


class FullBuildReason(StrEnum):
    """Why a job built the index in full (ADR §6.3)."""

    NO_INDEX = "no_index"
    PROFILE_CHANGED = "profile_changed"
    BRANCH_CHANGED = "branch_changed"
    BASE_UNKNOWN = "base_unknown"
    COMPARE_INCOMPLETE = "compare_incomplete"
    NOT_AHEAD = "not_ahead"
    GO_MODULE_CHANGED = "go_module_changed"
    EXCLUSIONS_CHANGED = "exclusions_changed"
    TOO_MANY_CHANGES = "too_many_changes"


_NO_EXCLUSIONS: Mapping[ExclusionClass, int] = MappingProxyType({})


@dataclass(frozen=True)
class BuildOutcome:
    """The result of one job; it carries ids, counters and codes, never paths."""

    status: BuildStatus
    error_code: str | None = None
    http_status: int | None = None
    retry_after: timedelta | None = None
    mode: BuildMode | None = None
    full_reason: FullBuildReason | None = None
    commit_sha: str | None = None
    # Files of this build per exclusion class.
    exclusions: Mapping[ExclusionClass, int] = field(default=_NO_EXCLUSIONS)
    provider_skips: Mapping[str, int] = field(default_factory=dict)
    parsed_files: int = 0
    written_files: int = 0
    deleted_files: int = 0
    github_request_count: int = 0
    downloaded_bytes: int = 0
    duration_ms: int = 0

    @property
    def skipped_unsupported_language(self) -> int:
        return self.exclusions.get(ExclusionClass.UNSUPPORTED_LANGUAGE, 0)

    @property
    def skipped_too_large(self) -> int:
        return self.exclusions.get(ExclusionClass.OVER_READ_LIMIT, 0) + self.exclusions.get(
            ExclusionClass.OVER_PARSE_LIMIT, 0
        )

    @property
    def parse_failed(self) -> int:
        return self.exclusions.get(ExclusionClass.PARSE_FAILED, 0)


_REFUSALS = {
    ClaimRefusal.STALE: BuildStatus.STALE,
    ClaimRefusal.LEASED: BuildStatus.LEASED,
    ClaimRefusal.BUSY: BuildStatus.BUSY,
    ClaimRefusal.QUOTA: BuildStatus.FAILED,
}


@dataclass(frozen=True)
class _Plan:
    """What a build examines: `paths` of the archive, the stored paths it `closes`, and
    the visible rows it keeps, which count toward the limits."""

    full: bool
    paths: tuple[str, ...]
    closes: frozenset[str]
    kept: IndexCounts


def _go_module(scan: ArchiveScan, limits: IndexLimits) -> str | None:
    """The module path of the root `go.mod`, read as the review reads it."""
    raw = scan.documents.get(_GO_MOD)
    if raw is None or len(raw) > limits.parse_bytes or is_binary_content(raw):
        return None
    try:
        return go_module_path(raw.decode("utf-8", errors="strict"))
    except UnicodeDecodeError:
        return None


def _summary(facts: AstFacts) -> AstFacts:
    """What the resolver reads of a Go member or a package `__init__.py`: the package
    clause, the build constraints and the exported top-level declarations."""
    symbols = tuple(
        replace(
            symbol,
            signature_ranges=(),
            docstring_ranges=(),
            references=(),
            member_references=(),
            local_bindings=(),
        )
        for symbol in facts.symbols
        if symbol.enclosing is None and symbol.exported
    )
    return replace(facts, symbols=symbols, imports=(), exports=())


class _TargetFacts:
    """`FactsProvider` over the summaries read before the parse pass."""

    def __init__(self, facts: Mapping[str, AstFacts | FileUnavailable]) -> None:
        self._facts = facts

    async def facts_for(self, path: str) -> AstFacts | FileUnavailable:
        return self._facts.get(path, FileUnavailable("missing"))


def _check_deadline(deadline: float) -> None:
    if time.monotonic() > deadline:
        raise IndexLimitExceeded("deadline")


def _admit(
    exclusions: IndexExclusions,
    entries: Mapping[str, ArchiveEntry],
    paths: Sequence[str],
    deadline: float,
) -> tuple[list[str], list[str]]:
    """The admitted files among `paths`, counting the excluded ones, and every Go file and
    package `__init__.py` of the archive that a resolved import may read."""
    admitted = []
    for path in paths:
        _check_deadline(deadline)
        entry = entries[path]
        if exclusions.admit_entry(path, symlink=entry.symlink, size=entry.size) is None:
            admitted.append(path)
    languages = {language_for_path(path) for path in admitted}
    targets = []
    for path, entry in entries.items():
        go = AstLanguage.GO in languages and path.endswith(".go")
        package = AstLanguage.PYTHON in languages and posixpath.basename(path) == "__init__.py"
        if (go or package) and not entry.symlink:
            _check_deadline(deadline)
            if exclusions.path_class(path) is None:
                targets.append(path)
    return admitted, targets


def _full_reason(comparison: Comparison | None) -> FullBuildReason | None:
    """Why a compare cannot start an incremental build (ADR §6.3), if it cannot."""
    if comparison is None:
        return FullBuildReason.BASE_UNKNOWN
    if not comparison.complete:
        return FullBuildReason.COMPARE_INCOMPLETE
    if comparison.ancestry is not Ancestry.AHEAD:
        return FullBuildReason.NOT_AHEAD
    touched = {item.path for item in comparison.files}
    touched |= {item.previous_path for item in comparison.files if item.previous_path}
    if any(posixpath.basename(path) in _GO_MODULE_FILES for path in touched):
        return FullBuildReason.GO_MODULE_CHANGED
    return None


def _same_file(entry: ArchiveEntry | None, blob_sha: str) -> bool:
    return entry is not None and not entry.symlink and entry.blob_sha == blob_sha


class BuildRepositoryIndex:
    """Build the index of one repository at its default-branch HEAD.

    `sources` builds the VCS port of a repository; `parser` is the indexer's own, not
    the review's. `versions` are what the published profile records.
    """

    def __init__(
        self,
        *,
        uow: Callable[[], CodeIndexUnitOfWork],
        repositories: IndexRepositoryReader,
        sources: Callable[[IndexedRepository], RepositorySnapshotSource],
        archives: ArchiveReader,
        parser: SourceParser,
        wakeups: IndexWakeups,
        versions: IndexVersions,
        limits: IndexLimits,
        settings: IndexBuildSettings,
    ) -> None:
        self._uow = uow
        self._repositories = repositories
        self._sources = sources
        self._archives = archives
        self._parser = parser
        self._wakeups = wakeups
        self._versions = versions
        self._limits = limits
        self._settings = settings

    async def execute(self, repository_id: UUID, generation: int) -> BuildOutcome:
        started = time.monotonic()
        repository = await self._repositories.get(repository_id)
        if repository is None:
            outcome = BuildOutcome(BuildStatus.STALE)
        else:
            outcome = await self._claimed(repository, generation, started)
        outcome = replace(outcome, duration_ms=_elapsed_ms(started))
        _log(repository_id, generation, outcome)
        return outcome

    async def _claimed(
        self, repository: IndexedRepository, generation: int, started: float
    ) -> BuildOutcome:
        leased: LeaseClaim | None = None
        try:
            async with self._uow() as uow:
                claim = await uow.index.claim_lease(
                    repository.repository_id,
                    generation,
                    self._settings.lease_seconds,
                    self._settings.admission,
                )
                # A QUOTA refusal wrote `failed`, so a refusal is committed too.
                await uow.commit()
                leased = claim if isinstance(claim, LeaseClaim) else None
            if isinstance(claim, ClaimRefused):
                return BuildOutcome(_REFUSALS[claim.reason], error_code=claim.error_code)
            job = _Job(
                uow=self._uow,
                archives=self._archives,
                parser=self._parser,
                wakeups=self._wakeups,
                versions=self._versions,
                limits=self._limits,
                settings=self._settings,
                repository_id=repository.repository_id,
                claim=claim,
                source=self._sources(repository),
                started=started,
            )
        except BaseException as error:
            # The lease is held from its commit on: the end of the claim transaction and
            # the source factory must not leave it to expire. No source has made a
            # request yet, and nothing was downloaded.
            if leased is not None:
                stats = IndexBuildStats(0, _elapsed_ms(started), 0)
                code = _unexpected_error_code(error)
                await _abandon(self._uow, repository.repository_id, leased.fence, code, stats)
            raise
        return await job.run()


async def _release_lease(
    uow: Callable[[], CodeIndexUnitOfWork],
    repository_id: UUID,
    fence: IndexFence,
    last_error_code: str | None,
    stats: IndexBuildStats,
    *,
    terminal_head_sha: str | None = None,
    expected_target_sha: str | None = None,
) -> None:
    # A job that ends without a publication records its metrics too (#142).
    async with uow() as unit:
        await unit.index.release(
            repository_id,
            fence,
            last_error_code=last_error_code,
            stats=stats,
            terminal_head_sha=terminal_head_sha,
            expected_target_sha=expected_target_sha,
        )
        await unit.commit()


def _unexpected_error_code(error: BaseException) -> str | None:
    """`internal_error` for an `Exception`; a cancellation or another non-`Exception`
    `BaseException` is not a failure."""
    return INTERNAL_ERROR if isinstance(error, Exception) else None


async def _abandon(
    uow: Callable[[], CodeIndexUnitOfWork],
    repository_id: UUID,
    fence: IndexFence,
    last_error_code: str | None,
    stats: IndexBuildStats,
) -> None:
    """Best effort after an unexpected error: release the lease with its `last_error_code`."""
    try:
        await asyncio.shield(_release_lease(uow, repository_id, fence, last_error_code, stats))
    except Exception:
        _LOGGER.warning(
            "code_index lease release after an error failed repository_id=%s",
            repository_id,
            exc_info=True,
        )


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _log(repository_id: UUID, generation: int, outcome: BuildOutcome) -> None:
    # SD §13: ids, statuses, codes and counters only; never content or a client path.
    skipped = ",".join(
        f"{exclusion.value}:{count}" for exclusion, count in outcome.exclusions.items() if count
    )
    provider_skipped = ",".join(
        f"{reason}:{count}" for reason, count in sorted(outcome.provider_skips.items()) if count
    )
    _LOGGER.info(
        "code_index build repository_id=%s generation=%d status=%s mode=%s reason=%s "
        "error=%s http_status=%s parsed=%d written=%d deleted=%d skipped=%s "
        "provider_skipped=%s requests=%d "
        "downloaded_bytes=%d duration_ms=%d",
        repository_id,
        generation,
        outcome.status.value,
        outcome.mode.value if outcome.mode else "-",
        outcome.full_reason.value if outcome.full_reason else "-",
        outcome.error_code or "-",
        outcome.http_status if outcome.http_status is not None else "-",
        outcome.parsed_files,
        outcome.written_files,
        outcome.deleted_files,
        skipped or "-",
        provider_skipped or "-",
        outcome.github_request_count,
        outcome.downloaded_bytes,
        outcome.duration_ms,
    )


class _Job:
    """One leased attempt; it keeps the counters its outcome reports."""

    def __init__(
        self,
        *,
        uow: Callable[[], CodeIndexUnitOfWork],
        archives: ArchiveReader,
        parser: SourceParser,
        wakeups: IndexWakeups,
        versions: IndexVersions,
        limits: IndexLimits,
        settings: IndexBuildSettings,
        repository_id: UUID,
        claim: LeaseClaim,
        source: RepositorySnapshotSource,
        started: float,
    ) -> None:
        self._uow = uow
        self._archives = archives
        self._parser = parser
        self._wakeups = wakeups
        self._versions = versions
        self._limits = limits
        self._settings = settings
        self._id = repository_id
        self._claim = claim
        self._fence = claim.fence
        self._source = source
        self._started = started
        self._deadline = started + self._limits.deadline_seconds
        self._mode: BuildMode | None = None
        self._full_reason: FullBuildReason | None = None
        self._commit_sha: str | None = None
        self._exclusions: IndexExclusions | None = None
        self._provider_skips: Mapping[str, int] = {}
        self._http_status: int | None = None
        self._pin_terminal_head = False
        self._downloaded = 0
        # Every path this job parsed, in either pass: `parsed_files` bounds the work.
        self._parsed: set[str] = set()
        self._written = 0
        self._deleted = 0

    async def run(self) -> BuildOutcome:
        try:
            return await self._build()
        except IndexLeaseLost:
            return self._outcome(BuildStatus.LEASE_LOST)
        except (IndexLimitExceeded, ArchiveUnsafe) as error:
            return await self._fail(error.code)
        except VcsUnavailable as error:
            self._http_status = error.http_status
            return await self._release(BuildStatus.TRANSIENT, error.code, error.retry_after)
        except VcsAccessDenied as error:
            self._http_status = error.http_status
            self._pin_terminal_head = error.pin_head
            return await self._release(BuildStatus.RELEASED, error.code)
        except PolicyError as error:
            self._http_status = error.http_status
            self._pin_terminal_head = error.pin_head
            return await self._release(BuildStatus.RELEASED, error.error_code)
        except BaseException as error:
            await self._abandon(_unexpected_error_code(error))
            raise

    # --- outcomes -----------------------------------------------------------------

    def _outcome(
        self,
        status: BuildStatus,
        error_code: str | None = None,
        retry_after: timedelta | None = None,
    ) -> BuildOutcome:
        exclusions = self._exclusions
        return BuildOutcome(
            status,
            error_code=error_code,
            http_status=self._http_status,
            retry_after=retry_after,
            mode=self._mode,
            full_reason=self._full_reason,
            commit_sha=self._commit_sha,
            exclusions=MappingProxyType(dict(exclusions.counts)) if exclusions else _NO_EXCLUSIONS,
            provider_skips=MappingProxyType(dict(self._provider_skips)),
            parsed_files=len(self._parsed),
            written_files=self._written,
            deleted_files=self._deleted,
            github_request_count=self._source.request_count,
            downloaded_bytes=self._downloaded,
        )

    def _stats(self) -> IndexBuildStats:
        return IndexBuildStats(
            self._downloaded,
            _elapsed_ms(self._started),
            self._source.request_count,
            http_status=self._http_status,
        )

    async def _fail(self, error_code: str) -> BuildOutcome:
        """ADR §7: `failed` in a fresh transaction that a cancellation does not cut."""

        async def mark_failed() -> None:
            async with self._uow() as uow:
                await uow.index.mark_failed(self._id, self._fence, error_code, stats=self._stats())
                await uow.commit()

        try:
            await asyncio.shield(mark_failed())
        except IndexLeaseLost:
            return self._outcome(BuildStatus.LEASE_LOST)
        return self._outcome(BuildStatus.FAILED, error_code)

    async def _release(
        self, status: BuildStatus, error_code: str, retry_after: timedelta | None = None
    ) -> BuildOutcome:
        # A source may mark a refusal such as Bitbucket static-token 401 as final for
        # the observed head. Other terminal refusals keep the original target discovery.
        observed_head = (
            self._commit_sha if status is BuildStatus.RELEASED and self._pin_terminal_head else None
        )
        try:
            await asyncio.shield(self._release_lease(error_code, terminal_head_sha=observed_head))
            # A TRANSIENT message is retried and comes back to the target anyway.
            if status is BuildStatus.RELEASED:
                await self._wake_if_the_target_moved(observed_head)
        except IndexLeaseLost:
            return self._outcome(BuildStatus.LEASE_LOST)
        return self._outcome(status, error_code, retry_after)

    async def _abandon(self, last_error_code: str | None) -> None:
        await _abandon(self._uow, self._id, self._fence, last_error_code, self._stats())

    async def _release_lease(
        self, last_error_code: str | None, *, terminal_head_sha: str | None = None
    ) -> None:
        await _release_lease(
            self._uow,
            self._id,
            self._fence,
            last_error_code,
            self._stats(),
            terminal_head_sha=terminal_head_sha,
            expected_target_sha=self._claim.target_commit_sha,
        )

    async def _wake(self) -> None:
        await self._wakeups.wake(self._id, self._fence.generation)

    async def _wake_if_the_target_moved(self, observed_head: str | None = None) -> None:
        """ADR §6.2: a wake-up refused as LEASED while this job held the lease left its
        target on the state row; a target other than the claim snapshot wakes the job."""
        after = await self._state()
        if after.target_commit_sha != (observed_head or self._claim.target_commit_sha):
            await self._wake()

    # --- the build ----------------------------------------------------------------

    async def _build(self) -> BuildOutcome:
        if not await self._discard_abandoned():
            await self._release_lease(None)
            await self._wake()
            return self._outcome(BuildStatus.REWAKE)
        head = await self._network(self._source.head)
        if head is None:
            # AC8: an empty repository or one without a default branch is no error.
            await self._release_lease(None)
            await self._wake_if_the_target_moved()
            return self._outcome(BuildStatus.EMPTY)
        self._commit_sha = head.commit_sha
        state = await self._state()
        profile = state.profile
        if (
            state.indexed_commit_sha == head.commit_sha
            and profile is not None
            and self._versions.built(profile)
            and profile.default_branch == head.branch
        ):
            await self._release_lease(None)
            await self._wake_if_the_target_moved()
            return self._outcome(BuildStatus.UNCHANGED)
        comparison = await self._comparison(state, head)
        document = await self._network(lambda: self._source.rules_document(head.commit_sha))
        policy = parse_review_ignore(document.text)
        archive = await self._network(
            lambda: self._source.archive(head.commit_sha, max_bytes=self._limits.download_bytes)
        )
        self._provider_skips = archive.provider_skips
        self._downloaded = len(archive.content)
        scan = await self._archives.scan(archive, documents=_DOCUMENTS, deadline=self._deadline)
        exclusions = IndexExclusions(IgnoreMatcher(policy), self._limits)
        self._exclusions = exclusions
        if (
            comparison is not None
            and profile is not None
            and profile.exclusions_digest != policy.digest
        ):
            self._full(FullBuildReason.EXCLUSIONS_CHANGED)
            comparison = None
        go_module = _go_module(scan, self._limits)
        plan = None
        if comparison is not None:
            plan = await self._incremental(state, comparison, scan, go_module)
        if plan is None:
            plan = _Plan(True, tuple(sorted(scan.entries)), frozenset(), IndexCounts(0, 0, 0))
        else:
            self._mode = BuildMode.INCREMENTAL
        written = await self._index(archive, scan, exclusions, go_module, plan)
        deleted = tuple(sorted(plan.closes - written))
        self._written, self._deleted = len(written), len(deleted)
        _check_deadline(self._deadline)
        async with self._uow() as uow:
            published = await uow.index.publish(
                self._id,
                self._fence,
                base_commit_sha=state.indexed_commit_sha,
                commit_sha=head.commit_sha,
                full=plan.full,
                deleted_paths=deleted,
                profile=self._versions.profile(head.branch, policy.digest),
                stats=self._stats(),
            )
            await uow.commit()
        if not published:
            # A compare-and-set miss spent the lease; a new wake-up retries (ADR §6.2).
            await self._wake()
            return self._outcome(BuildStatus.REWAKE)
        await self._wake_if_the_target_moved()
        return self._outcome(BuildStatus.PUBLISHED)

    async def _network[T](self, call: Callable[[], Awaitable[T]]) -> T:
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise IndexLimitExceeded("deadline")
        scope = asyncio.timeout(remaining)
        try:
            async with scope:
                return await call()
        except TimeoutError:
            if scope.expired():
                raise IndexLimitExceeded("deadline") from None
            raise

    async def _discard_abandoned(self) -> bool:
        """T3 in portions; `False` when it outlasts the soft cutoff (ADR §6.2)."""
        rows = self._settings.discard_batch_rows
        while True:
            async with self._uow() as uow:
                deleted = await uow.index.discard_abandoned(self._id, self._fence, rows)
                await uow.commit()
            if deleted < rows:
                return True
            if time.monotonic() - self._started >= self._settings.soft_cutoff_seconds:
                return False

    async def _state(self) -> IndexState:
        async with self._uow() as uow:
            state = await uow.index.get_state(self._id)
        if state is None:
            raise IndexLeaseLost("the index state row is gone")
        return state

    def _full(self, reason: FullBuildReason) -> None:
        self._mode = BuildMode.FULL
        self._full_reason = reason

    async def _comparison(self, state: IndexState, head: BranchHead) -> Comparison | None:
        """The compare an incremental build starts from, or `None` for a full build."""
        profile = state.profile
        indexed = state.indexed_commit_sha
        reason = None
        comparison = None
        if indexed is None or profile is None:
            reason = FullBuildReason.NO_INDEX
        elif not self._versions.built(profile):
            reason = FullBuildReason.PROFILE_CHANGED
        elif profile.default_branch != head.branch:
            reason = FullBuildReason.BRANCH_CHANGED
        else:
            comparison = await self._network(lambda: self._source.compare(indexed, head.commit_sha))
            reason = _full_reason(comparison)
        if reason is not None:
            self._full(reason)
            return None
        return comparison

    async def _incremental(
        self,
        state: IndexState,
        comparison: Comparison,
        scan: ArchiveScan,
        go_module: str | None,
    ) -> _Plan | None:
        """Steps 8 and 10: the change set C, the importers R and the rows kept, or `None`
        when the build has to be full."""
        stored = await self._stored_files()
        compared: set[str] = set()
        delta: set[str] = set()
        for item in comparison.files:
            compared.add(item.path)
            if item.previous_path:
                compared.add(item.previous_path)
                delta.add(item.previous_path)
            if item.status not in _SAME_PATH:
                delta.add(item.path)
        # The blob diff catches what the compare does not list, a type change included.
        changed = compared | {
            path
            for path, blob_sha in stored.items()
            if not _same_file(scan.entries.get(path), blob_sha)
        }
        # A path that is gone, a symlink or not indexed before may change a resolution:
        # too wide a delta costs re-extraction, too narrow a one breaks incremental = full.
        for path in changed:
            entry = scan.entries.get(path)
            if entry is None or entry.symlink or path not in stored:
                delta.add(path)
        limit = self._limits.incremental_files
        if len(changed) > limit:
            self._full(FullBuildReason.TOO_MANY_CHANGES)
            return None
        records = await self._stored_imports()
        affected = await asyncio.to_thread(
            affected_importers,
            records,
            delta=delta,
            changed=changed,
            go_module=go_module,
            limit=limit,
        )
        if affected is None:
            self._full(FullBuildReason.TOO_MANY_CHANGES)
            return None
        targets = changed | affected
        closes = frozenset(targets & stored.keys())
        async with self._uow() as uow:
            closing = await uow.index.visible_counts(self._id, sorted(closes))
        kept = IndexCounts(
            state.file_count - closing.file_count,
            state.symbol_count - closing.symbol_count,
            state.edge_count - closing.edge_count,
        )
        return _Plan(False, tuple(sorted(targets & scan.entries.keys())), closes, kept)

    async def _stored_files(self) -> dict[str, str]:
        rows = self._settings.read_page_rows
        files: dict[str, str] = {}
        after: str | None = None
        while True:
            async with self._uow() as uow:
                page = await uow.index.iter_files(self._id, after, rows)
            files.update(page)
            if len(page) < rows:
                return files
            after = page[-1][0]

    async def _stored_imports(self) -> list[ImportRecord]:
        """Every visible import record, unresolved ones included: rule W reads them all
        in one call, so the 500-file check sees the whole set."""
        rows = self._settings.read_page_rows
        records: list[ImportRecord] = []
        after: ImportRecord | None = None
        while True:
            async with self._uow() as uow:
                page = await uow.index.iter_imports(self._id, after, rows)
            records.extend(page)
            if len(page) < rows:
                return records
            after = page[-1]

    async def _parse(self, path: str, content: bytes) -> AstFacts | AstFailureReason:
        """One parser call of either pass. A path counts toward `parsed_files` once,
        before its first call; the job deadline is checked before the call and after it.

        The call itself is not cancelled at the deadline: the parser is shared across
        jobs, and a request cut while its answer is pending would hand that answer to the
        next file. The parser bounds one file, so one file's wait bounds the overshoot.
        """
        _check_deadline(self._deadline)
        if path not in self._parsed:
            if len(self._parsed) + 1 > self._limits.parsed_files:
                raise IndexLimitExceeded("parsed_files")
            self._parsed.add(path)
        language = language_for_path(path)
        assert language is not None, "an admitted path has a parser language"
        request = ParseRequest(path, language, content)
        result = (await self._parser.parse_many((request,)))[path]
        _check_deadline(self._deadline)
        return result

    async def _target_facts(
        self, archive: SourceArchive, scan: ArchiveScan, targets: Sequence[str]
    ) -> dict[str, AstFacts | FileUnavailable]:
        """Step 9: the summaries of every Go file and package `__init__.py` an import may
        resolve to, read as the review reads them. The symbols they hold count toward
        `symbols` apart from the rows the main pass stores."""
        facts: dict[str, AstFacts | FileUnavailable] = {}
        held = 0
        readable = set()
        for path in targets:
            if scan.entries[path].size > self._limits.parse_bytes:
                facts[path] = FileUnavailable(AstFailureReason.SOURCE_TOO_LARGE.value)
            else:
                readable.add(path)
        if not readable:
            return facts
        batches = self._archives.contents(
            archive, readable, deadline=self._deadline, batch_files=self._settings.batch_files
        )
        async for batch in batches:
            for path, content in batch:
                if is_binary_content(content):
                    facts[path] = FileUnavailable(BINARY_SOURCE)
                    continue
                result = await self._parse(path, content)
                if isinstance(result, AstFailureReason):
                    facts[path] = FileUnavailable(result.value)
                    continue
                summary = _summary(result)
                held += len(summary.symbols)
                if held > self._limits.symbols:
                    raise IndexLimitExceeded("symbols")
                facts[path] = summary
        return facts

    async def _index(
        self,
        archive: SourceArchive,
        scan: ArchiveScan,
        exclusions: IndexExclusions,
        go_module: str | None,
        plan: _Plan,
    ) -> set[str]:
        """Steps 9-11: parse the admitted files of `plan` and write them in batches."""
        admitted, targets = await asyncio.to_thread(
            _admit, exclusions, scan.entries, plan.paths, self._deadline
        )
        symlinks = frozenset(path for path, entry in scan.entries.items() if entry.symlink)
        resolver = IndexImportResolver(
            tree=RevisionTree(
                scan.commit_sha,
                {path: entry.blob_sha or "" for path, entry in scan.entries.items()},
                complete=True,
            ),
            facts=_TargetFacts(await self._target_facts(archive, scan, targets)),
            path_exclusion=lambda path: exclusions.path_class(path, symlink=path in symlinks),
            go_module=go_module,
        )
        limits = self._limits
        symbols, edges = plan.kept.symbol_count, plan.kept.edge_count
        # The graph bound counts the files this pass parses on top of the kept ones, not
        # the target-pass parses: most targets of an incremental build are kept files.
        parsed = 0
        written: set[str] = set()
        if not admitted:
            return written
        batches = self._archives.contents(
            archive,
            frozenset(admitted),
            deadline=self._deadline,
            batch_files=self._settings.batch_files,
        )
        async for batch in batches:
            files: list[IndexedFile] = []
            for path, content in batch:
                if exclusions.admit_content(content) is not None:
                    continue
                if plan.kept.file_count + parsed + 1 > limits.parsed_files:
                    raise IndexLimitExceeded("parsed_files")
                parsed += 1
                result = await self._parse(path, content)
                if exclusions.admit_parse(result) is not None:
                    continue
                assert isinstance(result, AstFacts)
                blob_sha = scan.entries[path].blob_sha
                assert blob_sha is not None, "an admitted file is within the read limit"
                imports = await resolver.resolve_file(path, result)
                files.append(
                    file_rows(
                        path,
                        blob_sha,
                        symbol_rows(path, result),
                        ((item.record, item.edges) for item in imports),
                    )
                )
            symbols += sum(len(file.symbols) for file in files)
            edges += sum(len(file.edges) for file in files)
            if symbols > limits.symbols:
                raise IndexLimitExceeded("symbols")
            if edges > limits.edges:
                raise IndexLimitExceeded("edges")
            if files:
                await self._write(files)
                written.update(file.path for file in files)
        return written

    async def _write(self, files: Sequence[IndexedFile]) -> None:
        """Step 11: one batch in one transaction; a quota stops it before the commit."""
        _check_deadline(self._deadline)
        async with self._uow() as uow:
            for file in files:
                self._check_storage(await uow.index.replace_file(self._id, self._fence, file))
            await uow.commit()

    def _check_storage(self, storage: IndexStorage) -> None:
        # ADR §7: the quotas count the published rows and the rows of this build; the
        # Workspace's also count every other build's, as `replace_file` read them in this
        # write transaction, so two builds of one Workspace cannot exceed it together.
        admission = self._settings.admission
        if storage.stored_bytes + storage.pending_bytes > admission.repository_storage_bytes:
            raise IndexLimitExceeded("repository_storage")
        if storage.workspace_bytes > admission.workspace_storage_bytes:
            raise IndexLimitExceeded("workspace_storage")
