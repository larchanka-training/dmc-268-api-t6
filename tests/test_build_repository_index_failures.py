"""BuildRepositoryIndex failure, release, and policy behavior."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import replace
from datetime import timedelta
from typing import Any

import pytest

from app.modules.code_index.application import build_index
from app.modules.code_index.application.build_index import (
    BuildMode,
    BuildStatus,
    FullBuildReason,
    limits_profile_version,
)
from app.modules.code_index.application.exclusions import ExclusionClass
from app.modules.code_index.application.index_store import (
    IndexAdmission,
    IndexBuildStats,
    IndexedFile,
    IndexedSymbol,
    LeaseClaim,
)
from app.modules.code_index.application.symbol_rows import file_rows, symbol_rows
from app.modules.code_index.application.vcs import (
    ArchiveUnsafe,
    IndexLimitExceeded,
    VcsAccessDenied,
    VcsUnavailable,
)
from app.modules.code_index.domain.limits import IndexLimits
from app.modules.reviews.application.ast_resolution import ImportResolution
from app.modules.reviews.application.ast_symbols import (
    AstFacts,
)
from app.modules.reviews.application.policy_errors import PolicyError, PolicyErrorCode
from app.modules.reviews.application.review_ignore import (
    parse_review_ignore,
)
from tests.build_repository_index_fakes import (
    _ADMISSION,
    _DIGEST,
    _FENCE,
    _GENERATION,
    _HEAD,
    _LIMITS,
    _MIB,
    _MODELS,
    _NO_INDEX,
    _OLD,
    _OLD_TREE,
    _PROFILE,
    _REPO,
    _RULES,
    _SETTINGS,
    _TREE,
    Link,
    Tree,
    World,
    _blob,
    _changed,
    _comparison,
    _incremental_world,
    _indexed,
    _state,
    _world,
)

_TARGETS = [
    pytest.param(None, "d" * 40, True, id="none-then-a-target"),
    pytest.param(None, None, False, id="none-then-none"),
    pytest.param("d" * 40, "d" * 40, False, id="same-target"),
    pytest.param("d" * 40, "e" * 40, True, id="another-target"),
]


@pytest.mark.parametrize(("claimed", "target", "moved"), _TARGETS)
def test_a_target_that_moved_during_the_build_wakes_the_repository(
    claimed: str | None, target: str | None, moved: bool
) -> None:
    world = _world(
        claim=LeaseClaim(_FENCE, claimed),
        after_publish=_state(indexed_commit_sha=_HEAD, target_commit_sha=target),
    )

    outcome = world.run()

    assert outcome.status is BuildStatus.PUBLISHED
    assert world.wakeups.woken == ([(_REPO, _GENERATION)] if moved else [])
    assert world.tracker.log[-2:] == (["get_state", "wake"] if moved else ["commit", "get_state"])


def _unsafe_archive(world: World) -> None:
    world.archives.error = ArchiveUnsafe("unsafe")


def _unavailable_head(world: World) -> None:
    world.source.errors["head"] = VcsUnavailable("down")


def _crashing_parser(world: World) -> None:
    world.parser.error = RuntimeError("parser crashed")


def _unchanged(world: World) -> None:
    world.store.state = _state(indexed_commit_sha=_HEAD)


def _empty(world: World) -> None:
    world.source.head_value = None


def _access_denied(world: World) -> None:
    world.source.errors["head"] = VcsAccessDenied("gone")


@pytest.mark.parametrize(
    ("arrange", "status"),
    [
        (_unchanged, BuildStatus.UNCHANGED),
        (_empty, BuildStatus.EMPTY),
        (_access_denied, BuildStatus.RELEASED),
    ],
    ids=["unchanged", "empty", "released"],
)
@pytest.mark.parametrize(("claimed", "target", "moved"), _TARGETS)
def test_a_target_that_moved_before_a_release_wakes_the_repository(
    arrange: Any, status: BuildStatus, claimed: str | None, target: str | None, moved: bool
) -> None:
    # A wake-up refused as LEASED during the job left its target on the state row.
    world = _world(
        state=_state(),
        claim=LeaseClaim(_FENCE, claimed),
        after_release=_state(target_commit_sha=target),
    )
    arrange(world)

    outcome = world.run()

    assert outcome.status is status
    assert world.wakeups.woken == ([(_REPO, _GENERATION)] if moved else [])
    expected = ["release", "commit", "get_state", *(["wake"] if moved else [])]
    assert world.tracker.log[-len(expected) :] == expected


def _lost_lease(world: World) -> None:
    world.store.lose_lease_at = "replace_file"


@pytest.mark.parametrize(
    ("arrange", "status", "tail"),
    [
        (_unavailable_head, BuildStatus.TRANSIENT, ["release", "commit"]),
        (_unsafe_archive, BuildStatus.FAILED, ["mark_failed", "commit"]),
        (_lost_lease, BuildStatus.LEASE_LOST, ["replace_file"]),
    ],
    ids=["transient", "failed", "lease-lost"],
)
def test_a_moved_target_is_not_rechecked_after_a_transient_failed_or_lost_lease(
    arrange: Any, status: BuildStatus, tail: list[str]
) -> None:
    world = _world(state=_state(), after_release=_state(target_commit_sha="d" * 40))
    arrange(world)

    outcome = world.run()

    assert outcome.status is status
    assert world.wakeups.woken == []
    assert world.tracker.log[-len(tail) :] == tail


@pytest.mark.parametrize("step", ["replace_file", "publish", "discard_abandoned", "release"])
def test_a_lost_lease_stops_the_job_without_a_write_or_a_retry(step: str) -> None:
    world = _world(lose_lease_at=step)
    if step == "release":
        world.source.head_value = None

    outcome = world.run()

    assert outcome.status is BuildStatus.LEASE_LOST
    assert world.store.failures == []
    assert world.store.releases == []
    assert world.wakeups.woken == []


def test_a_lost_lease_while_failing_is_still_a_lost_lease() -> None:
    world = _world(lose_lease_at="mark_failed")
    world.archives.error = ArchiveUnsafe("unsafe")

    assert world.run().status is BuildStatus.LEASE_LOST


def _fail_at_archive(world: World, error: BaseException) -> None:
    world.source.errors["archive"] = error


def _fail_at_scan(world: World, error: BaseException) -> None:
    world.archives.error = error


@pytest.mark.parametrize(
    ("inject", "error", "code", "requests"),
    [
        (
            _fail_at_archive,
            IndexLimitExceeded("download_bytes"),
            "limit_exceeded:download_bytes",
            3,
        ),
        (_fail_at_scan, IndexLimitExceeded("unpacked_bytes"), "limit_exceeded:unpacked_bytes", 3),
        (_fail_at_scan, IndexLimitExceeded("archive_entries"), "limit_exceeded:archive_entries", 3),
        (
            _fail_at_archive,
            IndexLimitExceeded("provider_requests"),
            "limit_exceeded:provider_requests",
            3,
        ),
        (_fail_at_scan, IndexLimitExceeded("deadline"), "limit_exceeded:deadline", 3),
        (_fail_at_scan, ArchiveUnsafe("unsafe"), "archive_unsafe", 3),
    ],
)
def test_an_archive_failure_fails_the_index_with_its_code_and_the_request_count(
    inject: Any, error: BaseException, code: str, requests: int
) -> None:
    world = _world()
    inject(world, error)

    outcome = world.run()

    assert (outcome.status, outcome.error_code) == (BuildStatus.FAILED, code)
    ((failed_code, stats),) = world.store.failures
    assert failed_code == code
    assert stats is not None
    assert stats.github_request_count == requests
    assert world.store.releases == []


def test_provider_request_cap_keeps_the_previous_index_publication() -> None:
    world = _world(
        state=_state(file_count=1),
        comparison=_comparison(_changed("app/models.py")),
        files={"app/models.py": _blob(_MODELS)},
    )
    world.source.errors["archive"] = IndexLimitExceeded("provider_requests")

    outcome = world.run()

    assert (outcome.status, outcome.error_code) == (
        BuildStatus.FAILED,
        "limit_exceeded:provider_requests",
    )
    assert world.store.publications == []
    assert world.store.state is not None
    assert world.store.state.indexed_commit_sha == _OLD


@pytest.mark.parametrize(
    ("limits", "admission", "other_bytes", "code"),
    [
        (replace(_LIMITS, parsed_files=1), _ADMISSION, 0, "limit_exceeded:parsed_files"),
        (replace(_LIMITS, symbols=1), _ADMISSION, 0, "limit_exceeded:symbols"),
        (replace(_LIMITS, edges=1), _ADMISSION, 0, "limit_exceeded:edges"),
        (
            _LIMITS,
            replace(_ADMISSION, repository_storage_bytes=150),
            0,
            "limit_exceeded:repository_storage",
        ),
        (
            _LIMITS,
            replace(_ADMISSION, workspace_storage_bytes=1_000),
            950,
            "limit_exceeded:workspace_storage",
        ),
    ],
)
def test_a_build_limit_fails_the_index_before_writing_over_it(
    limits: IndexLimits, admission: IndexAdmission, other_bytes: int, code: str
) -> None:
    world = _world(bytes_per_file=100, other_bytes=other_bytes)

    outcome = world.run(limits, replace(_SETTINGS, admission=admission, batch_files=1))

    assert (outcome.status, outcome.error_code) == (BuildStatus.FAILED, code)
    assert [failed for failed, _ in world.store.failures] == [code]
    assert world.store.publications == []


@pytest.mark.parametrize(("limit", "total"), [("parsed_files", 3), ("symbols", 3), ("edges", 2)])
def test_the_incremental_limits_count_the_rows_the_build_keeps(limit: str, total: int) -> None:
    # 4 stored files with 6 symbols and 5 edges; closing app/models, util and cli leaves 1
    # file, 1 symbol and 2 edges, and the build writes 2 files with 2 symbols and no edge:
    # 3 files, 3 symbols and 2 edges in all.
    counts = {"app/models.py": (2, 0), "app/util.py": (2, 1), "app/cli.py": (1, 2)}
    world = _incremental_world(counts=counts)
    assert world.run(replace(_LIMITS, **{limit: total})).status is BuildStatus.PUBLISHED

    world = _incremental_world(counts=counts)
    outcome = world.run(replace(_LIMITS, **{limit: total - 1}))
    assert (outcome.status, outcome.error_code) == (BuildStatus.FAILED, f"limit_exceeded:{limit}")


# Two exported top-level functions per file: each summary holds 2 symbols.
_GO_A = b"package m\n\nfunc A() {}\n\nfunc B() {}\n"
_GO_B = b"package m\n\nfunc C() {}\n\nfunc D() {}\n"
_GO_OLD: Tree = {"a.go": _GO_A, "b.go": _GO_B, "c.go": b"package m\n\nfunc E() {}\n"}
_GO_HEAD: Tree = {**_GO_OLD, "c.go": b"package m\n\nfunc F() {}\n"}


def test_the_target_pass_stops_before_a_parse_over_the_parsed_files_limit() -> None:
    # Every Go file is a target: `b.go` is the second path this job would parse.
    world = _world({"a.go": _GO_A, "b.go": _GO_B})

    outcome = world.run(replace(_LIMITS, parsed_files=1))

    assert (outcome.status, outcome.error_code) == (
        BuildStatus.FAILED,
        "limit_exceeded:parsed_files",
    )
    assert world.parser.parsed == ["a.go"]
    assert outcome.parsed_files == 1
    assert world.store.written == []
    assert world.store.publications == []


@pytest.mark.parametrize(
    ("limit", "status", "code", "parsed", "written"),
    [
        pytest.param(
            3,
            BuildStatus.FAILED,
            "limit_exceeded:symbols",
            ["a.go", "b.go"],
            [],
            id="over-the-limit",
        ),
        pytest.param(
            4,
            BuildStatus.PUBLISHED,
            None,
            ["a.go", "b.go", "a.go", "b.go"],
            ["a.go", "b.go"],
            id="at-the-limit",
        ),
    ],
)
def test_the_symbols_the_target_summaries_hold_count_toward_the_symbols_limit(
    limit: int, status: BuildStatus, code: str | None, parsed: list[str], written: list[str]
) -> None:
    # The two summaries hold 4 symbols: over 3, the target pass stops the build before
    # the main pass parses a file again.
    world = _world({"a.go": _GO_A, "b.go": _GO_B})

    outcome = world.run(replace(_LIMITS, symbols=limit))

    assert (outcome.status, outcome.error_code) == (status, code)
    assert world.parser.parsed == parsed
    assert world.written() == written


def test_held_symbols_accumulate_across_target_batches() -> None:
    # One file per archive batch: the total runs over the batches, not within one.
    world = _world({"a.go": _GO_A, "b.go": _GO_B})

    outcome = world.run(replace(_LIMITS, symbols=3), replace(_SETTINGS, batch_files=1))

    assert (outcome.status, outcome.error_code) == (BuildStatus.FAILED, "limit_exceeded:symbols")
    assert world.parser.parsed == ["a.go", "b.go"]
    assert world.written() == []


def _go_world(mode: BuildMode) -> World:
    if mode is BuildMode.FULL:
        return _world(_GO_HEAD)
    # The index holds the 3 files of the base; HEAD changes `c.go` only.
    return _world(
        _GO_HEAD,
        state=_state(file_count=3),
        comparison=_comparison(_changed("c.go")),
        files=_indexed(_GO_OLD),
    )


@pytest.mark.parametrize(
    ("mode", "limit", "status", "code", "parsed", "parsed_files"),
    [
        pytest.param(
            BuildMode.FULL,
            3,
            BuildStatus.PUBLISHED,
            None,
            ["a.go", "b.go", "c.go", "a.go", "b.go", "c.go"],
            3,
            id="full-at-the-limit",
        ),
        pytest.param(
            BuildMode.INCREMENTAL,
            3,
            BuildStatus.PUBLISHED,
            None,
            ["a.go", "b.go", "c.go", "c.go"],
            3,
            id="incremental-at-the-limit",
        ),
        pytest.param(
            BuildMode.FULL,
            2,
            BuildStatus.FAILED,
            "limit_exceeded:parsed_files",
            ["a.go", "b.go"],
            2,
            id="full-over-the-limit",
        ),
        pytest.param(
            BuildMode.INCREMENTAL,
            2,
            BuildStatus.FAILED,
            "limit_exceeded:parsed_files",
            ["a.go", "b.go"],
            2,
            id="incremental-over-the-limit",
        ),
    ],
)
def test_the_parsed_files_limit_counts_each_path_once_in_both_modes(
    mode: BuildMode,
    limit: int,
    status: BuildStatus,
    code: str | None,
    parsed: list[str],
    parsed_files: int,
) -> None:
    # The target pass parses the 3 Go files; the main pass parses them again (full) or
    # the changed one (incremental) without counting a path twice. The incremental graph
    # is the 2 kept files and `c.go`: the targets that are kept files are not counted
    # into it again.
    world = _go_world(mode)

    outcome = world.run(replace(_LIMITS, parsed_files=limit))

    assert (outcome.status, outcome.error_code, outcome.mode) == (status, code, mode)
    assert world.parser.parsed == parsed
    assert outcome.parsed_files == parsed_files


def test_the_repository_quota_counts_the_published_bytes_too() -> None:
    # 100 published bytes and 100 per written file: the second write is over 250.
    world = _world(state=_state(stored_bytes=100), bytes_per_file=100)
    admission = replace(_ADMISSION, repository_storage_bytes=250)

    outcome = world.run(settings=replace(_SETTINGS, admission=admission, batch_files=1))

    assert (outcome.status, outcome.error_code) == (
        BuildStatus.FAILED,
        "limit_exceeded:repository_storage",
    )
    assert len(world.store.written) == 2
    assert world.store.publications == []


def test_the_workspace_quota_counts_the_sum_of_the_write_and_rolls_the_batch_back() -> None:
    # The other repositories of the Workspace hold 850 bytes: the second file of the
    # batch brings the sum that `replace_file` returns to 1050, over 1000.
    world = _world(bytes_per_file=100, other_bytes=850)
    admission = replace(_ADMISSION, workspace_storage_bytes=1_000)

    outcome = world.run(settings=replace(_SETTINGS, admission=admission, batch_files=2))

    assert (outcome.status, outcome.error_code) == (
        BuildStatus.FAILED,
        "limit_exceeded:workspace_storage",
    )
    # Both writes ran in one transaction, and it ended without a commit.
    assert len({transaction for transaction, _ in world.store.written}) == 1
    log = world.tracker.log
    assert log[log.index("replace_file") :] == [
        "replace_file",
        "replace_file",
        "mark_failed",
        "commit",
    ]
    assert world.store.publications == []


def test_a_passed_deadline_fails_the_index() -> None:
    world = _world()

    outcome = world.run(replace(_LIMITS, deadline_seconds=0))

    assert (outcome.status, outcome.error_code) == (BuildStatus.FAILED, "limit_exceeded:deadline")
    assert world.network() == []


def test_the_deadline_stops_the_parse_pass_between_files() -> None:
    world = _world()
    world.parser.first_delay = 1.0

    outcome = world.run(replace(_LIMITS, deadline_seconds=1))

    assert (outcome.status, outcome.error_code) == (BuildStatus.FAILED, "limit_exceeded:deadline")
    assert world.parser.parsed == ["app/models.py"]
    assert world.store.publications == []


def test_a_last_parse_that_ends_past_the_deadline_neither_writes_nor_publishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The only file is the last one; its parse is not cut, it is checked once it ends.
    rows: list[str] = []

    def recorded_rows(path: str, facts: AstFacts) -> tuple[IndexedSymbol, ...]:
        rows.append(path)
        return symbol_rows(path, facts)

    monkeypatch.setattr(build_index, "symbol_rows", recorded_rows)
    world = _world({"app/models.py": _MODELS})
    world.parser.first_delay = 1.2

    outcome = world.run(replace(_LIMITS, deadline_seconds=1))

    assert (outcome.status, outcome.error_code) == (BuildStatus.FAILED, "limit_exceeded:deadline")
    assert world.parser.parsed == ["app/models.py"]
    # No row is built from a result that came back past the deadline.
    assert rows == []
    assert world.store.written == []
    assert world.store.publications == []
    assert [code for code, _ in world.store.failures] == ["limit_exceeded:deadline"]


def test_a_deadline_that_passes_after_the_last_parse_stops_the_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The rows of the last file take the job past its deadline after its parse ended.
    def slow_rows(*args: Any) -> IndexedFile:
        time.sleep(1.2)
        return file_rows(*args)

    monkeypatch.setattr(build_index, "file_rows", slow_rows)
    world = _world({"app/models.py": _MODELS})

    outcome = world.run(replace(_LIMITS, deadline_seconds=1))

    assert (outcome.status, outcome.error_code) == (BuildStatus.FAILED, "limit_exceeded:deadline")
    assert world.parser.parsed == ["app/models.py"]
    assert world.store.written == []
    assert world.store.publications == []


def test_a_deadline_that_passes_during_the_last_write_stops_the_publication() -> None:
    world = _world({"app/models.py": _MODELS}, write_delay=1.2)

    outcome = world.run(replace(_LIMITS, deadline_seconds=1))

    assert (outcome.status, outcome.error_code) == (BuildStatus.FAILED, "limit_exceeded:deadline")
    assert world.written() == ["app/models.py"]
    assert world.store.publications == []
    assert [code for code, _ in world.store.failures] == ["limit_exceeded:deadline"]


def test_a_deadline_that_passes_during_a_write_stops_the_next_file_before_its_parse() -> None:
    # One file per batch: the first write outlasts the deadline, between two parses.
    world = _world({"a.py": b"A = 1\n", "b.py": b"B = 1\n"}, write_delay=1.2)

    outcome = world.run(replace(_LIMITS, deadline_seconds=1), replace(_SETTINGS, batch_files=1))

    assert (outcome.status, outcome.error_code) == (BuildStatus.FAILED, "limit_exceeded:deadline")
    assert world.parser.parsed == ["a.py"]
    assert world.written() == ["a.py"]
    assert world.store.publications == []


@pytest.mark.parametrize(("step", "requests"), [("rules_document", 2), ("archive", 3)])
def test_the_deadline_interrupts_a_hanging_vcs_call(step: str, requests: int) -> None:
    world = _world()
    world.source.hang = step

    outcome = world.run(replace(_LIMITS, deadline_seconds=1))

    assert (outcome.status, outcome.error_code) == (BuildStatus.FAILED, "limit_exceeded:deadline")
    ((_, stats),) = world.store.failures
    assert stats is not None
    assert (stats.downloaded_bytes, stats.github_request_count) == (0, requests)


@pytest.mark.parametrize(
    ("step", "error", "status", "code", "retry_after"),
    [
        (
            "head",
            VcsUnavailable("rate", rate_limited=True, retry_after=timedelta(seconds=120)),
            BuildStatus.TRANSIENT,
            "vcs_rate_limited",
            timedelta(seconds=120),
        ),
        ("archive", VcsUnavailable("down"), BuildStatus.TRANSIENT, "vcs_unavailable", None),
        ("compare", VcsAccessDenied("gone"), BuildStatus.RELEASED, "vcs_access_denied", None),
    ],
)
def test_a_vcs_failure_releases_the_lease_with_its_code(
    step: str,
    error: BaseException,
    status: BuildStatus,
    code: str,
    retry_after: timedelta | None,
) -> None:
    world = _world(state=_state())
    world.source.errors[step] = error

    outcome = world.run()

    assert (outcome.status, outcome.error_code, outcome.retry_after) == (status, code, retry_after)
    assert world.store.releases == [code]
    assert world.store.failures == []


@pytest.mark.parametrize(
    ("rules", "code"),
    [
        (b"```review-ignore\nlegacy/**\n", "policy_ignore_block_unclosed"),
        (Link("../README.md"), "policy_document_type_unsupported"),
        (b"x" * 65_537, "policy_document_too_large"),
        (b"x" * (_MIB + 1), "policy_document_too_large"),
        (b"\xff\xfe", "policy_document_decode_error"),
    ],
    ids=[
        "unclosed-ignore-block",
        "symlink",
        "over-the-document-limit",
        "over-the-read-limit",
        "undecodable",
    ],
)
def test_a_policy_error_releases_the_lease_with_the_policy_code(
    rules: bytes | Link, code: str
) -> None:
    world = _world({**_TREE, _RULES: rules})

    outcome = world.run()

    assert (outcome.status, outcome.error_code) == (BuildStatus.RELEASED, code)
    assert world.store.releases == [code]
    assert world.store.written == []


def test_a_review_directory_that_is_not_a_directory_is_a_policy_error() -> None:
    world = _world({**_TREE, ".review": Link("elsewhere")})

    outcome = world.run()

    assert (outcome.status, outcome.error_code) == (
        BuildStatus.RELEASED,
        "policy_document_type_unsupported",
    )


def test_the_ignore_policy_of_head_excludes_its_paths() -> None:
    rules = b"```review-ignore\napp/views.py\n```\n"
    world = _world({**_TREE, _RULES: rules})

    outcome = world.run()

    assert world.written() == ["app/models.py"]
    assert outcome.exclusions[ExclusionClass.IGNORED] == 1
    profile = world.store.publications[0]["profile"]
    assert profile.exclusions_digest == parse_review_ignore(rules.decode()).digest


def test_an_unexpected_error_releases_the_lease_and_propagates() -> None:
    world = _world()
    world.parser.error = RuntimeError("parser crashed")

    with pytest.raises(RuntimeError, match="parser crashed"):
        world.run()

    assert world.store.releases == ["internal_error"]
    assert world.store.failures == []


def test_a_base_exception_that_is_not_an_exception_releases_the_lease_without_a_code() -> None:
    # Like a cancellation, it is not a failure of the index (#145): the lease is released
    # with no `last_error_code`, and the exception still propagates.
    world = _world()
    world.parser.error = KeyboardInterrupt()

    with pytest.raises(KeyboardInterrupt):
        world.run()

    assert world.store.releases == [None]
    assert world.store.failures == []


def _unfinished_discard(world: World) -> None:
    world.store.discards = [2]


def _policy_error(world: World) -> None:
    world.source.policy_tree = {_RULES: b"```review-ignore\nlegacy/**\n"}


def _unavailable_archive(world: World) -> None:
    world.source.errors["archive"] = VcsUnavailable("down")


def _failing_source_factory(world: World) -> None:
    world.source_error = RuntimeError("no source for this repository")


@pytest.mark.parametrize(
    ("arrange", "status", "downloaded", "requests"),
    [
        pytest.param(_unfinished_discard, BuildStatus.REWAKE, 0, 0, id="soft-cutoff"),
        pytest.param(_empty, BuildStatus.EMPTY, 0, 1, id="empty"),
        pytest.param(_unchanged, BuildStatus.UNCHANGED, 0, 1, id="unchanged"),
        pytest.param(_access_denied, BuildStatus.RELEASED, 0, 1, id="released-access"),
        pytest.param(_policy_error, BuildStatus.RELEASED, 0, 3, id="released-policy"),
        pytest.param(_unavailable_archive, BuildStatus.TRANSIENT, 0, 4, id="transient"),
        pytest.param(_crashing_parser, None, 7, 4, id="after-an-error"),
        pytest.param(_failing_source_factory, None, 0, 0, id="after-the-claim"),
    ],
)
def test_every_release_records_the_job_metrics(
    arrange: Any,
    status: BuildStatus | None,
    downloaded: int,
    requests: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The module function, not `time.monotonic`, which asyncio reads too.
    monkeypatch.setattr(build_index, "_elapsed_ms", lambda started: 1234)
    world = _world(state=_state())
    arrange(world)
    # A full discard portion outlasts the zero soft cutoff; the other paths discard none.
    settings = replace(_SETTINGS, discard_batch_rows=2, soft_cutoff_seconds=0)

    if status is None:
        with pytest.raises(RuntimeError):
            world.run(settings=settings)
    else:
        assert world.run(settings=settings).status is status

    assert world.store.release_stats == [IndexBuildStats(downloaded, 1234, requests)]


@pytest.mark.parametrize(
    ("arrange", "held", "failures", "releases"),
    [
        pytest.param(_unsafe_archive, "mark_failed", ["archive_unsafe"], [], id="mark-failed"),
        pytest.param(_unavailable_head, "release", [], ["vcs_unavailable"], id="release"),
        pytest.param(
            _crashing_parser, "release", [], ["internal_error"], id="release-after-an-error"
        ),
    ],
)
def test_a_cancellation_during_the_closing_write_does_not_cut_it(
    arrange: Any, held: str, failures: list[str], releases: list[str | None]
) -> None:
    world = _world(hold=held)
    arrange(world)

    async def scenario() -> None:
        job = asyncio.create_task(world.use_case().execute(_REPO, _GENERATION))
        await asyncio.wait_for(world.store.held.wait(), 5)
        job.cancel()
        with pytest.raises(asyncio.CancelledError):
            await job
        # The shielded write goes on after the job is cancelled.
        world.store.proceed.set()
        await asyncio.wait_for(world.store.finished.wait(), 5)

    asyncio.run(scenario())

    assert [code for code, _ in world.store.failures] == failures
    assert world.store.releases == releases
    assert world.tracker.log[-2:] == [held, "commit"]


def _parked_parse(world: World) -> asyncio.Event:
    world.parser.hang = True
    return world.parser.entered


def _parked_claim_exit(world: World) -> asyncio.Event:
    world.store.hold = "claim_exit"
    return world.store.held


@pytest.mark.parametrize(
    "park",
    [
        pytest.param(_parked_parse, id="while-parsing"),
        pytest.param(_parked_claim_exit, id="leaving-the-claim-transaction"),
    ],
)
def test_a_cancelled_job_releases_its_lease(park: Any) -> None:
    # A worker shutdown cancels the job: `CancelledError` is not an `Exception`.
    world = _world()
    parked = park(world)

    async def scenario() -> None:
        job = asyncio.create_task(world.use_case().execute(_REPO, _GENERATION))
        await asyncio.wait_for(parked.wait(), 5)
        job.cancel()
        with pytest.raises(asyncio.CancelledError):
            await job

    asyncio.run(scenario())

    assert world.store.releases == [None]
    assert world.store.failures == []
    assert world.tracker.log[-2:] == ["release", "commit"]


def test_no_transaction_is_open_during_a_vcs_call_or_a_wake_up() -> None:
    world = _incremental_world(after_publish=_state(target_commit_sha="d" * 40))

    world.run()

    # The tracker asserts it at every call; this pins the interleaving of one build.
    assert world.tracker.log == [
        "claim_lease",
        "commit",
        "discard_abandoned",
        "commit",
        "head",
        "get_state",
        "compare",
        "rules_document",
        "archive",
        "iter_files",
        "iter_imports",
        "visible_counts",
        "replace_file",
        "replace_file",
        "commit",
        "publish",
        "commit",
        "get_state",
        "wake",
    ]


def test_the_skip_counters_equal_one_per_exclusion_class() -> None:
    tree: Tree = {
        "bad\\path.py": b"x = 1\n",
        "config/.env.local": b"TOKEN=1\n",
        "vendor/lib.py": b"x = 1\n",
        "dist/app.js": b"x = 1\n",
        "README.md": b"# readme\n",
        "legacy/old.py": b"x = 1\n",
        "app/link.py": Link("models.py"),
        "app/huge.py": b"#" * (_MIB + 1),
        "app/large.py": b"#" * (256 * 1024 + 1),
        "app/blob.py": b"\x00binary",
        "app/broken.py": b"\xff\xfe = 1\n",
        "app/models.py": _MODELS,
        _RULES: b"```review-ignore\nlegacy/**\n```\n",
    }
    world = _world(tree)

    outcome = world.run()

    assert dict(outcome.exclusions) == {
        ExclusionClass.UNSUPPORTED_PATH: 1,
        ExclusionClass.SECRET: 1,
        ExclusionClass.VENDORED: 1,
        ExclusionClass.GENERATED: 1,
        # README.md and the rules document itself.
        ExclusionClass.UNSUPPORTED_LANGUAGE: 2,
        ExclusionClass.IGNORED: 1,
        ExclusionClass.SYMLINK: 1,
        ExclusionClass.OVER_READ_LIMIT: 1,
        ExclusionClass.OVER_PARSE_LIMIT: 1,
        ExclusionClass.BINARY: 1,
        ExclusionClass.PARSE_FAILED: 1,
    }
    assert outcome.skipped_unsupported_language == 2
    assert outcome.skipped_too_large == 2
    assert outcome.parse_failed == 1
    assert world.written() == ["app/models.py"]
    # The binary file is never parsed; the undecodable one is.
    assert world.parser.parsed == ["app/broken.py", "app/models.py"]


def test_one_log_line_ends_the_job_without_paths_or_content(
    caplog: pytest.LogCaptureFixture,
) -> None:
    world = _incremental_world()

    with caplog.at_level(logging.INFO, logger="app.modules.code_index.application.build_index"):
        world.run()

    (record,) = [
        item
        for item in caplog.records
        if item.name == "app.modules.code_index.application.build_index"
    ]
    message = record.getMessage()
    assert "status=published" in message
    assert "mode=incremental" in message
    assert str(_REPO) in message
    for path in _OLD_TREE:
        assert path not in message


def _file(world: World, path: str) -> IndexedFile:
    return next(file for _, file in world.store.written if file.path == path)


def test_targets_later_in_the_archive_resolve_through_their_fact_summaries() -> None:
    # The importers come first in archive order: target facts are read in a pass of
    # their own before any importer is resolved.
    tree: Tree = {
        "cmd/main.go": b'package main\n\nimport "example.com/m/pkg"\n\nfunc main() { pkg.Run() }\n',
        "app.py": b"from pkg import sub\n\n\ndef go():\n    return sub.work()\n",
        "big/main.go": b'package main\n\nimport "example.com/m/huge"\n\nfunc main() { huge.X() }\n',
        "pkg/run.go": b"package pkg\n\nfunc Run() {}\n",
        "pkg/__init__.py": b"",
        "pkg/sub.py": b"def work():\n    return 1\n",
        "huge/x.go": b"package huge\n\nfunc X() {}\n" + b"//" * (128 * 1024 + 1),
        "go.mod": b"module example.com/m\n",
    }
    world = _world(tree)

    world.run()

    main = _file(world, "cmd/main.go")
    assert [(r.specifier, r.resolution, r.target_path) for r in main.imports] == [
        ("example.com/m/pkg", ImportResolution.RESOLVED, "pkg")
    ]
    assert [(e.kind.value, e.target_path, e.target_name) for e in main.edges] == [
        ("imports", "pkg/run.go", ""),
        ("uses_imported", "pkg/run.go", "Run"),
    ]
    app = _file(world, "app.py")
    assert [(e.kind.value, e.target_path, e.target_name) for e in app.edges] == [
        ("imports", "pkg/__init__.py", ""),
        ("imports", "pkg/sub.py", ""),
        ("uses_imported", "pkg/sub.py", "work"),
    ]
    # A member over the parse limit leaves its package unresolved, as in the review.
    big = _file(world, "big/main.go")
    assert [(r.resolution, r.target_path) for r in big.imports] == [
        (ImportResolution.UNSUPPORTED, "huge")
    ]
    assert big.edges == ()


@pytest.mark.parametrize("hidden_parent", [False, True], ids=["hidden-rules", "hidden-directory"])
@pytest.mark.parametrize("incremental", [False, True], ids=["full", "incremental"])
def test_hidden_policy_is_authoritative_at_the_archive_commit(
    hidden_parent: bool, incremental: bool
) -> None:
    rules = b"```review-ignore\napp/views.py\n```\n"
    archive_tree = dict(_TREE) if hidden_parent else {**_TREE, ".review/notes.md": b"notes"}
    policy_tree = {**_TREE, _RULES: rules}
    # Literal SHA-256 fixture of the documented canonical policy payload.
    digest = "3523e722aeb7e7b9613ea2a9d481beea5c11d966e2b92b85bc6df799f65e4d68"
    world = _world(
        archive_tree,
        policy_tree=policy_tree,
        state=_state(profile=replace(_PROFILE, exclusions_digest=digest))
        if incremental
        else _NO_INDEX,
        comparison=_comparison(_changed("app/models.py")) if incremental else None,
        files={"app/models.py": _blob(b"old source")},
    )

    outcome = world.run()

    assert outcome.status is BuildStatus.PUBLISHED
    assert outcome.mode is (BuildMode.INCREMENTAL if incremental else BuildMode.FULL)
    assert world.written() == ["app/models.py"]
    assert world.source.policy_commits == [_HEAD]
    assert world.store.publications[0]["profile"].exclusions_digest == digest


def test_original_policy_blob_overrides_substituted_archive_rules() -> None:
    world = _world(
        {**_TREE, _RULES: b"```review-ignore\napp/models.py\n```\n"},
        policy_tree={_RULES: b"```review-ignore\napp/views.py\n```\n"},
    )

    assert world.run().status is BuildStatus.PUBLISHED
    assert world.written() == ["app/models.py"]


def test_unreadable_hidden_policy_preserves_previous_publication_without_downloading() -> None:
    world = _world(state=_state())
    world.source.errors["rules_document"] = PolicyError(
        PolicyErrorCode.REVISION_UNAVAILABLE, "unreadable"
    )

    outcome = world.run()

    assert (outcome.status, outcome.error_code) == (
        BuildStatus.RELEASED,
        "policy_revision_unavailable",
    )
    assert world.store.publications == []
    assert world.store.written == []
    assert world.store.state is not None
    assert world.store.state.published_publication == 4
    assert world.archives.scans == 0
    assert world.store.release_stats[0] is not None
    assert world.store.release_stats[0].github_request_count == 3
    assert world.store.release_stats[0].downloaded_bytes == 0


def test_truly_missing_rules_use_empty_masks_at_the_pinned_commit() -> None:
    world = _world(policy_tree={})

    assert world.run().status is BuildStatus.PUBLISHED
    assert world.written() == ["app/models.py", "app/views.py"]
    assert world.source.policy_commits == [_HEAD]
    assert world.store.publications[0]["profile"].exclusions_digest == _DIGEST


def test_same_commit_unsafe_v1_publication_requires_a_full_rebuild(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(build_index, "INDEX_EXCLUSIONS_VERSION", "index-exclusions/v1")
    old_limits_profile = limits_profile_version(_LIMITS)
    monkeypatch.setattr(build_index, "INDEX_EXCLUSIONS_VERSION", "index-exclusions/v2")
    world = _world(
        state=_state(
            indexed_commit_sha=_HEAD,
            profile=replace(_PROFILE, limits_profile_version=old_limits_profile),
        )
    )

    outcome = world.run()

    assert (outcome.status, outcome.mode, outcome.full_reason) == (
        BuildStatus.PUBLISHED,
        BuildMode.FULL,
        FullBuildReason.PROFILE_CHANGED,
    )
    assert world.source.policy_commits == [_HEAD]
