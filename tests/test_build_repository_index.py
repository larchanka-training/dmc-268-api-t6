"""Use case `BuildRepositoryIndex` against fakes of its ports (#142).

AC5: every mode decision. AC7: no transaction is open during a VCS call. AC8: every code,
and an empty repository without an error. AC12: a CAS miss, a lost lease, the soft cutoff
and a target change. AC3: the skip counters equal literals.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping, Set
from dataclasses import dataclass, field, replace
from typing import cast
from uuid import UUID

import httpx
import pytest

from app.modules.code_index.application import build_index
from app.modules.code_index.application.build_index import (
    BuildMode,
    BuildStatus,
    FullBuildReason,
    limits_profile_version,
    parser_profile_version,
)
from app.modules.code_index.application.exclusions import ExclusionClass
from app.modules.code_index.application.index_store import (
    ClaimRefusal,
    ClaimRefused,
    ImportRecord,
    IndexBuildStats,
    IndexState,
)
from app.modules.code_index.application.vcs import (
    Ancestry,
    ArchiveEntry,
    ArchiveScan,
    ChangedPath,
    ChangeStatus,
    Comparison,
    SourceArchive,
)
from app.modules.code_index.infrastructure.bitbucket_snapshot_source import (
    BitbucketRepositorySnapshotSource,
)
from app.modules.code_index.infrastructure.tar_archive import TarArchiveReader
from app.modules.integrations.bitbucket.application.request_credentials import (
    BitbucketRequestCredential,
    BoundBitbucketTarget,
)
from app.modules.reviews.application.ast_resolution import ImportResolution
from app.modules.reviews.application.review_ignore import (
    IgnoreMatcher,
    IgnorePolicy,
)
from tests.build_repository_index_fakes import (
    _ADMISSION,
    _CLI,
    _DIGEST,
    _GENERATION,
    _HEAD,
    _HEAD_TREE,
    _INCREMENTAL,
    _LIMITS,
    _NO_INDEX,
    _OLD,
    _OLD_IMPORTS,
    _OLD_TREE,
    _PROFILE,
    _REPO,
    _SETTINGS,
    _TREE,
    _VERSIONS,
    _VIEWS,
    Archives,
    Link,
    Repositories,
    Source,
    Tree,
    _blob,
    _changed,
    _comparison,
    _incremental_world,
    _indexed,
    _state,
    _world,
)


def test_the_profile_versions_fit_their_columns_and_follow_their_inputs() -> None:
    limits = limits_profile_version(_LIMITS)
    assert limits.startswith("limits/v1:")
    assert len(limits) <= 64
    # Only the limits that change content are in the digest.
    assert limits_profile_version(replace(_LIMITS, parsed_files=1)) == limits
    assert limits_profile_version(replace(_LIMITS, parse_bytes=1)) != limits
    assert limits_profile_version(replace(_LIMITS, read_bytes=1)) != limits
    parser = parser_profile_version(["python:1", "go:2"])
    assert parser == parser_profile_version(["go:2", "python:1"])
    assert len(parser) == 64
    assert parser != parser_profile_version(["python:1", "go:3"])
    assert _PROFILE.default_branch == "main"
    assert _PROFILE.exclusions_digest == _DIGEST


def test_a_missing_repository_is_stale_without_a_claim() -> None:
    world = _world()
    world.repositories = Repositories(known=False)

    assert world.run().status is BuildStatus.STALE
    assert world.tracker.log == []


@pytest.mark.parametrize(
    ("refusal", "status", "code"),
    [
        (ClaimRefused(ClaimRefusal.STALE), BuildStatus.STALE, None),
        (ClaimRefused(ClaimRefusal.LEASED), BuildStatus.LEASED, None),
        (ClaimRefused(ClaimRefusal.BUSY), BuildStatus.BUSY, None),
        (
            ClaimRefused(ClaimRefusal.QUOTA, "limit_exceeded:workspace_storage"),
            BuildStatus.FAILED,
            "limit_exceeded:workspace_storage",
        ),
    ],
)
def test_a_refused_claim_ends_the_job_after_committing_the_claim(
    refusal: ClaimRefused, status: BuildStatus, code: str | None
) -> None:
    world = _world(claim=refusal)

    outcome = world.run()

    assert (outcome.status, outcome.error_code) == (status, code)
    # A QUOTA refusal wrote `failed`: the refusal is committed too.
    assert world.tracker.log == ["claim_lease", "commit"]
    assert world.store.admission == _ADMISSION
    assert world.sources == []


def test_a_failing_source_factory_after_the_claim_releases_the_lease() -> None:
    world = _world()
    world.source_error = RuntimeError("no source for this repository")

    with pytest.raises(RuntimeError, match="no source"):
        world.run()

    assert world.store.releases == ["internal_error"]
    assert world.tracker.log == ["claim_lease", "commit", "release", "commit"]


def test_a_full_build_writes_every_admitted_file_in_batches_and_publishes_from_the_base() -> None:
    tree = {**_TREE, "app/cli.py": _CLI}
    world = _world(tree, state=_state(indexed_commit_sha=None, profile=None))

    outcome = world.run(settings=replace(_SETTINGS, batch_files=2))

    assert outcome.status is BuildStatus.PUBLISHED
    assert (outcome.mode, outcome.full_reason) == (BuildMode.FULL, FullBuildReason.NO_INDEX)
    assert outcome.commit_sha == _HEAD
    # Two files per transaction, each batch its own transaction.
    batches: dict[int, list[str]] = {}
    for transaction, file in world.store.written:
        batches.setdefault(transaction, []).append(file.path)
    assert list(batches.values()) == [["app/models.py", "app/views.py"], ["app/cli.py"]]
    views = next(file for _, file in world.store.written if file.path == "app/views.py")
    assert views.blob_sha == _blob(_VIEWS)
    assert [(record.specifier, record.target_path) for record in views.imports] == [
        ("app.models", "app/models.py")
    ]
    (publication,) = world.store.publications
    assert publication == {
        "base_commit_sha": None,
        "commit_sha": _HEAD,
        "full": True,
        "deleted_paths": (),
        "profile": _VERSIONS.profile("main", _DIGEST),
        "stats": IndexBuildStats(len(b"archive"), publication["stats"].build_duration_ms, 3),
    }
    assert world.network() == ["head", "archive"]
    assert (outcome.parsed_files, outcome.written_files, outcome.deleted_files) == (3, 3, 0)
    assert (outcome.github_request_count, outcome.downloaded_bytes) == (3, 7)
    assert world.wakeups.woken == []


def test_a_full_build_over_a_published_index_passes_the_indexed_commit_as_its_base() -> None:
    world = _world(state=_state(profile=replace(_PROFILE, extractor_version="facts/v0")))

    outcome = world.run()

    assert (outcome.mode, outcome.full_reason) == (BuildMode.FULL, FullBuildReason.PROFILE_CHANGED)
    assert world.store.publications[0]["base_commit_sha"] == _OLD
    assert world.network() == ["head", "archive"]


def _go_mod(
    path: str = "go.mod",
    status: ChangeStatus = ChangeStatus.MODIFIED,
    previous_path: str | None = None,
) -> ChangedPath:
    return ChangedPath(path, status, previous_path)


@pytest.mark.parametrize(
    ("state", "comparison", "reason", "compared"),
    [
        pytest.param(_NO_INDEX, None, FullBuildReason.NO_INDEX, False, id="no-index"),
        pytest.param(
            _state(profile=replace(_PROFILE, resolver_version="resolver/v0")),
            None,
            FullBuildReason.PROFILE_CHANGED,
            False,
            id="resolver-version",
        ),
        pytest.param(
            _state(profile=replace(_PROFILE, limits_profile_version="limits/v0:0")),
            None,
            FullBuildReason.PROFILE_CHANGED,
            False,
            id="limits-version",
        ),
        pytest.param(
            _state(profile=replace(_PROFILE, default_branch="master")),
            None,
            FullBuildReason.BRANCH_CHANGED,
            False,
            id="default-branch",
        ),
        pytest.param(_state(), None, FullBuildReason.BASE_UNKNOWN, True, id="base-unknown"),
        pytest.param(
            _state(),
            _comparison(ancestry=Ancestry.DIVERGED),
            FullBuildReason.NOT_AHEAD,
            True,
            id="diverged",
        ),
        pytest.param(
            _state(),
            _comparison(ancestry=Ancestry.BEHIND),
            FullBuildReason.NOT_AHEAD,
            True,
            id="behind",
        ),
        pytest.param(
            _state(),
            Comparison(Ancestry.AHEAD, (_changed("app/models.py"),), complete=False),
            FullBuildReason.COMPARE_INCOMPLETE,
            True,
            id="incomplete",
        ),
        pytest.param(
            _state(),
            Comparison(None, (), complete=False),
            FullBuildReason.COMPARE_INCOMPLETE,
            True,
            id="unread-compare",
        ),
        pytest.param(
            _state(),
            _comparison(_go_mod()),
            FullBuildReason.GO_MODULE_CHANGED,
            True,
            id="go-mod",
        ),
        pytest.param(
            _state(),
            _comparison(_go_mod("tools/go.mod")),
            FullBuildReason.GO_MODULE_CHANGED,
            True,
            id="nested-go-mod",
        ),
        pytest.param(
            _state(),
            _comparison(_go_mod("go.work", status=ChangeStatus.ADDED)),
            FullBuildReason.GO_MODULE_CHANGED,
            True,
            id="go-work",
        ),
        pytest.param(
            _state(),
            _comparison(
                _go_mod("build/go.mod.txt", status=ChangeStatus.RENAMED, previous_path="go.mod")
            ),
            FullBuildReason.GO_MODULE_CHANGED,
            True,
            id="go-mod-renamed-away",
        ),
        pytest.param(
            _state(profile=replace(_PROFILE, exclusions_digest="0" * 64)),
            _comparison(_changed("app/models.py")),
            FullBuildReason.EXCLUSIONS_CHANGED,
            True,
            id="ignore-policy",
        ),
    ],
)
def test_every_full_build_decision(
    state: IndexState, comparison: Comparison | None, reason: FullBuildReason, compared: bool
) -> None:
    world = _world(state=state, comparison=comparison, files=_indexed(_TREE))

    outcome = world.run()

    assert (outcome.status, outcome.mode, outcome.full_reason) == (
        BuildStatus.PUBLISHED,
        BuildMode.FULL,
        reason,
    )
    assert world.network() == (["head", "compare", "archive"] if compared else ["head", "archive"])
    assert world.store.publications[0]["full"] is True
    assert sorted(world.written()) == ["app/models.py", "app/views.py"]
    # A full build reads no stored rows.
    assert "iter_files" not in world.tracker.log


def test_an_incremental_build_rewrites_the_changes_and_their_importers_only() -> None:
    world = _incremental_world()

    outcome = world.run()

    assert (outcome.status, outcome.mode, outcome.full_reason) == (
        BuildStatus.PUBLISHED,
        BuildMode.INCREMENTAL,
        None,
    )
    # `app/models.py` changed; `app/cli.py` imports the removed `app/util.py` (rule W).
    assert sorted(world.written()) == ["app/cli.py", "app/models.py"]
    (publication,) = world.store.publications
    assert publication["base_commit_sha"] == _OLD
    assert publication["full"] is False
    assert publication["deleted_paths"] == ("app/util.py",)
    cli = next(file for _, file in world.store.written if file.path == "app/cli.py")
    assert [(record.specifier, record.resolution) for record in cli.imports] == [
        ("app.util", ImportResolution.EXTERNAL)
    ]
    assert world.network() == ["head", "compare", "archive"]
    assert (outcome.written_files, outcome.deleted_files) == (2, 1)


def test_sparse_incremental_build_does_not_fetch_unchanged_100_file_archive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created_matchers: list[IgnoreMatcher] = []

    def tracked_matcher(policy: IgnorePolicy) -> IgnoreMatcher:
        matcher = IgnoreMatcher(policy)
        created_matchers.append(matcher)
        return matcher

    monkeypatch.setattr(build_index, "IgnoreMatcher", tracked_matcher)
    tree: Tree = {
        **{f"f{i:03}.py": b"value = 1\n" for i in range(97)},
        "pkg/__init__.py": b"value = 1\n",
        "consumer.py": b"from pkg import value\n",
        "secret.py": b"private = 1\n",
    }
    old_tree: Tree = {**tree, "pkg/__init__.py": b"value = 0\n"}
    old_tree = {path: value for path, value in old_tree.items() if path != "secret.py"}
    comparison = _comparison(_changed("pkg/__init__.py"), _changed("secret.py", ChangeStatus.ADDED))
    world = _world(
        tree,
        state=_state(file_count=99),
        comparison=comparison,
        files=_indexed(old_tree),
        imports=[ImportRecord("consumer.py", "pkg", ImportResolution.RESOLVED, "pkg/__init__.py")],
    )

    @dataclass
    class SparseSource(Source):
        tree: Tree = field(default_factory=dict)
        selected: list[frozenset[str]] = field(default_factory=list)

        async def manifest(self, commit_sha: str) -> Mapping[str, ArchiveEntry]:
            assert commit_sha == _HEAD
            await self._call("manifest")
            return {
                path: ArchiveEntry(
                    path,
                    len(value) if isinstance(value, bytes) else 0,
                    symlink=isinstance(value, Link),
                )
                for path, value in self.tree.items()
            }

        async def archive_paths(
            self, commit_sha: str, paths: Set[str], *, max_bytes: int
        ) -> SourceArchive:
            assert commit_sha == _HEAD and max_bytes == _LIMITS.download_bytes
            await self._call("archive_paths")
            self.selected.append(frozenset(paths))
            return SourceArchive(commit_sha, "\n".join(sorted(paths)).encode())

        async def archive(self, commit_sha: str, *, max_bytes: int) -> SourceArchive:
            raise AssertionError("full archive must not be fetched for a proven one-file change")

    @dataclass
    class SparseArchives(Archives):
        async def scan(
            self, archive: SourceArchive, *, documents: Set[str], deadline: float
        ) -> ArchiveScan:
            complete = await super().scan(archive, documents=documents, deadline=deadline)
            selected = set(archive.content.decode().splitlines())
            return replace(
                complete,
                entries={
                    path: entry for path, entry in complete.entries.items() if path in selected
                },
                documents={
                    path: body for path, body in complete.documents.items() if path in selected
                },
            )

        async def contents(
            self, archive: SourceArchive, paths: Set[str], *, deadline: float, batch_files: int
        ) -> AsyncIterator[tuple[tuple[str, bytes], ...]]:
            selected = set(archive.content.decode().splitlines())
            async for batch in super().contents(
                archive, paths & selected, deadline=deadline, batch_files=batch_files
            ):
                yield batch

    world.source = SparseSource(world.tracker, comparison=comparison, policy_tree=tree, tree=tree)
    world.archives = SparseArchives(tree)

    outcome = world.run()

    assert outcome.status is BuildStatus.PUBLISHED
    assert outcome.mode is BuildMode.INCREMENTAL
    assert sorted(world.written()) == ["consumer.py", "pkg/__init__.py"]
    assert world.source.selected == [
        frozenset(),
        frozenset({"consumer.py", "pkg/__init__.py"}),
    ]
    assert outcome.exclusions[ExclusionClass.SECRET] == 1
    assert len(created_matchers) == 1
    assert world.store.publications[0]["full"] is False


@pytest.mark.parametrize("empty_diffstat", [False, True])
def test_bitbucket_201_file_push_uses_sparse_or_full_rebuild_for_diffstat(
    empty_diffstat: bool,
) -> None:
    files = {f"f{i:03}.py": b"value = 1\n" for i in range(201)}
    target = BoundBitbucketTarget(UUID(int=1), _REPO, UUID(int=2), UUID(int=3), 1, 1)
    head_sha = _OLD
    listing_requests: list[str] = []
    raw_reads: list[str] = []
    metadata_reads: list[str] = []

    class Published:
        manifest: dict[str, ArchiveEntry] | None = None
        sha: str | None = None

        async def load(
            self, bound: BoundBitbucketTarget, base_sha: str
        ) -> Mapping[str, ArchiveEntry] | None:
            assert bound == target
            return self.manifest if self.sha == base_sha else None

        async def save(
            self,
            bound: BoundBitbucketTarget,
            commit_sha: str,
            entries: Mapping[str, ArchiveEntry],
        ) -> bool:
            assert bound == target
            self.sha = commit_sha
            self.manifest = dict(entries)
            return True

    published = Published()

    class Credentials:
        async def credential_for(self, bound: BoundBitbucketTarget) -> BitbucketRequestCredential:
            assert bound == target
            return BitbucketRequestCredential(bound, "test-token")

        async def checkpoint(self, bound: BoundBitbucketTarget) -> None:
            assert bound == target

    async def resolve_target() -> BoundBitbucketTarget:
        return target

    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        assert request.url.host == "api.bitbucket.org"
        if path.endswith(f"{{{target.repository_uuid}}}/"):
            return httpx.Response(
                200,
                json={
                    "uuid": "{" + str(target.repository_uuid) + "}",
                    "mainbranch": {"name": "main"},
                },
            )
        if path.endswith("/refs/branches/main"):
            return httpx.Response(200, json={"target": {"hash": head_sha}})
        if path.endswith(f"/merge-base/{_OLD}..{_HEAD}"):
            return httpx.Response(200, json={"hash": _OLD})
        if path.endswith(f"/diffstat/{_HEAD}..{_OLD}"):
            return httpx.Response(
                200,
                json={
                    "size": 0 if empty_diffstat else 1,
                    "values": []
                    if empty_diffstat
                    else [
                        {
                            "status": "modified",
                            "old": {"path": "f000.py"},
                            "new": {"path": "f000.py"},
                        }
                    ],
                },
            )
        if path.endswith(f"/src/{head_sha}/"):
            listing_requests.append(path)
            page = int(request.url.params.get("page", "1"))
            names = sorted(files)[(page - 1) * 100 : page * 100]
            payload: dict[str, object] = {
                "size": 201,
                "values": [
                    {
                        "path": name,
                        "type": "commit_file",
                        "size": len(files[name]),
                        "commit": {"hash": head_sha},
                    }
                    for name in names
                ],
            }
            if page < 3:
                payload["next"] = f"https://api.bitbucket.org{path}?pagelen=100&page={page + 1}"
            return httpx.Response(200, json=payload)
        if request.url.params.get("format") == "meta":
            name = path.rsplit("/", 1)[-1]
            metadata_reads.append(name)
            return httpx.Response(
                200,
                json={
                    "path": name,
                    "type": "commit_file",
                    "size": len(files[name]),
                    "commit": {"hash": head_sha},
                    "attributes": [],
                },
            )
        if f"/src/{head_sha}/" in path:
            name = path.rsplit("/", 1)[-1]
            raw_reads.append(name)
            return httpx.Response(200, content=files[name])
        raise AssertionError(path)

    async def scenario() -> None:
        nonlocal head_sha
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            world = _world(files)
            world.archives = cast(Archives, TarArchiveReader(_LIMITS))

            def source() -> BitbucketRepositorySnapshotSource:
                return BitbucketRepositorySnapshotSource(
                    client=client,
                    target_for_repository=resolve_target,
                    credentials=Credentials(),
                    limits=_LIMITS,
                    published_manifest=published,
                )

            world.source = cast(Source, source())
            full = await world.use_case().execute(_REPO, _GENERATION)
            assert full.status is BuildStatus.PUBLISHED
            assert full.mode is BuildMode.FULL
            assert full.written_files == 201
            assert (
                full.listing_request_count,
                full.metadata_request_count,
                full.raw_read_count,
            ) == (3, 2, 201)
            assert (len(listing_requests), len(raw_reads)) == (3, 201)
            assert published.sha == _OLD
            assert published.manifest is not None and len(published.manifest) == 201

            world.store.files = {file.path: file.blob_sha for _, file in world.store.written}
            world.store.state = _state(indexed_commit_sha=_OLD, file_count=201)
            world.store.written.clear()
            world.store.publications.clear()
            files["f000.py"] = b"value = 2\n"
            head_sha = _HEAD
            listing_requests.clear()
            raw_reads.clear()
            metadata_reads.clear()
            world.source = cast(Source, source())

            update = await world.use_case().execute(_REPO, _GENERATION)
            assert update.status is BuildStatus.PUBLISHED
            if empty_diffstat:
                assert update.mode is BuildMode.FULL
                assert update.written_files == 201
                assert len(raw_reads) == 201
                changed = next(file for _, file in world.store.written if file.path == "f000.py")
                assert changed.blob_sha == _blob(b"value = 2\n")
                assert world.store.publications[0]["full"] is True
            else:
                assert update.mode is BuildMode.INCREMENTAL
                assert (update.written_files, update.deleted_files) == (1, 0)
                assert (
                    update.listing_request_count,
                    update.metadata_request_count,
                    update.raw_read_count,
                ) == (0, 5, 1)
                assert (len(listing_requests), metadata_reads, raw_reads) == (
                    0,
                    ["f000.py"],
                    ["f000.py"],
                )
                assert world.written() == ["f000.py"]
                assert world.store.publications[0]["full"] is False
            assert world.store.publications[0]["deleted_paths"] == ()

    asyncio.run(scenario())


def test_an_incremental_build_also_rewrites_a_stored_file_whose_blob_differs() -> None:
    stored = {**_indexed(_HEAD_TREE), "app/views.py": "f" * 40}
    world = _world(
        _HEAD_TREE,
        state=_state(),
        comparison=_comparison(_changed("app/models.py")),
        files=stored,
        imports=list(_OLD_IMPORTS[:1]),
    )

    outcome = world.run()

    assert outcome.mode is BuildMode.INCREMENTAL
    assert sorted(world.written()) == ["app/models.py", "app/views.py"]


def test_a_stored_file_that_became_a_symlink_loses_its_rows_and_wakes_its_importers() -> None:
    tree: Tree = {**_HEAD_TREE, "app/util.py": Link("cli.py")}
    world = _world(
        tree,
        state=_state(),
        comparison=_comparison(),
        files=_indexed(_OLD_TREE),
        imports=list(_OLD_IMPORTS),
    )

    outcome = world.run()

    assert outcome.mode is BuildMode.INCREMENTAL
    # `app/models.py` differs from its stored blob; `app/cli.py` imports the new symlink.
    assert sorted(world.written()) == ["app/cli.py", "app/models.py"]
    assert world.store.publications[0]["deleted_paths"] == ("app/util.py",)
    cli = next(file for _, file in world.store.written if file.path == "app/cli.py")
    assert [(record.resolution, record.target_path) for record in cli.imports] == [
        (ImportResolution.EXCLUDED, None)
    ]
    assert outcome.exclusions[ExclusionClass.SYMLINK] == 1


@pytest.mark.parametrize(
    ("limit", "comparison"),
    [
        pytest.param(1, _INCREMENTAL, id="changed-files"),
        # One change, but its importer pushes the set over the limit.
        pytest.param(
            2,
            _comparison(_changed("app/util.py", ChangeStatus.REMOVED), _changed("app/models.py")),
            id="importers",
        ),
    ],
)
def test_too_many_changes_make_the_build_full(limit: int, comparison: Comparison) -> None:
    world = _incremental_world()
    world.source.comparison = comparison

    outcome = world.run(replace(_LIMITS, incremental_files=limit))

    assert (outcome.mode, outcome.full_reason) == (
        BuildMode.FULL,
        FullBuildReason.TOO_MANY_CHANGES,
    )
    assert world.store.publications[0]["full"] is True
    assert sorted(world.written()) == ["app/cli.py", "app/models.py", "app/views.py"]


def test_head_equal_to_the_indexed_commit_with_the_same_profile_releases_unchanged() -> None:
    world = _world(state=_state(indexed_commit_sha=_HEAD))

    outcome = world.run()

    assert (outcome.status, outcome.commit_sha) == (BuildStatus.UNCHANGED, _HEAD)
    assert world.store.releases == [None]
    assert world.network() == ["head"]
    assert world.archives.scans == 0


def test_head_equal_to_the_indexed_commit_with_another_profile_builds_full() -> None:
    world = _world(
        state=_state(indexed_commit_sha=_HEAD, profile=replace(_PROFILE, default_branch="dev"))
    )

    outcome = world.run()

    assert (outcome.mode, outcome.full_reason) == (BuildMode.FULL, FullBuildReason.BRANCH_CHANGED)


@pytest.mark.parametrize(
    "version",
    ["extractor_version", "resolver_version", "parser_profile_version", "limits_profile_version"],
)
def test_head_equal_to_the_indexed_commit_built_with_other_versions_builds_full(
    version: str,
) -> None:
    previous = "resolver/v1+index/v1" if version == "resolver_version" else "other"
    profile = replace(_PROFILE, **{version: previous})
    world = _world(state=_state(indexed_commit_sha=_HEAD, profile=profile))

    outcome = world.run()

    assert (outcome.status, outcome.mode, outcome.full_reason) == (
        BuildStatus.PUBLISHED,
        BuildMode.FULL,
        FullBuildReason.PROFILE_CHANGED,
    )
    assert world.network() == ["head", "archive"]
    assert world.store.publications[0]["base_commit_sha"] == _HEAD


def test_an_empty_repository_releases_as_empty_without_an_error() -> None:
    world = _world(state=_state())
    world.source.head_value = None

    outcome = world.run()

    assert (outcome.status, outcome.error_code) == (BuildStatus.EMPTY, None)
    assert world.store.releases == [None]
    assert world.store.failures == []
    assert world.network() == ["head"]


def test_abandoned_rows_are_discarded_in_portions_before_any_vcs_call() -> None:
    world = _world(discards=[2, 2, 1])

    outcome = world.run(settings=replace(_SETTINGS, discard_batch_rows=2))

    assert outcome.status is BuildStatus.PUBLISHED
    assert world.tracker.log[:10] == [
        "claim_lease",
        "commit",
        "discard_abandoned",
        "commit",
        "discard_abandoned",
        "commit",
        "discard_abandoned",
        "commit",
        "head",
        "get_state",
    ]


def test_discarding_past_the_soft_cutoff_releases_and_wakes_the_repository() -> None:
    world = _world(discards=[2])

    outcome = world.run(settings=replace(_SETTINGS, discard_batch_rows=2, soft_cutoff_seconds=0))

    assert outcome.status is BuildStatus.REWAKE
    assert world.store.releases == [None]
    assert world.store.failures == []
    assert world.wakeups.woken == [(_REPO, _GENERATION)]
    assert world.network() == []


def test_a_publication_miss_wakes_the_repository() -> None:
    world = _world(published=False)

    outcome = world.run()

    assert outcome.status is BuildStatus.REWAKE
    assert world.wakeups.woken == [(_REPO, _GENERATION)]
    assert world.store.releases == []


# The target of the claim snapshot, the target on the state row at the end, and whether
# it moved: only a row that differs from the snapshot wakes the repository.
