"""Composition of the `index.repo` consumer in the worker process (#142).

- `CodeIndexSettings` reads the `CODE_INDEX_*` variables; the defaults live here, and an
  invalid value fails the start.
- `index_consumer` declares the `index.repo` topology on a confirm channel of its own,
  not on the review channel, and composes `BuildRepositoryIndex`.
- `index_leader_tasks` composes the index tasks the worker leader runs (#143): converge,
  garbage collection and the TTL purge always; the head reconcile and the wake-up
  dispatch only with the consumer, whose GitHub client and confirm channel they use.
- The indexer has its own httpx client and its own `SubprocessSourceParser`: the
  review's parser is sequential, and sharing it would queue reviews behind index jobs.
- Every job builds its own installation token provider and cache, so an attempt never
  reuses the token of the attempt before it: a retry after a 401 exchanges a fresh one.
  A shared token cache (the Redis one of #148) would need an invalidation on a 401
  first. The head reconcile passes one cache per leader run, so its reads of many
  repositories of one installation exchange one token (#143).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, fields
from datetime import timedelta
from functools import partial
from typing import Protocol

import httpx
from aio_pika.abc import AbstractIncomingMessage, AbstractQueue, AbstractRobustConnection
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.common.infrastructure.amqp_consumer import consume_queue
from app.modules.code_index.application.build_index import (
    BuildRepositoryIndex,
    IndexBuildSettings,
    IndexVersions,
    limits_profile_version,
    parser_profile_version,
)
from app.modules.code_index.application.index_resolution import INDEX_RESOLVER_VERSION
from app.modules.code_index.application.index_store import IndexAdmission
from app.modules.code_index.application.leader_tasks import (
    CodeIndexLeaderTasks,
    CollectIndexGarbage,
    ConvergeIndexes,
    DispatchIndexWakeups,
    PurgeUnusedIndexes,
    ReconcileIndexHeads,
    ScheduledTask,
)
from app.modules.code_index.application.ports import IndexedRepository, IndexWakeups
from app.modules.code_index.application.vcs import RepositorySnapshotSource
from app.modules.code_index.domain.limits import IndexLimits
from app.modules.code_index.infrastructure.amqp import (
    QUEUE,
    AmqpIndexWakeups,
    IndexQueuePublisher,
    declare_topology,
    handle_index_delivery,
    lease_waits,
)
from app.modules.code_index.infrastructure.bitbucket_snapshot_source import (
    BitbucketRepositorySnapshotSource,
)
from app.modules.code_index.infrastructure.github_snapshot_source import (
    GitHubRepositorySnapshotSource,
)
from app.modules.code_index.infrastructure.index_store import SqlAlchemyCodeIndexUnitOfWork
from app.modules.code_index.infrastructure.maintenance import (
    SqlAlchemyCodeIndexMaintenanceUnitOfWork,
)
from app.modules.code_index.infrastructure.repository_reader import (
    SqlAlchemyIndexRepositoryReader,
)
from app.modules.code_index.infrastructure.tar_archive import TarArchiveReader
from app.modules.integrations.bitbucket.infrastructure.credential_cipher import (
    BitbucketCredentialCipher,
)
from app.modules.integrations.bitbucket.infrastructure.request_credentials import (
    BitbucketRequestCredentials,
    SqlAlchemyBitbucketTargetReader,
)
from app.modules.integrations.webhooks.infrastructure.github_installation_tree_provider import (
    GitHubAppInstallationAccessTokenProvider,
    InMemoryInstallationAccessTokenCache,
    InstallationAccessTokenCache,
)
from app.modules.reviews.application.ast_symbols import AstLanguage
from app.modules.reviews.application.run_failures import RetryDelays
from app.modules.reviews.infrastructure import amqp as review_amqp
from app.modules.reviews.infrastructure.subprocess_source_parser import SubprocessSourceParser
from app.modules.reviews.infrastructure.tree_sitter_facts import QUERY_VERSION, profile_version

_LOGGER = logging.getLogger(__name__)

# Below RabbitMQ's `consumer_timeout` (30 min): the delivery is acked after the job.
MAX_DEADLINE_SECONDS = 1_500
# The lease outlives the job deadline by at least this much.
LEASE_MARGIN_SECONDS = 60
# At most a day: a LEASED redelivery waits in the 10m queue until the lease has expired.
MAX_LEASE_SECONDS = 86_400
# ADR §4 targets physical deletion of a purged index's rows within the hour: GC starts at
# least every half hour and keeps running on consecutive leader ticks until drained.
MAX_GC_INTERVAL_SECONDS = 1_800
# One GC portion is one DELETE transaction per graph table; this keeps it short.
MAX_GC_BATCH_ROWS = 100_000
# The leader looks for indexes past the TTL at least once a day.
MAX_TTL_INTERVAL_SECONDS = 86_400
# At most ten years: `clock_timestamp() - interval` of the TTL purge stays far inside the
# timestamp range.
MAX_TTL_DAYS = 3_650
# The leader reconciles every index with its head at least once a day.
MAX_RECONCILE_INTERVAL_SECONDS = 86_400
# One reconcile run reads at most this many heads, two GitHub requests each.
MAX_RECONCILE_BATCH = 500
# Converge runs at most this often, so a re-arm waits up to a minute (SD §11).
CONVERGE_INTERVAL_SECONDS = 60
# Far above any limit; it keeps `int()` off a pathological value.
_MAX_DIGITS = 18


@dataclass(frozen=True)
class CodeIndexSettings:
    """The plan's `CODE_INDEX_*` variables with their defaults: each field reads
    `CODE_INDEX_<FIELD>` (`max_symbols` reads `CODE_INDEX_MAX_SYMBOLS`)."""

    max_concurrent: int = 2
    max_concurrent_per_installation: int = 1
    deadline_seconds: int = 900
    lease_seconds: int = 1_200
    soft_cutoff_seconds: int = 300
    discard_batch_rows: int = 10_000
    max_download_bytes: int = 104_857_600
    max_unpacked_bytes: int = 1_073_741_824
    max_archive_entries: int = 100_000
    max_file_bytes: int = 1_048_576
    max_parsed_files: int = 20_000
    max_symbols: int = 500_000
    max_edges: int = 2_000_000
    max_incremental_files: int = 500
    max_repository_storage_bytes: int = 536_870_912
    max_workspace_storage_bytes: int = 2_147_483_648
    ttl_days: int = 30
    ttl_interval_seconds: int = 3_600
    gc_interval_seconds: int = 300
    gc_batch_rows: int = 10_000
    reconcile_interval_seconds: int = 600
    reconcile_batch: int = 50

    def __post_init__(self) -> None:
        for name in _field_names():
            if getattr(self, name) < 1:
                raise ValueError(f"{_variable(name)} must be a positive integer")
        if self.deadline_seconds > MAX_DEADLINE_SECONDS:
            raise ValueError(
                f"{_variable('deadline_seconds')} must be at most {MAX_DEADLINE_SECONDS}"
            )
        if self.lease_seconds < self.deadline_seconds + LEASE_MARGIN_SECONDS:
            raise ValueError(
                f"{_variable('lease_seconds')} must be at least "
                f"{_variable('deadline_seconds')} + {LEASE_MARGIN_SECONDS}"
            )
        if self.lease_seconds > MAX_LEASE_SECONDS:
            raise ValueError(f"{_variable('lease_seconds')} must be at most {MAX_LEASE_SECONDS}")
        if self.soft_cutoff_seconds >= self.deadline_seconds:
            raise ValueError(
                f"{_variable('soft_cutoff_seconds')} must be below {_variable('deadline_seconds')}"
            )
        if self.gc_interval_seconds > MAX_GC_INTERVAL_SECONDS:
            raise ValueError(
                f"{_variable('gc_interval_seconds')} must be at most {MAX_GC_INTERVAL_SECONDS}"
            )
        if self.gc_batch_rows > MAX_GC_BATCH_ROWS:
            raise ValueError(f"{_variable('gc_batch_rows')} must be at most {MAX_GC_BATCH_ROWS}")
        if self.ttl_interval_seconds > MAX_TTL_INTERVAL_SECONDS:
            raise ValueError(
                f"{_variable('ttl_interval_seconds')} must be at most {MAX_TTL_INTERVAL_SECONDS}"
            )
        if self.ttl_days > MAX_TTL_DAYS:
            raise ValueError(f"{_variable('ttl_days')} must be at most {MAX_TTL_DAYS}")
        if self.reconcile_interval_seconds > MAX_RECONCILE_INTERVAL_SECONDS:
            raise ValueError(
                f"{_variable('reconcile_interval_seconds')} must be at most "
                f"{MAX_RECONCILE_INTERVAL_SECONDS}"
            )
        if self.reconcile_batch > MAX_RECONCILE_BATCH:
            raise ValueError(
                f"{_variable('reconcile_batch')} must be at most {MAX_RECONCILE_BATCH}"
            )

    @classmethod
    def from_environment(cls, env: Mapping[str, str]) -> CodeIndexSettings:
        """An unset or empty variable keeps its default."""
        values: dict[str, int] = {}
        for name in _field_names():
            raw = env.get(_variable(name))
            if raw:
                values[name] = _positive_integer(_variable(name), raw)
        return cls(**values)

    @property
    def limits(self) -> IndexLimits:
        return IndexLimits(
            download_bytes=self.max_download_bytes,
            unpacked_bytes=self.max_unpacked_bytes,
            archive_entries=self.max_archive_entries,
            read_bytes=self.max_file_bytes,
            parsed_files=self.max_parsed_files,
            symbols=self.max_symbols,
            edges=self.max_edges,
            deadline_seconds=self.deadline_seconds,
            incremental_files=self.max_incremental_files,
        )

    @property
    def build(self) -> IndexBuildSettings:
        return IndexBuildSettings(
            admission=IndexAdmission(
                max_leases=self.max_concurrent,
                max_installation_leases=self.max_concurrent_per_installation,
                repository_storage_bytes=self.max_repository_storage_bytes,
                workspace_storage_bytes=self.max_workspace_storage_bytes,
            ),
            lease_seconds=self.lease_seconds,
            soft_cutoff_seconds=self.soft_cutoff_seconds,
            discard_batch_rows=self.discard_batch_rows,
        )


def _field_names() -> tuple[str, ...]:
    return tuple(item.name for item in fields(CodeIndexSettings))


def _variable(name: str) -> str:
    return f"CODE_INDEX_{name.upper()}"


def _positive_integer(variable: str, raw: str) -> int:
    text = raw.strip()
    if not (text.isascii() and text.isdecimal()) or len(text) > _MAX_DIGITS:
        raise ValueError(f"{variable} must be a positive integer")
    return int(text)


def index_versions(limits: IndexLimits) -> IndexVersions:
    """The versions a published profile records (plan, "Versions")."""
    return IndexVersions(
        extractor_version=QUERY_VERSION,
        resolver_version=INDEX_RESOLVER_VERSION,
        parser_profile_version=parser_profile_version(
            profile_version(language) for language in AstLanguage
        ),
        limits_profile_version=limits_profile_version(limits),
    )


def snapshot_sources(
    client: httpx.AsyncClient,
    *,
    app_id: str,
    private_key: str,
    now: Callable[[], float] = time.time,
    cache: InstallationAccessTokenCache | None = None,
    bitbucket_source_factory: Callable[[IndexedRepository], RepositorySnapshotSource] | None = None,
) -> Callable[[IndexedRepository], RepositorySnapshotSource]:
    """The VCS port of one job; GitHub and UUID-bound Bitbucket Cloud.

    With `cache`, every source shares it: one head reconcile run. Without it, each
    source gets a cache of its own.
    """

    def source(repository: IndexedRepository) -> RepositorySnapshotSource:
        if repository.provider == "bitbucket" and bitbucket_source_factory is not None:
            return bitbucket_source_factory(repository)
        if repository.provider != "github":
            raise ValueError(f"the indexer has no snapshot source for {repository.provider}")
        # The raw provider: its failures map to the port's errors in the source. A cache
        # per job, so that each attempt exchanges a fresh token.
        tokens = GitHubAppInstallationAccessTokenProvider(
            client=client,
            app_id=app_id,
            private_key=private_key,
            cache=InMemoryInstallationAccessTokenCache(now=now) if cache is None else cache,
            now=now,
        )
        return GitHubRepositorySnapshotSource(
            client=client,
            token_provider=tokens,
            installation_external_id=repository.installation_external_id,
            repository_full_name=repository.full_name,
        )

    return source


def index_leader_tasks(
    settings: CodeIndexSettings,
    session_factory: async_sessionmaker[AsyncSession],
    consumer: IndexConsumer | None,
) -> CodeIndexLeaderTasks:
    """The index tasks of the worker leader (#143, ADR 0001 §4-§6).

    Converge, garbage collection and the TTL purge touch only the database and always
    run. The head reconcile and the wake-up dispatch need the consumer's GitHub client
    and confirm channel, so they are off without the GitHub App. The order is converge,
    dispatch, GC, TTL, reconcile: the markers converge sets go out in the same tick; GC
    runs before the TTL purge because physical deletion has the one-hour target of
    ADR 0001 §4, while the 30-day TTL tolerates delay, so a TTL backlog never takes GC's
    share of the tick; and the reconcile, whose head reads are the slowest, takes only
    what the others left of the tick's budget; the markers it sets go out on the next
    tick. After a timeout only the reconcile runs again on the next tick: its cursor is
    already past the slow candidate, while GC or TTL stuck on a lock would time out on
    every tick and starve the tasks after them, so they wait their interval.
    """
    uow = partial(SqlAlchemyCodeIndexMaintenanceUnitOfWork, session_factory)
    tasks = [ScheduledTask("converge", ConvergeIndexes(uow=uow), CONVERGE_INTERVAL_SECONDS)]
    if consumer is not None:
        # An interval of 0 runs the task on every leader tick.
        tasks.append(
            ScheduledTask("dispatch", DispatchIndexWakeups(uow=uow, wakeups=consumer.wakeups), 0)
        )
    tasks += [
        ScheduledTask(
            "gc",
            CollectIndexGarbage(uow=uow, batch_rows=settings.gc_batch_rows),
            settings.gc_interval_seconds,
        ),
        ScheduledTask(
            "ttl",
            PurgeUnusedIndexes(uow=uow, unused_for=timedelta(days=settings.ttl_days)),
            settings.ttl_interval_seconds,
        ),
    ]
    if consumer is not None:
        tasks.append(
            ScheduledTask(
                "reconcile",
                ReconcileIndexHeads(
                    uow=uow,
                    sources=consumer.sources,
                    versions=index_versions(settings.limits),
                    batch=settings.reconcile_batch,
                ),
                settings.reconcile_interval_seconds,
                continue_after_timeout=True,
            )
        )
    return CodeIndexLeaderTasks(tasks)


class IndexWorkerSettings(Protocol):
    """What the composition reads of the worker's settings."""

    @property
    def code_index(self) -> CodeIndexSettings: ...

    @property
    def github_app_id(self) -> str | None: ...

    @property
    def github_private_key(self) -> str | None: ...

    @property
    def github_api_url(self) -> str: ...


@dataclass(frozen=True)
class IndexConsumer:
    """The `index.repo` consumer and what the leader's index tasks take from it.

    `wakeups` publishes on the consumer's confirm channel; `sources()` returns the
    snapshot sources of one head reconcile run, which share one token cache.
    """

    queue: AbstractQueue
    build: BuildRepositoryIndex
    publisher: IndexQueuePublisher
    wakeups: IndexWakeups
    sources: Callable[[], Callable[[IndexedRepository], RepositorySnapshotSource]]

    async def consume(self, *, stop: asyncio.Event | None = None) -> None:
        """One job at a time: the queue's channel has `prefetch_count=1`.

        An exception out of one delivery (a settle on a channel replaced by a reconnect
        during a long job, or a bug of the handler) is logged and the loop goes on, so the
        review consumers of the same `TaskGroup` are not cancelled for it. A delivery it
        left unsettled goes to `code_index.dlq` (`_dead_letter`).
        """
        handle = partial(
            handle_index_delivery, handler=self.build.execute, publisher=self.publisher
        )

        async def guarded(message: AbstractIncomingMessage) -> None:
            # CONTRACT: `Exception` only, so a cancellation still stops the loop.
            try:
                await handle(message)
            except Exception:
                # SD §13: the message id only, no customer data.
                _LOGGER.exception("Failed to handle index.repo message %s", message.message_id)
                if not message.processed:
                    await _dead_letter(message)

        await consume_queue(self.queue, guarded, stop=stop)


async def _dead_letter(message: AbstractIncomingMessage) -> None:
    """Best effort: on an open channel an unsettled delivery would stall the consumer
    (`prefetch_count=1`) and a requeue could spin, so it goes to `code_index.dlq` for
    inspection. On a closed channel this nack fails too, and the broker redelivers the
    delivery; the job is idempotent."""
    try:
        await message.nack(requeue=False)
    except Exception:
        _LOGGER.exception(
            "Failed to nack index.repo message %s; it is redelivered once its channel closes",
            message.message_id,
        )


@asynccontextmanager
async def index_consumer(
    settings: IndexWorkerSettings,
    connection: AbstractRobustConnection,
    session_factory: async_sessionmaker[AsyncSession],
    delays: RetryDelays,
    transport: httpx.AsyncBaseTransport | None = None,
) -> AsyncIterator[IndexConsumer | None]:
    """Declare the topology and compose the consumer for configured providers.

    `transport` replaces the network in tests of this composition.
    """
    channel = await connection.channel(publisher_confirms=True, on_return_raises=True)
    await declare_topology(channel, delays)
    bitbucket_cipher: BitbucketCredentialCipher | None = getattr(settings, "bitbucket_cipher", None)
    if not (settings.github_app_id and settings.github_private_key) and bitbucket_cipher is None:
        _LOGGER.warning(
            "GITHUB_APP_ID or GITHUB_APP_PRIVATE_KEY is not set: the index.repo consumer is "
            "disabled"
        )
        yield None
        return
    code_index = settings.code_index
    app_id, private_key = settings.github_app_id or "", settings.github_private_key or ""
    async with httpx.AsyncClient(
        base_url=settings.github_api_url, timeout=10.0, transport=transport
    ) as client:
        bitbucket_factory: Callable[[IndexedRepository], RepositorySnapshotSource] | None = None
        if bitbucket_cipher is not None:
            reader = SqlAlchemyBitbucketTargetReader(session_factory)
            credentials = BitbucketRequestCredentials(reader, bitbucket_cipher)

            def bitbucket_factory(repository: IndexedRepository) -> RepositorySnapshotSource:
                return BitbucketRepositorySnapshotSource(
                    client=client,
                    target_for_repository=partial(
                        reader.resolve_repository_target, repository.repository_id
                    ),
                    credentials=credentials,
                )

        def reconcile_sources() -> Callable[[IndexedRepository], RepositorySnapshotSource]:
            # One token cache per head reconcile run.
            return snapshot_sources(
                client,
                app_id=app_id,
                private_key=private_key,
                cache=InMemoryInstallationAccessTokenCache(now=time.time),
                bitbucket_source_factory=bitbucket_factory,
            )

        parser = SubprocessSourceParser(attempt_cpu_seconds=None)
        try:
            wakeups = await AmqpIndexWakeups.open(channel)
            build = BuildRepositoryIndex(
                uow=partial(SqlAlchemyCodeIndexUnitOfWork, session_factory),
                repositories=SqlAlchemyIndexRepositoryReader(session_factory),
                sources=snapshot_sources(
                    client,
                    app_id=app_id,
                    private_key=private_key,
                    bitbucket_source_factory=bitbucket_factory,
                ),
                archives=TarArchiveReader(code_index.limits),
                parser=parser,
                wakeups=wakeups,
                versions=index_versions(code_index.limits),
                limits=code_index.limits,
                settings=code_index.build,
            )
            consuming = await connection.channel()
            await consuming.set_qos(prefetch_count=1)
            yield IndexConsumer(
                await consuming.get_queue(QUEUE, ensure=False),
                build,
                await IndexQueuePublisher.open(
                    channel,
                    delays,
                    lease_waits=lease_waits(code_index.lease_seconds, delays.long),
                    # A job ends before its lease; never below the review queues' TTL.
                    local_ttl_seconds=max(
                        code_index.lease_seconds, review_amqp.LOCAL_RETRY_TTL_SECONDS
                    ),
                ),
                wakeups,
                reconcile_sources,
            )
        finally:
            await parser.aclose()
