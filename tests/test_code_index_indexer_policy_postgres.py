"""PostgreSQL evidence for authoritative policy reads during indexing (#207)."""

from __future__ import annotations

import pytest

from app.modules.code_index.application import build_index
from app.modules.code_index.application.build_index import (
    BuildMode,
    BuildStatus,
    FullBuildReason,
)
from app.modules.code_index.application.exclusions import ExclusionClass
from app.modules.code_index.domain.enums import IndexStatus
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

pytest_plugins = ("tests.code_index_indexer_fixtures",)

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
