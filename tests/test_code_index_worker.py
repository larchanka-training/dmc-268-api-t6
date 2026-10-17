"""`CODE_INDEX_*` settings and the composition of the `index.repo` consumer (#142).

The defaults and bounds are the plan's table; the indexer owns its parser (AC3, AC8), and
each job exchanges its own installation token, so a retry after a 401 never reuses the
revoked one.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, cast
from uuid import uuid4

import httpx
import pytest
from aio_pika.abc import AbstractRobustConnection
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.bootstrap.code_index_worker as code_index_worker
from app.bootstrap.code_index_worker import CodeIndexSettings, index_versions, snapshot_sources
from app.modules.code_index.application.build_index import IndexBuildSettings
from app.modules.code_index.application.index_store import IndexAdmission
from app.modules.code_index.application.ports import IndexedRepository
from app.modules.code_index.domain.limits import IndexLimits
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
        }
    )

    assert (settings.limits.deadline_seconds, settings.build.lease_seconds) == (1500, 1560)
    assert settings.build.soft_cutoff_seconds == 1499
    assert (
        CodeIndexSettings.from_environment({"CODE_INDEX_LEASE_SECONDS": "86400"}).lease_waits == 144
    )


@pytest.mark.parametrize(
    ("lease_seconds", "waits"), [(960, 2), (1200, 2), (1201, 3), (1560, 3), (86_400, 144)]
)
def test_a_leased_redelivery_waits_in_the_10m_queue_until_the_lease_has_expired(
    lease_seconds: int, waits: int
) -> None:
    assert CodeIndexSettings(lease_seconds=lease_seconds).lease_waits == waits


# --- versions ---------------------------------------------------------------------


def test_the_versions_of_an_index() -> None:
    versions = index_versions(CodeIndexSettings().limits)

    assert versions.extractor_version == "facts/v1"
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
            # AC3/AC8: not the review's parser, and without the review's CPU budget.
            assert [parser.kwargs for parser in parsers] == [{"attempt_cpu_seconds": None}]
            assert not parsers[0].closed

    asyncio.run(scenario())

    assert parsers[0].closed
    publishing, consuming = connection.channels
    assert publishing.kwargs == {"publisher_confirms": True, "on_return_raises": True}
    assert declared == [(publishing, delays)]
    assert (consuming.kwargs, consuming.prefetch, consuming.queues) == ({}, 1, ["index.repo"])


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


def test_the_consumer_waits_for_the_lease_of_its_settings(
    composed: tuple[list[Parser], list[Any]],
) -> None:
    settings = CodeIndexSettings(deadline_seconds=1500, lease_seconds=1560)

    async def scenario() -> int:
        async with code_index_worker.index_consumer(
            Worker(code_index=settings),
            cast(AbstractRobustConnection, FakeConnection()),
            _SESSIONS,
            RetryDelays(),
        ) as consumer:
            assert consumer is not None
            return consumer.publisher.lease_waits

    assert asyncio.run(scenario()) == 3
