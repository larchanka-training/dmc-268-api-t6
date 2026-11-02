"""Security boundaries shared by Bitbucket connection, review, and worker paths."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta, tzinfo
from typing import cast
from uuid import UUID

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.bootstrap import reviews_api
from app.bootstrap.reviews_api import ReviewsApiResources
from app.modules.integrations.bitbucket.api.connections import CredentialRotateRequest
from app.modules.integrations.bitbucket.application.credential_input import valid_bitbucket_token
from app.modules.integrations.bitbucket.application.request_credentials import (
    BitbucketRequestCredential,
    BoundBitbucketTarget,
)
from app.modules.integrations.bitbucket.infrastructure import retry_after as retry_after_module
from app.modules.integrations.bitbucket.infrastructure.commit_hash import (
    pr_commit_hash,
    resolved_commit_hash,
)
from app.modules.integrations.bitbucket.infrastructure.credential_cipher import (
    BitbucketCredentialCipher,
)
from app.modules.integrations.bitbucket.infrastructure.retry_after import parse_retry_after
from app.modules.repositories.application.rules_status import RulesRefreshTarget, RulesStatusKind


def test_bitbucket_token_accepts_printable_ascii_but_rejects_del() -> None:
    assert valid_bitbucket_token("!~")
    assert CredentialRotateRequest(api_token="!~").api_token == "!~"
    assert not valid_bitbucket_token("good\x7fbad")
    with pytest.raises(ValidationError):
        CredentialRotateRequest(api_token="good\x7fbad")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("15", timedelta(seconds=15)),
        ("86401", timedelta(days=1)),
        ("0", None),
        ("-1", None),
        ("invalid", None),
        ("1\u00e9", None),
        ("1" * 65, None),
    ],
)
def test_retry_after_seconds_and_invalid_values(value: str, expected: timedelta | None) -> None:
    assert parse_retry_after(value) == expected


def test_retry_after_http_date_uses_utc_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> FixedDateTime:
            assert tz is UTC
            return cls(2026, 1, 1, tzinfo=UTC)

    monkeypatch.setattr(retry_after_module, "datetime", FixedDateTime)

    assert parse_retry_after("Thu, 01 Jan 2026 00:00:30 GMT") == timedelta(seconds=30)
    assert parse_retry_after("Wed, 31 Dec 2025 23:59:59 GMT") is None


def test_bitbucket_pr_hash_prefix_requires_a_matching_full_sha() -> None:
    full = "a" * 12 + "b" * 28
    assert pr_commit_hash(full[:12]) == full[:12]
    assert pr_commit_hash(full) == full
    assert resolved_commit_hash(full, full[:12]) == full
    with pytest.raises(ValueError):
        resolved_commit_hash("a" * 12, full[:12])
    with pytest.raises(ValueError):
        resolved_commit_hash("c" * 40, full[:12])
    with pytest.raises(ValueError):
        pr_commit_hash("A" * 12)
    with pytest.raises(ValueError):
        pr_commit_hash("a" * 13)


def test_composed_bitbucket_rules_read_uses_its_separate_http_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository_uuid = UUID("22222222-2222-4222-8222-222222222222")
    bound = BoundBitbucketTarget(
        UUID("33333333-3333-4333-8333-333333333333"),
        UUID("44444444-4444-4444-8444-444444444444"),
        UUID("11111111-1111-4111-8111-111111111111"),
        repository_uuid,
        1,
    )
    now = datetime.now(UTC)
    target = RulesRefreshTarget(
        bound.repository_id,
        17,
        "workspace/repo",
        "main",
        now,
        now + timedelta(minutes=5),
        "bitbucket",
        bound,
    )
    bitbucket_paths: list[str] = []

    class Credentials:
        def __init__(self, *_args: object) -> None:
            pass

        async def checkpoint(self, actual: BoundBitbucketTarget) -> None:
            assert actual == bound

        async def credential_for(self, actual: BoundBitbucketTarget) -> BitbucketRequestCredential:
            assert actual == bound
            return BitbucketRequestCredential(actual, "test-token")

    monkeypatch.setattr(reviews_api, "BitbucketRequestCredentials", Credentials)

    def github_respond(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"Bitbucket request reached GitHub client: {request.url}")

    def bitbucket_respond(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.bitbucket.org"
        assert request.headers["Authorization"] == "Bearer test-token"
        bitbucket_paths.append(request.url.path)
        if request.url.path.endswith(f"{{{repository_uuid}}}/"):
            return httpx.Response(
                200,
                json={"uuid": f"{{{repository_uuid}}}", "mainbranch": {"name": "main"}},
            )
        if request.url.path.endswith("/refs/branches/main"):
            return httpx.Response(200, json={"target": {"hash": "a" * 40}})
        if request.url.path.endswith("/src/" + "a" * 40 + "/"):
            return httpx.Response(200, json={"values": []})
        raise AssertionError(request.url.path)

    async def exercise() -> RulesStatusKind:
        async with (
            httpx.AsyncClient(
                transport=httpx.MockTransport(github_respond), trust_env=True
            ) as github_client,
            httpx.AsyncClient(
                transport=httpx.MockTransport(bitbucket_respond), trust_env=False
            ) as bitbucket_client,
        ):
            cipher = BitbucketCredentialCipher(active_key_id="test", keys={"test": b"a" * 32})
            # Reading the claimed target does not touch a database; the dummy
            # resources expose the actual composition without requiring PG.
            resources = ReviewsApiResources(
                cast(AsyncEngine, object()), cast(async_sessionmaker[AsyncSession], object())
            )
            refresher = resources.rules_status_refresher(
                client=github_client,
                bitbucket_client=bitbucket_client,
                bitbucket_cipher=cipher,
            )
            return (await refresher._reader.read(target)).status

    assert asyncio.run(exercise()) is RulesStatusKind.MISSING
    assert len(bitbucket_paths) == 3
