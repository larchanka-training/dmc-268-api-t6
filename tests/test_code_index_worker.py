"""`CODE_INDEX_*` settings and the composition of the `index.repo` consumer (#142).

The defaults and bounds are the plan's table; the indexer owns its parser (AC3, AC8), and
each job exchanges its own installation token, so a retry after a 401 never reuses the
revoked one. The sources of one head reconcile run share one token cache (#143). The
worker leader converges, purges unused indexes and collects garbage without the GitHub
App; with it, it also reconciles heads and dispatches wake-ups (#143).
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID, uuid4

import httpx
import pytest
from aio_pika.abc import (
    AbstractExchange,
    AbstractIncomingMessage,
    AbstractQueue,
    AbstractRobustConnection,
)
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.bootstrap.code_index_worker as code_index_worker
from app.bootstrap.code_index_worker import (
    CodeIndexSettings,
    index_leader_tasks,
    index_versions,
    snapshot_sources,
)
from app.modules.code_index.application.build_index import (
    BuildOutcome,
    BuildRepositoryIndex,
    BuildStatus,
    IndexBuildSettings,
)
from app.modules.code_index.application.index_store import IndexAdmission
from app.modules.code_index.application.leader_tasks import (
    CollectIndexGarbage,
    ConvergeIndexes,
    DispatchIndexWakeups,
    PurgeUnusedIndexes,
    ReconcileIndexHeads,
)
from app.modules.code_index.application.ports import IndexedRepository, IndexWakeups
from app.modules.code_index.application.vcs import RepositorySnapshotSource
from app.modules.code_index.domain.limits import IndexLimits
from app.modules.code_index.infrastructure.amqp import (
    AmqpIndexWakeups,
    IndexQueuePublisher,
    IndexRepoMessage,
    handle_index_delivery,
)
from app.modules.code_index.infrastructure.bitbucket_snapshot_source import (
    BitbucketRepositorySnapshotSource,
)
from app.modules.integrations.bitbucket.infrastructure.credential_cipher import (
    BitbucketCredentialCipher,
)
from app.modules.integrations.webhooks.infrastructure.github_installation_tree_provider import (
    InMemoryInstallationAccessTokenCache,
)
from app.modules.reviews.application.run_failures import RetryDelays

_MIB = 1_048_576
_PEM = (
    rsa.generate_private_key(public_exponent=65537, key_size=2048)
    .private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
    .decode()
)

# --- settings -------------------------------------------------------------------


def test_every_default_is_the_plan_table() -> None:
    settings = CodeIndexSettings.from_environment({})

    assert settings.limits == IndexLimits(
        download_bytes=104_857_600,
        unpacked_bytes=1_073_741_824,
        archive_entries=100_000,
        read_bytes=1_048_576,
        parse_bytes=262_144,
        parsed_files=20_000,
        symbols=500_000,
        edges=2_000_000,
        deadline_seconds=900,
        incremental_files=500,
    )
    assert settings.build == IndexBuildSettings(
        admission=IndexAdmission(
            max_leases=2,
            max_installation_leases=1,
            repository_storage_bytes=536_870_912,
            workspace_storage_bytes=2_147_483_648,
        ),
        lease_seconds=1_200,
        soft_cutoff_seconds=300,
        discard_batch_rows=10_000,
    )
    # The leader tasks (#143).
    assert (
        settings.ttl_days,
        settings.ttl_interval_seconds,
        settings.gc_interval_seconds,
        settings.gc_batch_rows,
        settings.reconcile_interval_seconds,
        settings.reconcile_batch,
    ) == (30, 3_600, 300, 10_000, 600, 50)


def test_every_variable_sets_its_value() -> None:
    settings = CodeIndexSettings.from_environment(
        {
            "CODE_INDEX_MAX_CONCURRENT": "4",
            "CODE_INDEX_MAX_CONCURRENT_PER_INSTALLATION": "2",
            "CODE_INDEX_DEADLINE_SECONDS": "1000",
            "CODE_INDEX_LEASE_SECONDS": "1100",
            "CODE_INDEX_SOFT_CUTOFF_SECONDS": "200",
            "CODE_INDEX_DISCARD_BATCH_ROWS": "500",
            "CODE_INDEX_MAX_DOWNLOAD_BYTES": "1000",
            "CODE_INDEX_MAX_UNPACKED_BYTES": "2000",
            "CODE_INDEX_MAX_ARCHIVE_ENTRIES": "30",
            "CODE_INDEX_MAX_FILE_BYTES": "40",
            "CODE_INDEX_MAX_PARSED_FILES": "50",
            "CODE_INDEX_MAX_SYMBOLS": "60",
            "CODE_INDEX_MAX_EDGES": "70",
            "CODE_INDEX_MAX_INCREMENTAL_FILES": "80",
            "CODE_INDEX_MAX_REPOSITORY_STORAGE_BYTES": "90",
            "CODE_INDEX_MAX_WORKSPACE_STORAGE_BYTES": "100",
            "CODE_INDEX_TTL_DAYS": "7",
            "CODE_INDEX_TTL_INTERVAL_SECONDS": "600",
            "CODE_INDEX_GC_INTERVAL_SECONDS": "120",
            "CODE_INDEX_GC_BATCH_ROWS": "500",
            "CODE_INDEX_RECONCILE_INTERVAL_SECONDS": "900",
            "CODE_INDEX_RECONCILE_BATCH": "20",
        }
    )

    assert settings.limits == IndexLimits(
        download_bytes=1000,
        unpacked_bytes=2000,
        archive_entries=30,
        read_bytes=40,
        parse_bytes=262_144,
        parsed_files=50,
        symbols=60,
        edges=70,
        deadline_seconds=1000,
        incremental_files=80,
    )
    assert settings.build == IndexBuildSettings(
        admission=IndexAdmission(4, 2, 90, 100),
        lease_seconds=1100,
        soft_cutoff_seconds=200,
        discard_batch_rows=500,
    )
    assert (
        settings.ttl_days,
        settings.ttl_interval_seconds,
        settings.gc_interval_seconds,
        settings.gc_batch_rows,
        settings.reconcile_interval_seconds,
        settings.reconcile_batch,
    ) == (7, 600, 120, 500, 900, 20)


def test_an_empty_variable_keeps_the_default() -> None:
    settings = CodeIndexSettings.from_environment({"CODE_INDEX_DEADLINE_SECONDS": ""})

    assert settings.limits.deadline_seconds == 900


@pytest.mark.parametrize(
    ("env", "variable"),
    [
        ({"CODE_INDEX_MAX_CONCURRENT": "two"}, "CODE_INDEX_MAX_CONCURRENT"),
        ({"CODE_INDEX_MAX_EDGES": "1.5"}, "CODE_INDEX_MAX_EDGES"),
        ({"CODE_INDEX_MAX_SYMBOLS": "-1"}, "CODE_INDEX_MAX_SYMBOLS"),
        ({"CODE_INDEX_MAX_FILE_BYTES": "0"}, "CODE_INDEX_MAX_FILE_BYTES"),
        ({"CODE_INDEX_MAX_PARSED_FILES": "9" * 5000}, "CODE_INDEX_MAX_PARSED_FILES"),
        # Below RabbitMQ's consumer_timeout.
        (
            {"CODE_INDEX_DEADLINE_SECONDS": "1501", "CODE_INDEX_LEASE_SECONDS": "1600"},
            "CODE_INDEX_DEADLINE_SECONDS",
        ),
        # The lease outlives the deadline by at least a minute.
        ({"CODE_INDEX_LEASE_SECONDS": "959"}, "CODE_INDEX_LEASE_SECONDS"),
        ({"CODE_INDEX_SOFT_CUTOFF_SECONDS": "900"}, "CODE_INDEX_SOFT_CUTOFF_SECONDS"),
        # At most a day: the LEASED waits follow the lease, and a lease far in the future
        # would overflow the lease timestamp.
        ({"CODE_INDEX_LEASE_SECONDS": "86401"}, "CODE_INDEX_LEASE_SECONDS"),
        ({"CODE_INDEX_LEASE_SECONDS": "1000000000000"}, "CODE_INDEX_LEASE_SECONDS"),
        # ADR §4: GC starts at least every half hour, so a purged index's rows go within
        # the hour.
        ({"CODE_INDEX_GC_INTERVAL_SECONDS": "1801"}, "CODE_INDEX_GC_INTERVAL_SECONDS"),
        ({"CODE_INDEX_GC_BATCH_ROWS": "100001"}, "CODE_INDEX_GC_BATCH_ROWS"),
        ({"CODE_INDEX_TTL_INTERVAL_SECONDS": "86401"}, "CODE_INDEX_TTL_INTERVAL_SECONDS"),
        ({"CODE_INDEX_TTL_DAYS": "0"}, "CODE_INDEX_TTL_DAYS"),
        # At most ten years: the purge threshold stays far inside the timestamp range.
        ({"CODE_INDEX_TTL_DAYS": "3651"}, "CODE_INDEX_TTL_DAYS"),
        (
            {"CODE_INDEX_RECONCILE_INTERVAL_SECONDS": "86401"},
            "CODE_INDEX_RECONCILE_INTERVAL_SECONDS",
        ),
        ({"CODE_INDEX_RECONCILE_BATCH": "501"}, "CODE_INDEX_RECONCILE_BATCH"),
    ],
    ids=[
        "not-a-number",
        "fraction",
        "negative",
        "zero",
        "too-many-digits",
        "deadline-over-1500",
        "lease-under-deadline-plus-60",
        "soft-cutoff-not-under-deadline",
        "lease-over-a-day",
        "lease-far-in-the-future",
        "gc-interval-over-half-an-hour",
        "gc-batch-over-100000-rows",
        "ttl-interval-over-a-day",
        "ttl-zero",
        "ttl-over-ten-years",
        "reconcile-interval-over-a-day",
        "reconcile-batch-over-500",
    ],
)
def test_an_invalid_value_fails_the_start(env: dict[str, str], variable: str) -> None:
    with pytest.raises(ValueError, match=variable):
        CodeIndexSettings.from_environment(env)


def test_the_bounds_admit_their_edges() -> None:
    settings = CodeIndexSettings.from_environment(
        {
            "CODE_INDEX_DEADLINE_SECONDS": "1500",
            "CODE_INDEX_LEASE_SECONDS": "1560",
            "CODE_INDEX_SOFT_CUTOFF_SECONDS": "1499",
            "CODE_INDEX_RECONCILE_INTERVAL_SECONDS": "86400",
            "CODE_INDEX_RECONCILE_BATCH": "500",
        }
    )

    assert (settings.limits.deadline_seconds, settings.build.lease_seconds) == (1500, 1560)
    assert (settings.reconcile_interval_seconds, settings.reconcile_batch) == (86_400, 500)
    assert settings.build.soft_cutoff_seconds == 1499
    assert CodeIndexSettings.from_environment({"CODE_INDEX_LEASE_SECONDS": "86400"}).build == (
        IndexBuildSettings(admission=CodeIndexSettings().build.admission, lease_seconds=86_400)
    )
    edges = CodeIndexSettings.from_environment(
        {
            "CODE_INDEX_GC_INTERVAL_SECONDS": "1800",
            "CODE_INDEX_GC_BATCH_ROWS": "100000",
            "CODE_INDEX_TTL_INTERVAL_SECONDS": "86400",
        }
    )
    assert (edges.gc_interval_seconds, edges.gc_batch_rows, edges.ttl_interval_seconds) == (
        1_800,
        100_000,
        86_400,
    )
    assert CodeIndexSettings.from_environment({"CODE_INDEX_TTL_DAYS": "3650"}).ttl_days == 3_650


# --- leader tasks -----------------------------------------------------------------


def test_the_leader_purges_unused_indexes_hourly_and_collects_garbage_every_five_minutes() -> None:
    # Without the consumer (no GitHub App): converge, GC and TTL touch only the database.
    tasks = index_leader_tasks(CodeIndexSettings(), _SESSIONS, None).tasks
    configured = index_leader_tasks(
        CodeIndexSettings.from_environment(
            {
                "CODE_INDEX_TTL_DAYS": "7",
                "CODE_INDEX_TTL_INTERVAL_SECONDS": "600",
                "CODE_INDEX_GC_INTERVAL_SECONDS": "120",
                "CODE_INDEX_GC_BATCH_ROWS": "500",
            }
        ),
        _SESSIONS,
        None,
    ).tasks

    assert [(task.name, task.interval_seconds) for task in tasks] == [
        ("converge", 60),
        ("gc", 300),
        ("ttl", 3_600),
    ]
    assert [(task.name, task.interval_seconds) for task in configured] == [
        ("converge", 60),
        ("gc", 120),
        ("ttl", 600),
    ]
    for scheduled, unused_for, batch_rows in (
        (tasks, timedelta(days=30), 10_000),
        (configured, timedelta(days=7), 500),
    ):
        converge, gc, ttl = (task.task for task in scheduled)
        assert isinstance(converge, ConvergeIndexes)
        assert isinstance(ttl, PurgeUnusedIndexes) and ttl.unused_for == unused_for
        assert isinstance(gc, CollectIndexGarbage) and gc.batch_rows == batch_rows


def _leader_consumer() -> code_index_worker.IndexConsumer:
    """A consumer whose wake-ups and reconcile sources the leader tasks take."""
    return code_index_worker.IndexConsumer(
        cast(AbstractQueue, object()),
        cast(BuildRepositoryIndex, object()),
        cast(IndexQueuePublisher, object()),
        cast(IndexWakeups, object()),
        lambda: cast(Callable[[IndexedRepository], RepositorySnapshotSource], object()),
    )


def test_with_the_consumer_the_leader_reconciles_heads_and_dispatches_wake_ups() -> None:
    consumer = _leader_consumer()
    tasks = index_leader_tasks(CodeIndexSettings(), _SESSIONS, consumer).tasks
    configured = index_leader_tasks(
        CodeIndexSettings.from_environment(
            {"CODE_INDEX_RECONCILE_INTERVAL_SECONDS": "120", "CODE_INDEX_RECONCILE_BATCH": "7"}
        ),
        _SESSIONS,
        consumer,
    ).tasks

    # Converge runs before the dispatch, so the markers it sets go out in the same tick;
    # GC runs before the TTL purge, whose 30-day threshold tolerates delay (ADR 0001 §4);
    # the reconcile runs last, and the markers it sets go out on the next tick.
    assert [(task.name, task.interval_seconds) for task in tasks] == [
        ("converge", 60),
        ("dispatch", 0),
        ("gc", 300),
        ("ttl", 3_600),
        ("reconcile", 600),
    ]
    assert [(task.name, task.interval_seconds) for task in configured][-1] == ("reconcile", 120)
    # Only the reconcile, whose cursor is already past the slow candidate, continues on the
    # next tick after a timeout; the others wait their interval.
    assert [(task.name, task.continue_after_timeout) for task in tasks] == [
        ("converge", False),
        ("dispatch", False),
        ("gc", False),
        ("ttl", False),
        ("reconcile", True),
    ]
    for scheduled, batch in ((tasks, 50), (configured, 7)):
        converge, dispatch, _, _, reconcile = (task.task for task in scheduled)
        assert isinstance(converge, ConvergeIndexes)
        assert isinstance(reconcile, ReconcileIndexHeads) and reconcile.batch == batch
        assert isinstance(dispatch, DispatchIndexWakeups)


# --- versions ---------------------------------------------------------------------


def test_the_versions_of_an_index() -> None:
    versions = index_versions(CodeIndexSettings().limits)

    assert versions.extractor_version == "facts/v2"
    assert versions.resolver_version == "resolver/v1+index/v1"
    assert len(versions.parser_profile_version) == 64
    assert versions.limits_profile_version.startswith("limits/v1:")


def test_the_limits_digest_changes_with_the_file_limit_only() -> None:
    def digest(**env: str) -> str:
        limits = CodeIndexSettings.from_environment(env).limits
        return index_versions(limits).limits_profile_version

    default = digest()

    assert digest(CODE_INDEX_MAX_FILE_BYTES=str(2 * _MIB)) != default
    assert digest(CODE_INDEX_MAX_SYMBOLS="10", CODE_INDEX_MAX_EDGES="10") == default
    assert digest(CODE_INDEX_MAX_PARSED_FILES="10", CODE_INDEX_MAX_DOWNLOAD_BYTES="10") == default


# --- snapshot sources -----------------------------------------------------------


@dataclass
class GitHub:
    exchanges: int = 0
    authorizations: list[str] = field(default_factory=list)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/app/installations/17/access_tokens":
            self.exchanges += 1
            return httpx.Response(
                201, json={"token": f"t{self.exchanges}", "expires_at": "2099-01-01T00:00:00Z"}
            )
        self.authorizations.append(request.headers["Authorization"])
        if request.url.path == "/repos/octo/repo":
            return httpx.Response(200, json={"default_branch": "main"})
        if request.url.path == "/repos/octo/repo/commits/main":
            return httpx.Response(200, text="e" * 40)
        raise AssertionError(f"unexpected GitHub call {request.method} {request.url}")


_REPOSITORY = IndexedRepository(uuid4(), "github", 17, "octo/repo")


def test_each_job_exchanges_its_own_installation_token() -> None:
    github = GitHub()

    async def two_jobs() -> None:
        transport = httpx.MockTransport(github)
        async with httpx.AsyncClient(base_url="https://api.github.test", transport=transport) as c:
            sources = snapshot_sources(c, app_id="1", private_key=_PEM)
            first = sources(_REPOSITORY)
            await first.head()
            await first.head()
            # A retry is a new job: it never reuses the token of the attempt before it.
            await sources(_REPOSITORY).head()

    asyncio.run(two_jobs())

    assert github.exchanges == 2
    assert github.authorizations == ["Bearer t1"] * 4 + ["Bearer t2"] * 2


def test_the_sources_of_one_token_cache_share_its_installation_token() -> None:
    github = GitHub()

    async def one_reconcile_run() -> None:
        transport = httpx.MockTransport(github)
        async with httpx.AsyncClient(base_url="https://api.github.test", transport=transport) as c:
            # The head reconcile of one leader run: one cache for all its repositories.
            sources = snapshot_sources(
                c,
                app_id="1",
                private_key=_PEM,
                cache=InMemoryInstallationAccessTokenCache(now=time.time),
            )
            await sources(_REPOSITORY).head()
            await sources(dataclasses.replace(_REPOSITORY, repository_id=uuid4())).head()

    asyncio.run(one_reconcile_run())

    assert github.exchanges == 1
    assert github.authorizations == ["Bearer t1"] * 4


def test_a_repository_of_another_provider_has_no_snapshot_source() -> None:
    async def build() -> None:
        async with httpx.AsyncClient() as client:
            sources = snapshot_sources(client, app_id="1", private_key=_PEM)
            sources(IndexedRepository(uuid4(), "gitlab", 17, "octo/repo"))

    with pytest.raises(ValueError, match="gitlab"):
        asyncio.run(build())


# --- the consumer -----------------------------------------------------------------


@dataclass
class FakeChannel:
    kwargs: dict[str, Any]
    prefetch: int | None = None
    queues: list[str] = field(default_factory=list)

    async def set_qos(self, *, prefetch_count: int) -> None:
        self.prefetch = prefetch_count

    async def get_exchange(self, name: str, *, ensure: bool) -> str:
        return name

    async def get_queue(self, name: str, *, ensure: bool) -> str:
        self.queues.append(name)
        return name


@dataclass
class FakeConnection:
    channels: list[FakeChannel] = field(default_factory=list)

    async def channel(self, **kwargs: Any) -> FakeChannel:
        self.channels.append(FakeChannel(kwargs))
        return self.channels[-1]


@dataclass(frozen=True)
class Worker:
    code_index: CodeIndexSettings = CodeIndexSettings()
    github_app_id: str | None = "1"
    github_private_key: str | None = _PEM
    github_api_url: str = "https://api.github.test"
    bitbucket_cipher: BitbucketCredentialCipher | None = None


@dataclass
class Parser:
    kwargs: dict[str, Any]
    closed: bool = False

    async def aclose(self) -> None:
        self.closed = True


@pytest.fixture
def composed(monkeypatch: pytest.MonkeyPatch) -> tuple[list[Parser], list[Any]]:
    parsers: list[Parser] = []
    declared: list[Any] = []

    def parser(**kwargs: Any) -> Parser:
        parsers.append(Parser(kwargs))
        return parsers[-1]

    async def declare_topology(channel: Any, delays: RetryDelays) -> None:
        declared.append((channel, delays))

    monkeypatch.setattr(code_index_worker, "SubprocessSourceParser", parser)
    monkeypatch.setattr(code_index_worker, "declare_topology", declare_topology)
    return parsers, declared


_SESSIONS = cast(async_sessionmaker[AsyncSession], object())


def test_the_consumer_owns_its_parser_and_its_channels(
    composed: tuple[list[Parser], list[Any]],
) -> None:
    parsers, declared = composed
    connection = FakeConnection()
    delays = RetryDelays()

    async def scenario() -> None:
        async with code_index_worker.index_consumer(
            Worker(), cast(AbstractRobustConnection, connection), _SESSIONS, delays
        ) as consumer:
            assert consumer is not None
            assert consumer.publisher.lease_waits == 2
            assert isinstance(consumer.wakeups, AmqpIndexWakeups)
            # AC3/AC8: not the review's parser, and without the review's CPU budget.
            assert [parser.kwargs for parser in parsers] == [{"attempt_cpu_seconds": None}]
            assert not parsers[0].closed

    asyncio.run(scenario())

    assert parsers[0].closed
    publishing, consuming = connection.channels
    assert publishing.kwargs == {"publisher_confirms": True, "on_return_raises": True}
    assert declared == [(publishing, delays)]
    assert (consuming.kwargs, consuming.prefetch, consuming.queues) == ({}, 1, ["index.repo"])


def test_each_reconcile_run_of_the_consumer_shares_one_token_cache(
    composed: tuple[list[Parser], list[Any]],
) -> None:
    github = GitHub()

    async def two_runs() -> None:
        async with code_index_worker.index_consumer(
            Worker(),
            cast(AbstractRobustConnection, FakeConnection()),
            _SESSIONS,
            RetryDelays(),
            httpx.MockTransport(github),
        ) as consumer:
            assert consumer is not None
            for _ in range(2):
                sources = consumer.sources()
                await sources(_REPOSITORY).head()
                await sources(dataclasses.replace(_REPOSITORY, repository_id=uuid4())).head()

    asyncio.run(two_runs())

    # One exchange per run, not per repository; the next run starts with a fresh cache.
    assert github.exchanges == 2
    assert github.authorizations == ["Bearer t1"] * 4 + ["Bearer t2"] * 4


def test_without_the_github_app_the_topology_is_declared_and_nothing_consumes(
    composed: tuple[list[Parser], list[Any]], caplog: pytest.LogCaptureFixture
) -> None:
    parsers, declared = composed
    connection = FakeConnection()

    async def scenario() -> None:
        async with code_index_worker.index_consumer(
            Worker(github_private_key=None),
            cast(AbstractRobustConnection, connection),
            _SESSIONS,
            RetryDelays(),
        ) as consumer:
            assert consumer is None

    with caplog.at_level(logging.WARNING):
        asyncio.run(scenario())

    assert len(declared) == 1 and len(connection.channels) == 1
    assert parsers == []
    assert "the index.repo consumer is disabled" in caplog.text


def test_bitbucket_only_worker_composes_uuid_bound_snapshot_source(
    composed: tuple[list[Parser], list[Any]],
) -> None:
    """A Bitbucket deployment can index even when no GitHub App is configured."""

    async def scenario() -> None:
        async with code_index_worker.index_consumer(
            Worker(
                github_app_id=None,
                github_private_key=None,
                bitbucket_cipher=BitbucketCredentialCipher(
                    active_key_id="test", keys={"test": b"a" * 32}
                ),
            ),
            cast(AbstractRobustConnection, FakeConnection()),
            _SESSIONS,
            RetryDelays(),
        ) as consumer:
            assert consumer is not None
            source = consumer.sources()(IndexedRepository(uuid4(), "bitbucket", 7, "old/slug"))
            assert isinstance(source, BitbucketRepositorySnapshotSource)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("settings", "delays", "waits"),
    [
        (CodeIndexSettings(deadline_seconds=1500, lease_seconds=1560), RetryDelays(), 3),
        # The waits are turns of the consumer's own 10m queue, whatever its TTL.
        (CodeIndexSettings(), RetryDelays(long=timedelta(minutes=5)), 4),
    ],
    ids=["lease-1560", "long-delay-5-min"],
)
def test_the_consumer_waits_for_the_lease_of_its_settings_in_its_own_10m_queue(
    composed: tuple[list[Parser], list[Any]],
    settings: CodeIndexSettings,
    delays: RetryDelays,
    waits: int,
) -> None:
    async def scenario() -> int:
        async with code_index_worker.index_consumer(
            Worker(code_index=settings),
            cast(AbstractRobustConnection, FakeConnection()),
            _SESSIONS,
            delays,
        ) as consumer:
            assert consumer is not None
            return consumer.publisher.lease_waits

    assert asyncio.run(scenario()) == waits


@pytest.mark.parametrize(
    ("settings", "ttl"),
    [
        (CodeIndexSettings(lease_seconds=1560), 1560),
        # Never shorter than the 300 s of the review queues.
        (CodeIndexSettings(deadline_seconds=10, lease_seconds=70, soft_cutoff_seconds=5), 300),
    ],
    ids=["lease-1560", "lease-70"],
)
def test_the_local_count_of_the_consumer_lives_for_its_lease(
    composed: tuple[list[Parser], list[Any]], settings: CodeIndexSettings, ttl: float
) -> None:
    async def scenario() -> float:
        async with code_index_worker.index_consumer(
            Worker(code_index=settings),
            cast(AbstractRobustConnection, FakeConnection()),
            _SESSIONS,
            RetryDelays(),
        ) as consumer:
            assert consumer is not None
            return consumer.publisher.local_ttl_seconds

    assert asyncio.run(scenario()) == ttl


def test_the_github_client_of_the_consumer_is_closed_when_it_exits(
    composed: tuple[list[Parser], list[Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    clients: list[httpx.AsyncClient] = []

    def sources(
        client: httpx.AsyncClient, **kwargs: Any
    ) -> Callable[[IndexedRepository], RepositorySnapshotSource]:
        clients.append(client)
        return snapshot_sources(client, **kwargs)

    monkeypatch.setattr(code_index_worker, "snapshot_sources", sources)

    async def scenario() -> None:
        async with code_index_worker.index_consumer(
            Worker(), cast(AbstractRobustConnection, FakeConnection()), _SESSIONS, RetryDelays()
        ) as consumer:
            assert consumer is not None
            [client] = clients
            assert not client.is_closed

    asyncio.run(scenario())

    assert clients[0].is_closed


def _codeload_line() -> logging.LogRecord:
    url = httpx.URL("https://codeload.github.com/octo/repo/legacy.tar.gz/abc?token=secret")
    return logging.LogRecord(
        "httpx",
        logging.INFO,
        __file__,
        0,
        'HTTP Request: %s %s "%s %d %s"',
        ("GET", url, "HTTP/1.1", 200, "OK"),
        None,
    )


def test_a_snapshot_source_redacts_the_codeload_log_line_before_any_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    logger = logging.getLogger("httpx")
    monkeypatch.setattr(logger, "filters", [])
    before = _codeload_line()
    assert logger.filter(before) and "token=secret" in before.getMessage()
    requests: list[httpx.Request] = []

    def github(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(500)

    async def build() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(github)) as client:
            snapshot_sources(client, app_id="1", private_key=_PEM)(_REPOSITORY)

    asyncio.run(build())

    assert requests == []
    after = _codeload_line()
    assert logger.filter(after)
    assert after.getMessage() == (
        "HTTP Request: GET https://codeload.github.com/octo/repo/legacy.tar.gz/abc?redacted "
        '"HTTP/1.1 200 OK"'
    )


# --- the consumer loop --------------------------------------------------------------


@dataclass
class Wakeup:
    """A delivery as aio-pika settles it: `processed` only after a settle that went out."""

    repository_id: UUID = field(default_factory=uuid4)
    channel_closed: bool = False
    acked: bool = False
    nacks: list[bool] = field(default_factory=list)
    processed: bool = False
    headers: dict[str, Any] = field(default_factory=dict)
    redelivered: bool = False
    routing_key: str = "index.repo"

    def __post_init__(self) -> None:
        message = IndexRepoMessage.wake(self.repository_id, 1, datetime.now(UTC))
        self.body = json.dumps(message.wire()).encode()
        self.message_id = str(message.message_id)

    async def ack(self) -> None:
        if self.channel_closed:
            raise ConnectionError("the channel was replaced by a reconnect")
        self.acked = self.processed = True

    async def nack(self, requeue: bool = True) -> None:
        self.nacks.append(requeue)
        if self.channel_closed:
            raise ConnectionError("the channel was replaced by a reconnect")
        self.processed = True


@dataclass
class Deliveries:
    messages: list[Wakeup]
    closed: bool = False

    def iterator(self) -> Deliveries:
        return self

    async def __aenter__(self) -> Deliveries:
        return self

    async def __aexit__(self, *exc: object) -> None:
        self.closed = True

    async def __aiter__(self) -> AsyncIterator[Wakeup]:
        for message in self.messages:
            yield message


@dataclass
class Jobs:
    seen: list[UUID] = field(default_factory=list)
    started: asyncio.Event | None = None

    async def execute(self, repository_id: UUID, generation: int) -> BuildOutcome:
        self.seen.append(repository_id)
        if self.started is not None and len(self.seen) == 1:
            self.started.set()
            await asyncio.Event().wait()
        return BuildOutcome(BuildStatus.PUBLISHED)


def _consumer(deliveries: Deliveries, jobs: Jobs) -> code_index_worker.IndexConsumer:
    exchange = cast(AbstractExchange, object())
    return code_index_worker.IndexConsumer(
        cast(AbstractQueue, deliveries),
        cast(BuildRepositoryIndex, jobs),
        IndexQueuePublisher(
            exchange, exchange, RetryDelays(), lease_waits=2, local_ttl_seconds=1200
        ),
        cast(IndexWakeups, object()),
        lambda: cast(Callable[[IndexedRepository], RepositorySnapshotSource], object()),
    )


def _fail_on(failing: Wakeup, monkeypatch: pytest.MonkeyPatch, *, after_settle: bool) -> None:
    """A bug of the delivery handler on `failing`: before any settle, or after its ack."""

    async def handle(
        message: AbstractIncomingMessage,
        handler: Callable[[UUID, int], Awaitable[BuildOutcome]],
        publisher: IndexQueuePublisher,
    ) -> None:
        if message is not cast(AbstractIncomingMessage, failing):
            await handle_index_delivery(message, handler, publisher)
            return
        if after_settle:
            await handle_index_delivery(message, handler, publisher)
        raise RuntimeError("a bug of the delivery handler")

    monkeypatch.setattr(code_index_worker, "handle_index_delivery", handle)


def _guard_lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == code_index_worker.__name__]


def test_a_failed_settle_is_logged_and_the_consumer_takes_the_next_delivery(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # A reconnect during a long job replaced the channel: the ack raises, the guard's nack
    # raises too, and the review consumers in the same TaskGroup must not be cancelled.
    first, second = Wakeup(channel_closed=True), Wakeup()
    deliveries, jobs = Deliveries([first, second]), Jobs()

    with caplog.at_level(logging.ERROR):
        asyncio.run(_consumer(deliveries, jobs).consume())

    assert jobs.seen == [first.repository_id, second.repository_id]
    assert second.acked and second.nacks == []
    assert not first.acked and first.nacks == [False] and not first.processed
    assert deliveries.closed
    lines = _guard_lines(caplog)
    assert len(lines) == 2 and all(first.message_id in line for line in lines)
    # SD §13: the message id only, no customer data.
    assert str(first.repository_id) not in caplog.text


def test_an_error_before_any_settle_dead_letters_the_delivery_and_the_next_is_handled(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # Not a channel failure: left unsettled on an open channel, the delivery would stall
    # the consumer (`prefetch_count=1`), and a requeue could spin.
    first, second = Wakeup(), Wakeup()
    _fail_on(first, monkeypatch, after_settle=False)
    deliveries, jobs = Deliveries([first, second]), Jobs()

    with caplog.at_level(logging.ERROR):
        asyncio.run(_consumer(deliveries, jobs).consume())

    assert first.nacks == [False] and not first.acked
    assert second.acked and second.nacks == []
    assert jobs.seen == [second.repository_id]
    [line] = _guard_lines(caplog)
    assert first.message_id in line


def test_an_error_after_the_ack_settles_nothing_more(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    first, second = Wakeup(), Wakeup()
    _fail_on(first, monkeypatch, after_settle=True)
    deliveries, jobs = Deliveries([first, second]), Jobs()

    with caplog.at_level(logging.ERROR):
        asyncio.run(_consumer(deliveries, jobs).consume())

    assert first.acked and first.nacks == []
    assert second.acked and second.nacks == []
    assert jobs.seen == [first.repository_id, second.repository_id]
    [line] = _guard_lines(caplog)
    assert first.message_id in line


def test_cancelling_the_consumer_stops_it_during_a_job() -> None:
    first, second = Wakeup(), Wakeup()
    deliveries = Deliveries([first, second])

    async def scenario() -> Jobs:
        jobs = Jobs(started=asyncio.Event())
        assert jobs.started is not None
        task = asyncio.create_task(_consumer(deliveries, jobs).consume())
        await jobs.started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return jobs

    jobs = asyncio.run(scenario())

    assert jobs.seen == [first.repository_id]
    assert not first.acked and not second.acked
    assert deliveries.closed
