"""PostgreSQL evidence for authoritative policy reads during indexing (#207)."""

from __future__ import annotations

from datetime import datetime

import httpx
import pytest
from sqlalchemy import text

from app.modules.code_index.application import build_index
from app.modules.code_index.application.build_index import (
    BuildMode,
    BuildStatus,
    FullBuildReason,
)
from app.modules.code_index.application.exclusions import ExclusionClass
from app.modules.code_index.domain.enums import IndexStatus
from app.modules.code_index.infrastructure.github_snapshot_source import (
    GitHubRepositorySnapshotSource,
)
from app.modules.reviews.application.policy_errors import PolicyError, PolicyErrorCode
from tests.code_index_indexer_harness import (
    _USER,
    RESOLVED,
    Indexer,
    Link,
    Tree,
    _Database,
    _imports,
    _raw_rows,
    _record,
    _run,
    _uses,
)

pytest_plugins = ("tests.code_index_postgres", "tests.code_index_indexer_fixtures")

# --- authoritative policy excluded from source archives (#207) ----------------------------

_HIDDEN_RULES = b"```review-ignore\nignored.py\n```\n"


def _hidden_policy_tree(*, target: int = 1, rules: bytes = _HIDDEN_RULES) -> Tree:
    return {
        "lib.py": f"def target():\n    return {target}\n".encode(),
        "user.py": _USER,
        "ignored.py": f"def must_not_be_stored():\n    return {target + 98}\n".encode(),
        ".review/rules.md": rules,
        ".review/notes.md": b"policy notes\n",
    }


@pytest.mark.integration
@pytest.mark.parametrize(
    "hidden", [".review/rules.md", ".review"], ids=["hidden-rules", "hidden-parent"]
)
def test_hidden_unchanged_policy_incremental_rows_equal_fresh_full(
    database: _Database, hidden: str
) -> None:
    async def scenario(indexer: Indexer) -> None:
        indexer.branch.export_ignored = frozenset({hidden})
        before = await indexer.start(_hidden_policy_tree())
        head = indexer.branch.push(_hidden_policy_tree(target=2))

        outcome = await indexer.build(database.a)
        incremental_job = indexer.jobs[-1]
        after = await indexer.incremental_equals_full(outcome, before)

        assert outcome.commit_sha == head
        assert (incremental_job.requests, outcome.github_request_count) == (8, 8)
        assert indexer.jobs[-1].request_count == 7
        assert {path for path, _ in after.files} == {"lib.py", "user.py"}
        assert len(after.symbols) == 2
        assert after.edges == frozenset(
            {_imports("user.py", "lib.py"), _uses("user.py", "lib.py", "target")}
        )
        assert after.imports == frozenset({_record("user.py", "lib", RESOLVED, "lib.py")})
        state = await indexer.state(database.a)
        full_state = await indexer.state(database.b)
        assert (state.file_count, state.symbol_count, state.edge_count, state.pending_bytes) == (
            2,
            2,
            2,
            0,
        )
        assert state.stored_bytes == full_state.stored_bytes
        assert state.stored_bytes > 0
        # The changed ignored source is examined but contributes no stored rows.
        assert outcome.exclusions[ExclusionClass.IGNORED] == 1

    _run(database, scenario)


@pytest.mark.integration
def test_hidden_policy_change_forces_full_and_matches_fresh_full(database: _Database) -> None:
    async def scenario(indexer: Indexer) -> None:
        indexer.branch.export_ignored = frozenset({".review"})
        before = await indexer.start(_hidden_policy_tree(rules=b"policy without ignore masks\n"))
        assert {path for path, _ in before.files} == {"lib.py", "user.py", "ignored.py"}
        head = indexer.branch.push(_hidden_policy_tree(target=2))

        outcome = await indexer.build(database.a)

        assert (outcome.status, outcome.mode, outcome.full_reason) == (
            BuildStatus.PUBLISHED,
            BuildMode.FULL,
            FullBuildReason.EXCLUSIONS_CHANGED,
        )
        assert outcome.commit_sha == head
        assert indexer.jobs[-1].compares == 1
        assert outcome.github_request_count == 8
        after = await indexer.snapshot(database.a)
        await indexer.fresh_full(database.b)
        assert after == await indexer.snapshot(database.b)
        assert {path for path, _ in after.files} == {"lib.py", "user.py"}
        a, b = await indexer.state(database.a), await indexer.state(database.b)
        assert (a.file_count, a.symbol_count, a.edge_count, a.pending_bytes) == (2, 2, 2, 0)
        assert a.stored_bytes == b.stored_bytes

    _run(database, scenario)


@pytest.mark.integration
@pytest.mark.parametrize(
    ("notes", "requests"), [(False, 5), (True, 6)], ids=["no-review-directory", "no-rules-blob"]
)
def test_proven_missing_policy_indexes_with_empty_custom_masks(
    database: _Database, notes: bool, requests: int
) -> None:
    async def scenario(indexer: Indexer) -> None:
        tree = {"lib.py": b"def target():\n    return 1\n", "user.py": _USER}
        if notes:
            tree[".review/notes.md"] = b"notes\n"
        snapshot = await indexer.start(tree)

        assert {path for path, _ in snapshot.files} == {"lib.py", "user.py"}
        assert indexer.jobs[-1].request_count == requests
        published = await indexer.state(database.a)
        assert (await indexer.build(database.a)).status is BuildStatus.UNCHANGED
        unchanged = await indexer.state(database.a)
        assert unchanged.published_publication == published.published_publication
        assert unchanged.stored_bytes == published.stored_bytes
        assert unchanged.stats.github_request_count == 2
        assert await indexer.snapshot(database.a) == snapshot

    _run(database, scenario)


@pytest.mark.integration
@pytest.mark.parametrize(
    ("rules", "code", "requests"),
    [
        (b"\xff", "policy_document_decode_error", 6),
        (b"```review-ignore\nignored.py\n", "policy_ignore_block_unclosed", 6),
        (Link("../rules.md"), "policy_document_type_unsupported", 5),
        (None, "policy_revision_unavailable", 4),
    ],
    ids=["invalid-utf8", "unclosed-mask", "symlink", "unreadable-root"],
)
def test_hidden_policy_failure_keeps_previous_published_rows(
    database: _Database, rules: bytes | Link | None, code: str, requests: int
) -> None:
    async def scenario(indexer: Indexer) -> None:
        indexer.branch.export_ignored = frozenset({".review"})
        before = await indexer.start(_hidden_policy_tree())
        published = await indexer.state(database.a)
        rows = await _raw_rows(database, database.a)
        tree = dict(_hidden_policy_tree(target=2))
        if rules is not None:
            tree[".review/rules.md"] = rules
        head = indexer.branch.push(tree)
        if rules is None:
            indexer.branch.policy_errors[head] = PolicyError(
                PolicyErrorCode.REVISION_UNAVAILABLE, "unreadable pinned tree"
            )

        outcome = await indexer.build(database.a)

        assert (outcome.status, outcome.error_code) == (BuildStatus.RELEASED, code)
        state = await indexer.state(database.a)
        assert state.status is IndexStatus.READY
        assert state.last_error_code == code
        assert state.indexed_commit_sha == published.indexed_commit_sha
        assert state.published_publication == published.published_publication
        assert (state.file_count, state.symbol_count, state.edge_count, state.stored_bytes) == (
            published.file_count,
            published.symbol_count,
            published.edge_count,
            published.stored_bytes,
        )
        assert state.pending_bytes == 0
        assert (state.stats.downloaded_bytes, state.stats.github_request_count) == (0, requests)
        assert await indexer.snapshot(database.a) == before
        assert await _raw_rows(database, database.a) == rows

    _run(database, scenario)


async def _wake_marker(database: _Database) -> datetime | None:
    async with database.factory() as session:
        marker = await session.scalar(
            text("SELECT wake_requested_at FROM code_index_states WHERE repository_id = :id"),
            {"id": database.a},
        )
    assert marker is None or isinstance(marker, datetime)
    return marker


@pytest.mark.integration
@pytest.mark.parametrize(
    ("status", "target", "expected_code", "requests", "retries"),
    [
        (401, "tree", "policy_fetch_transient", 4, True),
        (401, "blob", "policy_fetch_transient", 6, True),
        (403, "tree", "policy_access_denied", 4, False),
        (403, "blob", "policy_access_denied", 6, False),
    ],
)
def test_policy_http_failure_reconciles_only_healable_401_on_same_head(
    database: _Database, status: int, target: str, expected_code: str, requests: int, retries: bool
) -> None:
    class Tokens:
        async def get_installation_access_token(self, installation_external_id: int) -> str:
            assert installation_external_id == 17
            return "token"

    async def scenario(indexer: Indexer) -> None:
        before = await indexer.start(_hidden_policy_tree())
        published = await indexer.state(database.a)
        head = indexer.branch.push(_hidden_policy_tree(target=2))
        async with indexer.uow() as uow:
            assert await uow.index.request_build(database.a, target_commit_sha=head)
            await uow.commit()

        def respond(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path == "/repos/octo/indexed-a":
                return httpx.Response(200, json={"default_branch": "main"})
            if path == "/repos/octo/indexed-a/commits/main":
                return httpx.Response(200, text=head)
            if path.endswith(f"/compare/{published.indexed_commit_sha}...{head}"):
                return httpx.Response(
                    200,
                    json={
                        "status": "ahead",
                        "files": [{"filename": "lib.py", "status": "modified"}],
                    },
                )
            if path.endswith(f"/git/trees/{head}"):
                if target == "tree":
                    return httpx.Response(status)
                return httpx.Response(
                    200,
                    json={
                        "tree": [
                            {"path": ".review", "type": "tree", "mode": "040000", "sha": "d" * 40}
                        ],
                        "truncated": False,
                    },
                )
            if path.endswith(f"/git/trees/{'d' * 40}"):
                return httpx.Response(
                    200,
                    json={
                        "tree": [
                            {"path": "rules.md", "type": "blob", "mode": "100644", "sha": "e" * 40}
                        ],
                        "truncated": False,
                    },
                )
            if path.endswith(f"/git/blobs/{'e' * 40}"):
                return httpx.Response(status)
            raise AssertionError(f"unexpected GitHub request: {path}")

        async with httpx.AsyncClient(
            base_url="https://api.github.com", transport=httpx.MockTransport(respond)
        ) as client:
            outcome = await indexer.build(
                database.a,
                1,
                source=lambda repository: GitHubRepositorySnapshotSource(
                    client=client,
                    token_provider=Tokens(),
                    installation_external_id=repository.installation_external_id,
                    repository_full_name=repository.full_name,
                ),
            )

        assert (outcome.status, outcome.error_code, outcome.github_request_count) == (
            BuildStatus.RELEASED,
            expected_code,
            requests,
        )
        released = await indexer.state(database.a)
        assert (released.status, released.last_error_code) == (IndexStatus.READY, expected_code)
        assert (released.indexed_commit_sha, released.published_publication) == (
            published.indexed_commit_sha,
            published.published_publication,
        )
        assert await indexer.snapshot(database.a) == before

        marker = await _wake_marker(database)
        assert marker is not None
        async with indexer.uow() as uow:
            assert await uow.index.clear_wake_request(database.a, marker)
            await uow.commit()
        async with database.factory() as session:
            read_at = await session.scalar(text("SELECT clock_timestamp()"))
        assert read_at is not None
        async with indexer.uow() as uow:
            marked = await uow.index.reconcile_head(
                database.a,
                generation=1,
                branch="main",
                commit_sha=head,
                read_at=read_at,
                versions_current=lambda profile: True,
            )
            await uow.commit()
        assert marked is retries
        reconciled = await indexer.state(database.a)
        assert (await _wake_marker(database) is not None) is retries
        assert reconciled.last_error_code == expected_code

        if retries:
            healed = await indexer.build(database.a)
            assert healed.status is BuildStatus.PUBLISHED
            healed_state = await indexer.state(database.a)
            assert (healed_state.indexed_commit_sha, healed_state.last_error_code) == (head, None)

    _run(database, scenario)


@pytest.mark.integration
def test_same_commit_v1_index_rebuilds_with_hidden_policy(
    database: _Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario(indexer: Indexer) -> None:
        indexer.branch.export_ignored = frozenset({".review"})
        monkeypatch.setattr(build_index, "INDEX_EXCLUSIONS_VERSION", "index-exclusions/v1")
        await indexer.start(_hidden_policy_tree())
        before = await indexer.state(database.a)
        monkeypatch.setattr(build_index, "INDEX_EXCLUSIONS_VERSION", "index-exclusions/v2")

        outcome = await indexer.build(database.a)

        assert (outcome.status, outcome.mode, outcome.full_reason) == (
            BuildStatus.PUBLISHED,
            BuildMode.FULL,
            FullBuildReason.PROFILE_CHANGED,
        )
        assert outcome.commit_sha == before.indexed_commit_sha
        assert outcome.github_request_count == 7
        assert indexer.jobs[-1].compares == 0
        state = await indexer.state(database.a)
        assert state.published_publication > before.published_publication
        assert state.profile is not None
        assert before.profile is not None
        assert state.profile.limits_profile_version != before.profile.limits_profile_version
        assert {path for path, _ in (await indexer.snapshot(database.a)).files} == {
            "lib.py",
            "user.py",
        }

    _run(database, scenario)
