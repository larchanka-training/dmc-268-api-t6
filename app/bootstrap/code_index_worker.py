"""Composition of the `index.repo` consumer in the worker process (#142).

- `CodeIndexSettings` reads the `CODE_INDEX_*` variables; the defaults live here, and an
  invalid value fails the start.
- `index_consumer` declares the `index.repo` topology on a confirm channel of its own,
  not on the review channel, and composes `BuildRepositoryIndex`.
- The indexer has its own httpx client and its own `SubprocessSourceParser`: the
  review's parser is sequential, and sharing it would queue reviews behind index jobs.
- Every job builds its own installation token provider and cache, so an attempt never
  reuses the token of the attempt before it: a retry after a 401 exchanges a fresh one.
  A shared token cache (the Redis one of #148) would need an invalidation on a 401
  first.
"""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, fields
from functools import partial
from typing import Protocol

import httpx
from aio_pika.abc import AbstractQueue, AbstractRobustConnection
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.code_index.application.build_index import (
    BuildRepositoryIndex,
    IndexBuildSettings,
    IndexVersions,
    limits_profile_version,
    parser_profile_version,
)
from app.modules.code_index.application.index_resolution import INDEX_RESOLVER_VERSION
from app.modules.code_index.application.index_store import IndexAdmission
from app.modules.code_index.application.ports import IndexedRepository
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
from app.modules.code_index.infrastructure.github_snapshot_source import (
    GitHubRepositorySnapshotSource,
)
from app.modules.code_index.infrastructure.index_store import SqlAlchemyCodeIndexUnitOfWork
from app.modules.code_index.infrastructure.repository_reader import (
    SqlAlchemyIndexRepositoryReader,
)
from app.modules.code_index.infrastructure.tar_archive import TarArchiveReader
from app.modules.integrations.webhooks.infrastructure.github_installation_tree_provider import (
    GitHubAppInstallationAccessTokenProvider,
    InMemoryInstallationAccessTokenCache,
)
from app.modules.reviews.application.ast_symbols import AstLanguage
from app.modules.reviews.application.run_failures import RetryDelays
from app.modules.reviews.infrastructure.amqp import consume
from app.modules.reviews.infrastructure.subprocess_source_parser import SubprocessSourceParser
from app.modules.reviews.infrastructure.tree_sitter_facts import QUERY_VERSION, profile_version

_LOGGER = logging.getLogger(__name__)

# Below RabbitMQ's `consumer_timeout` (30 min): the delivery is acked after the job.
MAX_DEADLINE_SECONDS = 1_500
# The lease outlives the job deadline by at least this much.
LEASE_MARGIN_SECONDS = 60
# At most a day: a LEASED redelivery waits in the 10m queue until the lease has expired.
MAX_LEASE_SECONDS = 86_400
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
) -> Callable[[IndexedRepository], RepositorySnapshotSource]:
    """The VCS port of one job; GitHub only (GitLab is #97)."""

    def source(repository: IndexedRepository) -> RepositorySnapshotSource:
        if repository.provider != "github":
            raise ValueError(f"the indexer has no snapshot source for {repository.provider}")
        # The raw provider: its failures map to the port's errors in the source. A cache
        # per job, so that each attempt exchanges a fresh token.
        tokens = GitHubAppInstallationAccessTokenProvider(
            client=client,
            app_id=app_id,
            private_key=private_key,
            cache=InMemoryInstallationAccessTokenCache(now=now),
            now=now,
        )
        return GitHubRepositorySnapshotSource(
            client=client,
            token_provider=tokens,
            installation_external_id=repository.installation_external_id,
            repository_full_name=repository.full_name,
        )

    return source


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
    queue: AbstractQueue
    build: BuildRepositoryIndex
    publisher: IndexQueuePublisher

    async def consume(self) -> None:
        """One job at a time: the queue's channel has `prefetch_count=1`."""
        await consume(
            self.queue,
            partial(handle_index_delivery, handler=self.build.execute, publisher=self.publisher),
        )


@asynccontextmanager
async def index_consumer(
    settings: IndexWorkerSettings,
    connection: AbstractRobustConnection,
    session_factory: async_sessionmaker[AsyncSession],
    delays: RetryDelays,
    transport: httpx.AsyncBaseTransport | None = None,
) -> AsyncIterator[IndexConsumer | None]:
    """Declare the topology and compose the consumer; `None` without the GitHub App.

    `transport` replaces the network in tests of this composition.
    """
    channel = await connection.channel(publisher_confirms=True, on_return_raises=True)
    await declare_topology(channel, delays)
    if not (settings.github_app_id and settings.github_private_key):
        _LOGGER.warning(
            "GITHUB_APP_ID or GITHUB_APP_PRIVATE_KEY is not set: the index.repo consumer is "
            "disabled"
        )
        yield None
        return
    code_index = settings.code_index
    async with httpx.AsyncClient(
        base_url=settings.github_api_url, timeout=10.0, transport=transport
    ) as client:
        parser = SubprocessSourceParser(attempt_cpu_seconds=None)
        try:
            build = BuildRepositoryIndex(
                uow=partial(SqlAlchemyCodeIndexUnitOfWork, session_factory),
                repositories=SqlAlchemyIndexRepositoryReader(session_factory),
                sources=snapshot_sources(
                    client, app_id=settings.github_app_id, private_key=settings.github_private_key
                ),
                archives=TarArchiveReader(code_index.limits),
                parser=parser,
                wakeups=await AmqpIndexWakeups.open(channel),
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
                    channel, delays, lease_waits=lease_waits(code_index.lease_seconds, delays.long)
                ),
            )
        finally:
            await parser.aclose()
