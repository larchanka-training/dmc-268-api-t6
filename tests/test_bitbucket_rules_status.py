"""Bitbucket rules status reads the pinned default-branch document."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from uuid import UUID

import httpx
import pytest

from app.modules.code_index.application.vcs import BranchHead, VcsAccessDenied, VcsUnavailable
from app.modules.code_index.infrastructure.bitbucket_snapshot_source import (
    BitbucketRepositorySnapshotSource,
)
from app.modules.integrations.bitbucket.application.request_credentials import (
    BitbucketRequestCredential,
    BoundBitbucketTarget,
)
from app.modules.repositories.application.rules_status import (
    ProviderRulesDocumentReader,
    ReadBitbucketRulesDocument,
    RulesReading,
    RulesRefreshTarget,
    RulesStatusKind,
)
from app.modules.reviews.application.policy_documents import missing_document, normalize_document
from app.modules.reviews.application.policy_errors import PolicyError, PolicyErrorCode
from app.modules.reviews.application.policy_snapshot import PolicyDocument

_SHA = "a" * 40
_NOW = datetime(2026, 10, 28, tzinfo=UTC)
_TARGET = RulesRefreshTarget(
    UUID("11111111-1111-4111-8111-111111111111"),
    17,
    "workspace/repo",
    "main",
    _NOW,
    _NOW + timedelta(minutes=5),
    "bitbucket",
)


@dataclass
class FakeSource:
    head_result: BranchHead | None = BranchHead("main", _SHA)
    document: PolicyDocument | None = None
    head_error: Exception | None = None
    document_error: Exception | None = None
    checkpoint_error: Exception | None = None
    read_sha: str | None = None

    async def head(self) -> BranchHead | None:
        if self.head_error is not None:
            raise self.head_error
        return self.head_result

    async def rules_document(self, commit_sha: str) -> PolicyDocument:
        self.read_sha = commit_sha
        if self.document_error is not None:
            raise self.document_error
        assert self.document is not None
        return self.document

    async def checkpoint(self) -> None:
        if self.checkpoint_error is not None:
            raise self.checkpoint_error


def _read(source: FakeSource) -> RulesReading:
    reader = ReadBitbucketRulesDocument(lambda _: source)
    return asyncio.run(reader.read(_TARGET))


def test_present_rules_are_parsed_at_the_pinned_default_branch_head() -> None:
    raw = b"# Rules\n```review-ignore\ndocs/**\n```\n"
    source = FakeSource(
        head_result=BranchHead("trunk", _SHA),
        document=normalize_document(".review/rules.md", raw, blob_oid="b" * 40),
    )

    reading = _read(source)

    assert reading.status is RulesStatusKind.PRESENT
    assert (reading.branch, reading.sha, source.read_sha) == ("trunk", _SHA, _SHA)
    assert (reading.text, reading.ignore_masks) == (
        "# Rules\n```review-ignore\ndocs/**\n```\n",
        ("docs/**",),
    )
    assert reading.checksum == "dad032d2882db0959c725c723813404e6de27f4784752cb2ba381ec9ece1e845"


def test_complete_missing_rules_listing_is_terminal_missing() -> None:
    source = FakeSource(document=missing_document(".review/rules.md"))

    reading = _read(source)

    assert reading.status is RulesStatusKind.MISSING
    assert (reading.branch, reading.sha, source.read_sha) == ("main", _SHA, _SHA)


def test_invalid_rules_document_is_reported_as_invalid() -> None:
    source = FakeSource(
        document_error=PolicyError(
            PolicyErrorCode.DOCUMENT_DECODE_ERROR,
            "The policy document must be valid UTF-8",
            source_path=".review/rules.md",
        )
    )

    reading = _read(source)

    assert reading.status is RulesStatusKind.INVALID
    assert reading.diagnostics[0].code == "policy_document_decode_error"


@pytest.mark.parametrize(
    "source",
    [
        FakeSource(head_result=None),
        FakeSource(head_error=VcsUnavailable("provider failed")),
        FakeSource(document_error=PolicyError(PolicyErrorCode.FETCH_TRANSIENT, "incomplete")),
        FakeSource(
            document=missing_document(".review/rules.md"),
            checkpoint_error=VcsAccessDenied("binding changed"),
        ),
    ],
    ids=["no-default-branch", "head-failed", "listing-incomplete", "binding-changed"],
)
def test_unproven_absence_and_changed_binding_are_unavailable(source: FakeSource) -> None:
    reading = _read(source)

    assert reading.status is RulesStatusKind.UNAVAILABLE
    assert reading.diagnostics


@pytest.mark.parametrize(
    ("source", "expected_code", "expected_message"),
    [
        pytest.param(
            FakeSource(head_result=None),
            "policy_revision_unavailable",
            "Ревизия default branch недоступна: источник не подтвердил, есть ли файл правил.",
            id="no-head",
        ),
        pytest.param(
            FakeSource(head_error=VcsUnavailable("rate limited", rate_limited=True)),
            "policy_rate_limited",
            "Источник ограничил частоту запросов при чтении правил.",
            id="rate-limited",
        ),
        pytest.param(
            FakeSource(document_error=PolicyError(PolicyErrorCode.FETCH_TRANSIENT, "incomplete")),
            "policy_fetch_transient",
            "Не удалось прочитать документ правил из репозитория.",
            id="transient",
        ),
    ],
)
def test_bitbucket_unavailable_diagnostic_has_provider_neutral_text(
    source: FakeSource, expected_code: str, expected_message: str
) -> None:
    reading = _read(source)

    assert reading.status is RulesStatusKind.UNAVAILABLE
    assert [(item.code, item.message) for item in reading.diagnostics] == [
        (expected_code, expected_message)
    ]


@dataclass
class FakeReader:
    reading: RulesReading
    targets: list[RulesRefreshTarget] = field(default_factory=list)

    async def read(self, target: RulesRefreshTarget) -> RulesReading:
        self.targets.append(target)
        return self.reading


def test_provider_dispatch_never_uses_github_reader_for_bitbucket() -> None:
    github = FakeReader(RulesReading(RulesStatusKind.MISSING, "github", _SHA))
    bitbucket = FakeReader(RulesReading(RulesStatusKind.MISSING, "bitbucket", _SHA))
    dispatch = ProviderRulesDocumentReader(github=github, bitbucket=bitbucket)

    bitbucket_result = asyncio.run(dispatch.read(_TARGET))
    github_target = replace(_TARGET, provider="github")
    github_result = asyncio.run(dispatch.read(github_target))

    assert (bitbucket_result.branch, github_result.branch) == ("bitbucket", "github")
    assert bitbucket.targets == [_TARGET]
    assert github.targets == [github_target]


def test_bitbucket_http_read_reaches_present_from_pinned_listing() -> None:
    raw = b"# Rules\n"
    repository_uuid = UUID("22222222-2222-4222-8222-222222222222")
    bound = BoundBitbucketTarget(
        UUID("33333333-3333-4333-8333-333333333333"),
        UUID("44444444-4444-4444-8444-444444444444"),
        _TARGET.repository_id,
        repository_uuid,
        1,
    )
    seen: list[str] = []

    class Credentials:
        async def checkpoint(self, target: BoundBitbucketTarget) -> None:
            assert target == bound

        async def credential_for(self, target: BoundBitbucketTarget) -> BitbucketRequestCredential:
            assert target == bound
            return BitbucketRequestCredential(target, "test-token")

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.bitbucket.org"
        assert request.headers["Authorization"] == "Bearer test-token"
        path = request.url.path
        seen.append(path)
        if path.endswith(f"{{{repository_uuid}}}/"):
            return httpx.Response(
                200,
                json={"uuid": f"{{{repository_uuid}}}", "mainbranch": {"name": "trunk"}},
            )
        if path.endswith("/refs/branches/trunk"):
            return httpx.Response(200, json={"target": {"hash": _SHA}})
        if path.endswith(f"/src/{_SHA}/"):
            return httpx.Response(
                200,
                json={
                    "values": [
                        {"path": ".review", "type": "commit_directory", "commit": {"hash": _SHA}}
                    ]
                },
            )
        if path.endswith(f"/src/{_SHA}/.review/"):
            return httpx.Response(
                200,
                json={
                    "values": [
                        {
                            "path": ".review/rules.md",
                            "type": "commit_file",
                            "size": len(raw),
                            "commit": {"hash": _SHA},
                        }
                    ]
                },
            )
        if path.endswith(f"/src/{_SHA}/.review/rules.md"):
            return httpx.Response(200, content=raw)
        raise AssertionError(path)

    async def exercise() -> RulesReading:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:

            async def target() -> BoundBitbucketTarget:
                return bound

            source = BitbucketRepositorySnapshotSource(
                client=client, target_for_repository=target, credentials=Credentials()
            )
            return await ReadBitbucketRulesDocument(lambda _: source).read(_TARGET)

    reading = asyncio.run(exercise())

    assert (reading.status, reading.branch, reading.sha, reading.text) == (
        RulesStatusKind.PRESENT,
        "trunk",
        _SHA,
        "# Rules\n",
    )
    assert len(seen) == 5
    assert all(f"{{{repository_uuid}}}" in path for path in seen)
