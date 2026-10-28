"""Rules status of a repository (#150): the read of the default branch and when it is redone.

The read runs through the real GitHub adapters against a mocked GitHub API, so the
default branch, the commit SHA, the trees and the blob are requested exactly as in
``webhook-worker``.
"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any, Self
from uuid import UUID

import httpx
import pytest

from app.modules.integrations.webhooks.infrastructure.github_installation_tree_provider import (
    StaticGitHubInstallationAccessTokenProvider,
)
from app.modules.repositories.application.rules_status import (
    GetRulesStatus,
    ReadRulesDocument,
    RefreshRulesStatuses,
    RulesDiagnostic,
    RulesReading,
    RulesRefreshTarget,
    RulesStatus,
    RulesStatusKind,
    StoredRulesStatus,
    needs_refresh,
)
from app.modules.reviews.application.policy_documents import MAX_DOCUMENT_BYTES
from app.modules.reviews.infrastructure.github_policy_source import (
    GitHubDefaultBranchHeadSource,
    GitHubRepositoryPolicySource,
)
from app.modules.reviews.infrastructure.github_vcs import HttpGitHubVcsProvider

_REPOSITORY = UUID("00000000-0000-4000-8000-0000000000a2")
_HEAD = "a" * 40
_ROOT = "1" * 40
_REVIEW = "2" * 40
_RULES = "3" * 40
_NOW = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)
_LEASE_END = _NOW + timedelta(minutes=5)
_TARGET = RulesRefreshTarget(_REPOSITORY, 17, "octo/repo", "main", _NOW, _LEASE_END, "github")


def _entry(path: str, sha: str, *, kind: str = "blob", size: int = 5) -> dict[str, Any]:
    mode = {"blob": "100644", "tree": "040000", "link": "120000"}[kind]
    item = {"path": path, "mode": mode, "type": "tree" if kind == "tree" else "blob", "sha": sha}
    return item if kind == "tree" else item | {"size": size}


@dataclass
class FakeGitHub:
    """The GitHub REST answers of one repository: ``rules`` None means no file at all."""

    branch: str = "main"
    rules: bytes | None = None
    review_dir: bool = True
    truncated: bool = False
    # Path suffix -> status code answered instead of the normal response.
    failures: dict[str, int] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)
    paths: list[str] = field(default_factory=list)

    def respond(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.paths.append(path)
        assert request.headers["Authorization"] == "Bearer token"
        for suffix, status in self.failures.items():
            if path.endswith(suffix):
                return httpx.Response(status, headers=self.headers, json={"message": "x"})
        if path == "/repos/octo/repo":
            return httpx.Response(200, json={"id": 101, "default_branch": self.branch})
        if path == f"/repos/octo/repo/commits/{self.branch}":
            assert request.headers["Accept"] == "application/vnd.github.sha"
            return httpx.Response(200, text=_HEAD)
        if path == f"/repos/octo/repo/git/trees/{_HEAD}":
            tree = [_entry("README.md", "4" * 40)]
            if self.review_dir:
                tree.append(_entry(".review", _REVIEW, kind="tree"))
            return httpx.Response(200, json={"tree": tree, "truncated": self.truncated})
        if path == f"/repos/octo/repo/git/trees/{_REVIEW}":
            tree = [] if self.rules is None else [_entry("rules.md", _RULES, size=len(self.rules))]
            return httpx.Response(200, json={"tree": tree})
        if path == f"/repos/octo/repo/git/blobs/{_RULES}":
            assert self.rules is not None
            content = base64.b64encode(self.rules).decode()
            return httpx.Response(
                200,
                json={
                    "sha": _RULES,
                    "encoding": "base64",
                    "content": content,
                    "size": len(self.rules),
                },
            )
        raise AssertionError(f"unexpected GitHub request: {request.url}")


def _read(github: FakeGitHub) -> RulesReading:
    async def exercise() -> RulesReading:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(github.respond), base_url="https://api.github.test"
        ) as client:
            tokens = StaticGitHubInstallationAccessTokenProvider("token")
            vcs = HttpGitHubVcsProvider(client=client, token_provider=tokens)

            def source(target: RulesRefreshTarget, sha: str) -> GitHubRepositoryPolicySource:
                return GitHubRepositoryPolicySource(
                    client=client,
                    token_provider=tokens,
                    installation_external_id=target.installation_external_id,
                    repository_full_name=target.repository_full_name,
                    policy_base_sha=sha,
                    read_blob=lambda oid: vcs.get_repository_blob(
                        target.installation_external_id, target.repository_full_name, oid
                    ),
                )

            reader = ReadRulesDocument(
                heads=GitHubDefaultBranchHeadSource(client=client, token_provider=tokens),
                sources=source,
            )
            return await reader.read(_TARGET)

    return asyncio.run(exercise())


def test_present_file_gives_text_masks_and_checksum_of_the_head_commit() -> None:
    raw = b"# Rules\r\n\n```review-ignore\ndocs/**\n*.lock\n```\n"
    github = FakeGitHub(rules=raw)

    reading = _read(github)

    assert reading.status is RulesStatusKind.PRESENT
    assert (reading.branch, reading.sha) == ("main", _HEAD)
    assert reading.text == "# Rules\n\n```review-ignore\ndocs/**\n*.lock\n```\n"
    assert reading.ignore_masks == ("*.lock", "docs/**")
    assert reading.checksum == sha256(raw).hexdigest()
    assert reading.diagnostics == ()
    # Branch, then the full commit SHA, then the tree and the blob by SHA.
    assert github.paths == [
        "/repos/octo/repo",
        "/repos/octo/repo/commits/main",
        f"/repos/octo/repo/git/trees/{_HEAD}",
        f"/repos/octo/repo/git/trees/{_REVIEW}",
        f"/repos/octo/repo/git/blobs/{_RULES}",
    ]


def test_empty_file_is_present_not_missing() -> None:
    reading = _read(FakeGitHub(rules=b""))

    assert reading.status is RulesStatusKind.PRESENT
    assert (reading.text, reading.ignore_masks) == ("", ())
    assert reading.checksum == sha256(b"").hexdigest()


@pytest.mark.parametrize("review_dir", [True, False], ids=["no-file", "no-directory"])
def test_reliably_absent_file_is_missing(review_dir: bool) -> None:
    reading = _read(FakeGitHub(rules=None, review_dir=review_dir))

    assert reading == RulesReading(RulesStatusKind.MISSING, "main", _HEAD)


def test_renamed_default_branch_is_read_from_github() -> None:
    reading = _read(FakeGitHub(branch="trunk", rules=b"rules"))

    assert (reading.status, reading.branch) == (RulesStatusKind.PRESENT, "trunk")


@pytest.mark.parametrize(
    ("raw", "code", "text", "line"),
    [
        (b"```review-ignore\nsrc/a?.py\n```\n", "policy_ignore_pattern_invalid", True, 2),
        (b"```review-ignore\ndocs/**\n", "policy_ignore_block_unclosed", True, 1),
        (b"\xff\xfe", "policy_document_decode_error", False, None),
        (b"ok\x01", "policy_document_control_character", False, 1),
    ],
    ids=["bad-mask", "unclosed-block", "not-utf8", "control-character"],
)
def test_format_error_is_invalid_with_its_diagnostic(
    raw: bytes, code: str, text: bool, line: int | None
) -> None:
    reading = _read(FakeGitHub(rules=raw))

    assert (reading.status, reading.sha) == (RulesStatusKind.INVALID, _HEAD)
    assert reading.text == (raw.decode() if text else None)
    (diagnostic,) = reading.diagnostics
    assert (diagnostic.code, diagnostic.line, diagnostic.retryable) == (code, line, False)
    assert diagnostic.message


def test_too_large_file_is_invalid_without_reading_its_blob() -> None:
    github = FakeGitHub(rules=b"x" * (MAX_DOCUMENT_BYTES + 1))

    reading = _read(github)

    assert (reading.status, reading.text) == (RulesStatusKind.INVALID, None)
    (diagnostic,) = reading.diagnostics
    assert (diagnostic.code, diagnostic.limit, diagnostic.actual) == (
        "policy_document_too_large",
        MAX_DOCUMENT_BYTES,
        MAX_DOCUMENT_BYTES + 1,
    )
    assert "64 КиБ" in diagnostic.message
    assert "Где: файл `.review/rules.md`" in diagnostic.message
    assert not any("/git/blobs/" in path for path in github.paths)


def test_symlink_is_invalid() -> None:
    class Linked(FakeGitHub):
        def respond(self, request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith(f"/git/trees/{_REVIEW}"):
                return httpx.Response(200, json={"tree": [_entry("rules.md", _RULES, kind="link")]})
            return super().respond(request)

    reading = _read(Linked(rules=b"x"))

    assert reading.status is RulesStatusKind.INVALID
    assert reading.diagnostics[0].code == "policy_document_type_unsupported"


@pytest.mark.parametrize(
    ("failures", "headers", "code", "sha", "retryable"),
    [
        ({f"/git/trees/{_REVIEW}": 404}, {}, "policy_revision_unavailable", _HEAD, False),
        ({f"/git/trees/{_HEAD}": 404}, {}, "policy_revision_unavailable", _HEAD, False),
        ({f"/git/blobs/{_RULES}": 404}, {}, "policy_revision_unavailable", _HEAD, False),
        ({"/repos/octo/repo": 404}, {}, "policy_revision_unavailable", None, False),
        ({"/commits/main": 422}, {}, "policy_revision_unavailable", None, False),
        # An empty repository: GitHub answers 409 "Git Repository is empty".
        ({"/commits/main": 409}, {}, "policy_revision_unavailable", None, False),
        ({"/repos/octo/repo": 403}, {}, "policy_access_denied", None, False),
        ({f"/git/trees/{_HEAD}": 401}, {}, "policy_access_denied", _HEAD, False),
        (
            {f"/git/trees/{_HEAD}": 403},
            {"X-RateLimit-Remaining": "0"},
            "policy_rate_limited",
            _HEAD,
            True,
        ),
        ({"/commits/main": 502}, {}, "policy_fetch_transient", None, True),
        ({f"/git/blobs/{_RULES}": 500}, {}, "policy_fetch_transient", _HEAD, True),
    ],
    ids=[
        "ambiguous-404-review-tree",
        "ambiguous-404-root-tree",
        "404-blob",
        "404-repository",
        "unknown-ref",
        "empty-repository-409",
        "access-denied",
        "unauthorized",
        "rate-limited",
        "transient-commit",
        "transient-blob",
    ],
)
def test_failed_read_is_unavailable_never_missing(
    failures: dict[str, int], headers: dict[str, str], code: str, sha: str | None, retryable: bool
) -> None:
    reading = _read(FakeGitHub(rules=b"rules", failures=failures, headers=headers))

    assert (reading.status, reading.branch, reading.sha) == (
        RulesStatusKind.UNAVAILABLE,
        "main",
        sha,
    )
    (diagnostic,) = reading.diagnostics
    assert (diagnostic.code, diagnostic.retryable) == (code, retryable)


def test_truncated_tree_is_unavailable_not_missing() -> None:
    reading = _read(FakeGitHub(rules=None, review_dir=False, truncated=True))

    assert reading.status is RulesStatusKind.UNAVAILABLE
    assert reading.diagnostics[0].code == "policy_revision_unavailable"


@pytest.mark.parametrize(
    ("sha", "tree"),
    [
        (_HEAD, [_entry(".review", _REVIEW, kind="tree"), _entry(".review", _RULES)]),
        (_REVIEW, [_entry("rules.md", _RULES), _entry("rules.md", "4" * 40)]),
    ],
    ids=["root", "review"],
)
def test_ambiguous_tree_is_unavailable_not_missing(sha: str, tree: list[dict[str, Any]]) -> None:
    class Duplicated(FakeGitHub):
        def respond(self, request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith(f"/git/trees/{sha}"):
                return httpx.Response(200, json={"tree": tree, "truncated": False})
            return super().respond(request)

    reading = _read(Duplicated(rules=b"rules"))

    assert reading.status is RulesStatusKind.UNAVAILABLE
    (diagnostic,) = reading.diagnostics
    assert (diagnostic.code, diagnostic.retryable) == ("policy_revision_unavailable", False)


@pytest.mark.parametrize("answer", ["abc", "a" * 64], ids=["short", "sha-256"])
def test_unreadable_commit_sha_is_a_transient_failure(answer: str) -> None:
    class OddSha(FakeGitHub):
        def respond(self, request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/commits/main"):
                return httpx.Response(200, text=answer)
            return super().respond(request)

    reading = _read(OddSha(rules=b"rules"))

    assert (reading.status, reading.sha) == (RulesStatusKind.UNAVAILABLE, None)
    assert reading.diagnostics[0] == RulesDiagnostic(
        "policy_fetch_transient",
        "Не удалось прочитать документы правил из GitHub.",
        None,
        None,
        True,
    )


def _status(
    kind: RulesStatusKind, checked_at: datetime, *diagnostics: RulesDiagnostic
) -> RulesStatus:
    return RulesStatus(kind, "main", _HEAD, checked_at, diagnostics=diagnostics)


_TRANSIENT = RulesDiagnostic("policy_fetch_transient", "m", None, None, True)
_DENIED = RulesDiagnostic("policy_access_denied", "m", None, None, False)
_LIMITED = RulesDiagnostic(
    "policy_rate_limited", "m", None, None, True, retry_after=timedelta(seconds=60)
)
_LONG_LIMIT = RulesDiagnostic(
    "policy_rate_limited", "m", None, None, True, retry_after=timedelta(hours=1)
)
_SHORT_LIMIT = RulesDiagnostic(
    "policy_rate_limited", "m", None, None, True, retry_after=timedelta(seconds=5)
)


@pytest.mark.parametrize(
    ("stored", "due"),
    [
        (
            StoredRulesStatus("main", None, refresh_requested=False, installation_available=True),
            True,
        ),
        (
            StoredRulesStatus("main", None, refresh_requested=True, installation_available=True),
            False,
        ),
        (
            StoredRulesStatus(
                "main",
                _status(RulesStatusKind.PRESENT, _NOW - timedelta(minutes=9)),
                False,
                installation_available=True,
            ),
            False,
        ),
        (
            StoredRulesStatus(
                "main",
                _status(RulesStatusKind.PRESENT, _NOW - timedelta(minutes=10)),
                False,
                installation_available=True,
            ),
            True,
        ),
        (
            StoredRulesStatus(
                "main",
                _status(RulesStatusKind.MISSING, _NOW - timedelta(hours=1)),
                True,
                installation_available=True,
            ),
            False,
        ),
        (
            StoredRulesStatus(
                "main",
                _status(RulesStatusKind.UNAVAILABLE, _NOW - timedelta(seconds=30), _TRANSIENT),
                False,
                installation_available=True,
            ),
            True,
        ),
        (
            StoredRulesStatus(
                "main",
                _status(RulesStatusKind.UNAVAILABLE, _NOW - timedelta(seconds=29), _TRANSIENT),
                False,
                installation_available=True,
            ),
            False,
        ),
        (
            StoredRulesStatus(
                "main",
                _status(RulesStatusKind.UNAVAILABLE, _NOW - timedelta(seconds=59), _LIMITED),
                False,
                installation_available=True,
            ),
            False,
        ),
        (
            StoredRulesStatus(
                "main",
                _status(RulesStatusKind.UNAVAILABLE, _NOW - timedelta(seconds=60), _LIMITED),
                False,
                installation_available=True,
            ),
            True,
        ),
        (
            StoredRulesStatus(
                "main",
                _status(RulesStatusKind.UNAVAILABLE, _NOW - timedelta(seconds=29), _SHORT_LIMIT),
                False,
                installation_available=True,
            ),
            False,
        ),
        (
            StoredRulesStatus(
                "main",
                _status(RulesStatusKind.UNAVAILABLE, _NOW - timedelta(minutes=10), _LONG_LIMIT),
                False,
                installation_available=True,
            ),
            True,
        ),
        (
            StoredRulesStatus(
                "main",
                _status(RulesStatusKind.UNAVAILABLE, _NOW - timedelta(seconds=5), _TRANSIENT),
                False,
                installation_available=True,
            ),
            False,
        ),
        (
            StoredRulesStatus(
                "main",
                _status(RulesStatusKind.UNAVAILABLE, _NOW - timedelta(minutes=1), _DENIED),
                False,
                installation_available=True,
            ),
            False,
        ),
    ],
    ids=[
        "never-read",
        "never-read-requested",
        "fresh",
        "stale-after-10-minutes",
        "stale-but-requested",
        "retryable-after-30-seconds",
        "retryable-at-29-seconds",
        "retry-after-60-not-yet",
        "retry-after-60-reached",
        "retry-after-below-30-keeps-30",
        "retry-after-over-10-minutes-ends-at-10-minutes",
        "retryable-within-pause",
        "permanent-error-fresh",
    ],
)
def test_a_get_requests_a_read_only_when_it_is_due(stored: StoredRulesStatus, due: bool) -> None:
    assert needs_refresh(stored, _NOW) is due


@dataclass
class FakeStatusStore:
    stored: StoredRulesStatus | None
    requests: int = 0
    commits: int = 0

    @property
    def rules_status(self) -> Self:
        return self

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        return None

    async def get(self, repository_id: UUID) -> StoredRulesStatus | None:
        return self.stored

    async def request_refresh(self, repository_id: UUID) -> None:
        self.requests += 1


def _get(store: FakeStatusStore) -> RulesStatus | None:
    return asyncio.run(GetRulesStatus(lambda: store, now=lambda: _NOW).execute(_REPOSITORY))


def test_a_repository_never_read_is_pending_and_requests_its_read() -> None:
    store = FakeStatusStore(
        StoredRulesStatus("develop", None, refresh_requested=False, installation_available=True)
    )

    status = _get(store)

    assert status == RulesStatus(RulesStatusKind.PENDING, "develop", None, None)
    assert (store.requests, store.commits) == (1, 1)


def test_a_stale_result_is_returned_and_requested_again() -> None:
    old = _status(RulesStatusKind.PRESENT, _NOW - timedelta(minutes=11))
    store = FakeStatusStore(
        StoredRulesStatus("main", old, refresh_requested=False, installation_available=True)
    )

    assert _get(store) == old
    assert store.requests == 1


def test_a_fresh_result_is_returned_without_a_request() -> None:
    fresh = _status(RulesStatusKind.MISSING, _NOW - timedelta(minutes=1))
    store = FakeStatusStore(
        StoredRulesStatus("main", fresh, refresh_requested=False, installation_available=True)
    )

    assert _get(store) == fresh
    assert (store.requests, store.commits) == (0, 0)


@pytest.mark.parametrize(
    "old",
    [None, _status(RulesStatusKind.PRESENT, _NOW - timedelta(hours=1))],
    ids=["never-read", "read-before"],
)
def test_a_repository_the_app_lost_is_access_denied_without_a_request(
    old: RulesStatus | None,
) -> None:
    store = FakeStatusStore(
        StoredRulesStatus("main", old, refresh_requested=False, installation_available=False)
    )

    status = _get(store)

    assert status == RulesStatus(
        RulesStatusKind.UNAVAILABLE,
        "main",
        None,
        _NOW,
        diagnostics=(
            RulesDiagnostic(
                "policy_access_denied",
                "Нет доступа к документам правил репозитория.",
                None,
                None,
                False,
            ),
        ),
    )
    assert status is not None and status.retryable is False
    assert (store.requests, store.commits) == (0, 0)


def test_an_invisible_repository_is_none_without_a_request() -> None:
    store = FakeStatusStore(None)

    assert _get(store) is None
    assert store.requests == 0


@dataclass
class FakeRefreshStore:
    targets: tuple[RulesRefreshTarget, ...]
    saved: list[tuple[UUID, RulesReading]] = field(default_factory=list)
    claims: list[tuple[datetime, datetime, int]] = field(default_factory=list)
    lost: frozenset[UUID] = frozenset()

    @property
    def rules(self) -> Self:
        return self

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        return None

    async def claim(
        self, *, now: datetime, until: datetime, limit: int
    ) -> tuple[RulesRefreshTarget, ...]:
        self.claims.append((now, until, limit))
        claimed, self.targets = self.targets, ()
        return claimed

    async def save(
        self, target: RulesRefreshTarget, reading: RulesReading, *, checked_at: datetime
    ) -> bool:
        if target.repository_id in self.lost:
            return False
        self.saved.append((target.repository_id, reading))
        return True

    async def request_for_provider_repository(
        self, installation_external_id: int, repository_external_id: int
    ) -> bool:
        raise AssertionError("not used by the refresh")


@dataclass
class FakeReader:
    read: Callable[[RulesRefreshTarget], Any]


def test_refresh_saves_each_claimed_read_and_an_unexpected_failure_as_transient() -> None:
    broken = RulesRefreshTarget(UUID(int=1), 17, "octo/broken", "main", _NOW, _LEASE_END, "github")
    store = FakeRefreshStore((broken, _TARGET))
    missing = RulesReading(RulesStatusKind.MISSING, "main", _HEAD)

    async def read(target: RulesRefreshTarget) -> RulesReading:
        if target is broken:
            raise RuntimeError("bug")
        return missing

    refreshed = asyncio.run(
        RefreshRulesStatuses(
            uow_factory=lambda: store, reader=FakeReader(read), now=lambda: _NOW
        ).execute()
    )

    assert refreshed == 2
    # The failed read is stored as a temporary failure, so a GET asks again after 30 s.
    transient = RulesReading(
        RulesStatusKind.UNAVAILABLE,
        "main",
        None,
        diagnostics=(
            RulesDiagnostic(
                "policy_fetch_transient",
                "Не удалось прочитать документы правил из GitHub.",
                None,
                None,
                True,
            ),
        ),
    )
    assert store.saved == [(UUID(int=1), transient), (_REPOSITORY, missing)]
    assert store.claims == [(_NOW, _NOW + timedelta(minutes=5), 20)]


def test_a_read_whose_claim_was_taken_over_is_dropped(caplog: pytest.LogCaptureFixture) -> None:
    store = FakeRefreshStore((_TARGET,), lost=frozenset({_REPOSITORY}))
    missing = RulesReading(RulesStatusKind.MISSING, "main", _HEAD)

    async def read(target: RulesRefreshTarget) -> RulesReading:
        return missing

    with caplog.at_level("WARNING"):
        refreshed = asyncio.run(
            RefreshRulesStatuses(
                uow_factory=lambda: store, reader=FakeReader(read), now=lambda: _NOW
            ).execute()
        )

    assert refreshed == 1
    assert store.saved == []
    assert "claim was taken over" in caplog.text


def test_a_rate_limit_keeps_its_retry_after_for_the_next_get() -> None:
    reading = _read(
        FakeGitHub(rules=b"x", failures={f"/git/trees/{_HEAD}": 429}, headers={"Retry-After": "90"})
    )

    (diagnostic,) = reading.diagnostics
    assert (diagnostic.code, diagnostic.retry_after) == (
        "policy_rate_limited",
        timedelta(seconds=90),
    )


@pytest.mark.parametrize(
    ("header", "stored"),
    [
        ("86400", timedelta(days=1)),
        ("999999", timedelta(days=1)),
        # The policy classifier is shared with runs: a value too long for int() waits the
        # ceiling, and leading zeros do not count toward that length.
        ("1" * 5000, timedelta(days=1)),
        ("0" * 5000 + "120", timedelta(seconds=120)),
    ],
    ids=["one-day", "capped-at-one-day", "over-int-limit", "zero-padded"],
)
def test_the_stored_retry_after_is_at_most_one_day(header: str, stored: timedelta) -> None:
    reading = _read(
        FakeGitHub(
            rules=b"x", failures={f"/git/trees/{_HEAD}": 429}, headers={"Retry-After": header}
        )
    )

    assert reading.diagnostics[0].retry_after == stored


def test_a_store_must_say_whether_the_installation_is_available() -> None:
    # No fail-open default: a store that forgets the flag fails instead of showing a lost
    # repository as readable.
    with pytest.raises(TypeError, match="installation_available"):
        StoredRulesStatus("main", None, refresh_requested=False)  # type: ignore[call-arg]
