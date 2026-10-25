"""Empty additions require durable evidence without fetching their content (#213)."""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, dataclass, field, replace
from uuid import UUID

import pytest

from app.modules.reviews.application.check_runs import RunProviderCheckpoint
from app.modules.reviews.application.get_run_diff import (
    DiffSnapshot,
    GetRunDiff,
    StoreDiffSnapshot,
    is_proven_empty_snapshot,
)
from app.modules.reviews.application.process_run import ReviewRunProcessor, RunVcsInput
from app.modules.reviews.application.prompt_builder import PullRequestMeta
from app.modules.reviews.application.vcs_diff import (
    FetchVcsReviewInput,
    PullRequestLocator,
    VcsFile,
    VcsPullRequest,
    is_proven_empty_addition,
)
from tests.installation_onboarding_fakes import provider_checkpoint
from tests.trigger_uow import processing_uow
from tests.vcs_facts import EMPTY_BLOB_SHA as _EMPTY_BLOB

_HEAD = "a" * 40
_LOCATOR = PullRequestLocator(17, "octo/repo", 7)


@dataclass
class EmptyAdditionProvider:
    files: tuple[VcsFile, ...]
    blob_requests: list[str] = field(default_factory=list)

    async def get_pull_request(
        self, locator: PullRequestLocator, *, checkpoint: RunProviderCheckpoint
    ) -> VcsPullRequest:
        await checkpoint()
        return VcsPullRequest(
            locator,
            _HEAD,
            "b" * 40,
            PullRequestMeta(
                "Add package",
                None,
                "octo",
                "feature",
                "main",
                (),
                len(self.files),
                0,
                0,
                False,
                False,
            ),
        )

    async def get_diff(
        self, pull_request: VcsPullRequest, *, checkpoint: RunProviderCheckpoint
    ) -> tuple[VcsFile, ...]:
        await checkpoint()
        return self.files

    async def get_blob(
        self, locator: PullRequestLocator, sha: str, *, checkpoint: RunProviderCheckpoint
    ) -> bytes:
        await checkpoint()
        self.blob_requests.append(sha)
        return b""


@pytest.mark.parametrize("patch", [None, ""])
def test_fetch_excludes_proven_empty_addition_without_blob_request(patch: str | None) -> None:
    empty = VcsFile(
        "pkg/__init__.py", "added", _EMPTY_BLOB, None, 0, 0, 0, patch, git_mode="100644"
    )
    provider = EmptyAdditionProvider((empty,))

    result = asyncio.run(
        FetchVcsReviewInput(provider, checkpoint=provider_checkpoint).execute(_LOCATOR, _HEAD)
    )

    assert [(item.file.filename, item.reason.value) for item in result.omissions] == [
        ("pkg/__init__.py", "empty_file")
    ]
    assert result.changed_files == ()
    assert result.files == (empty,)
    assert provider.blob_requests == []


@pytest.mark.parametrize(
    "file",
    [
        VcsFile("pkg/__init__.py", "added", None, None, 0, 0, 0, None),
        VcsFile(
            "pkg/__init__.py",
            "added",
            "4b825dc642cb6eb9a060e54bf8d69288fbee4904",
            None,
            0,
            0,
            0,
            None,
        ),  # The empty tree is not the empty blob.
        VcsFile("pkg/__init__.py", "modified", _EMPTY_BLOB, None, 0, 0, 0, None),
        VcsFile("pkg/__init__.py", "renamed", _EMPTY_BLOB, "old.py", 0, 0, 0, None),
        VcsFile("pkg/__init__.py", "added", _EMPTY_BLOB, "old.py", 0, 0, 0, None),
        VcsFile("../escape.py", "added", _EMPTY_BLOB, None, 0, 0, 0, None),
        VcsFile("pkg/__init__.py", "added", _EMPTY_BLOB, None, 1, 0, 0, None),
        VcsFile("pkg/__init__.py", "added", _EMPTY_BLOB, None, 0, 1, 0, None),
        VcsFile("pkg/__init__.py", "added", _EMPTY_BLOB, None, 0, 0, 1, None),
        VcsFile("pkg/__init__.py", "added", _EMPTY_BLOB, None, -1, 0, 0, None),
        VcsFile("pkg/__init__.py", "added", _EMPTY_BLOB, None, 0, 0, 0, None, size=1),
        VcsFile("pkg/__init__.py", "added", _EMPTY_BLOB, None, 0, 0, 0, None, size=-1),
        VcsFile("pkg/__init__.py", "added", _EMPTY_BLOB, None, 0, 0, 0, "garbage"),
        VcsFile("pkg/__init__.py", "added", _EMPTY_BLOB, None, 0, 0, 0, "@@ -0,0 +1 @@\n+x"),
        VcsFile("pkg/__init__.py", "added", _EMPTY_BLOB, None, 0, 0, 0, "@@ -0,0 +1,2 @@\n+x"),
    ],
)
def test_missing_or_contradictory_evidence_never_proves_empty(file: VcsFile) -> None:
    assert is_proven_empty_addition(replace(file, git_mode="100644")) is False


class SnapshotRepository:
    """Round-trip only durable fields through JSON, as a repository port fake."""

    rows = "[]"

    async def store_diff_snapshots(
        self, run_id: UUID, code_change_id: UUID, head_sha: str, snapshots: list[DiffSnapshot]
    ) -> list[DiffSnapshot]:
        self.rows = json.dumps([asdict(item) for item in snapshots])
        return [DiffSnapshot(**item) for item in json.loads(self.rows)]

    async def get_run_diff(self, run_id: UUID) -> list[DiffSnapshot]:
        return [DiffSnapshot(**item) for item in json.loads(self.rows)]


@pytest.mark.parametrize("patch", [None, ""])
def test_empty_proof_survives_fetch_snapshot_storage_and_reload(patch: str | None) -> None:
    async def scenario() -> None:
        provider = EmptyAdditionProvider(
            (
                VcsFile(
                    "pkg/__init__.py",
                    "added",
                    _EMPTY_BLOB,
                    None,
                    0,
                    0,
                    0,
                    patch,
                    size=0,
                    git_mode="100644",
                ),
            )
        )
        fetched = await FetchVcsReviewInput(provider, checkpoint=provider_checkpoint).execute(
            _LOCATOR, _HEAD
        )
        repository = SnapshotRepository()
        run_id = UUID(int=1)
        await StoreDiffSnapshot(repository).execute(
            run_id=run_id,
            code_change_id=UUID(int=2),
            head_sha=_HEAD,
            files=[
                DiffSnapshot(
                    filename=item.file.filename,
                    patch=item.file.patch,
                    blob_sha=item.file.blob_sha,
                    status=item.file.status,
                    additions=item.file.additions,
                    deletions=item.file.deletions,
                    changes=item.file.changes,
                    omission_reason=item.reason.value,
                )
                for item in fetched.omissions
            ],
        )
        reloaded = await GetRunDiff(repository).execute(run_id)

        assert reloaded is not None and len(reloaded) == 1
        assert reloaded[0].blob_sha == _EMPTY_BLOB
        assert reloaded[0].omission_reason == "empty_file"
        assert reloaded[0].review_patch is None
        assert is_proven_empty_snapshot(reloaded[0]) is True
        assert provider.blob_requests == []

    asyncio.run(scenario())


def test_snapshot_does_not_erase_conflicting_fetched_size() -> None:
    async def scenario() -> None:
        provider = EmptyAdditionProvider(
            (
                VcsFile(
                    "pkg/__init__.py",
                    "added",
                    _EMPTY_BLOB,
                    None,
                    0,
                    0,
                    0,
                    None,
                    size=1,
                    git_mode="100644",
                ),
            )
        )
        fetched = await FetchVcsReviewInput(provider, checkpoint=provider_checkpoint).execute(
            _LOCATOR, _HEAD
        )
        snapshot = DiffSnapshot(
            "pkg/__init__.py",
            None,
            blob_sha=_EMPTY_BLOB,
            status="added",
            omission_reason=fetched.omissions[0].reason.value,
        )
        assert snapshot.omission_reason == "missing_patch"
        assert is_proven_empty_snapshot(snapshot) is False
        assert is_proven_empty_snapshot(replace(snapshot, omission_reason=None)) is False

    asyncio.run(scenario())


def test_empty_blob_without_regular_file_mode_never_proves_empty() -> None:
    file = VcsFile("pkg/__init__.py", "added", _EMPTY_BLOB, None, 0, 0, 0, None)
    assert is_proven_empty_addition(file) is False


@pytest.mark.parametrize("mode", ["120000", "160000", "040000", "100664", ""])
def test_symlink_gitlink_and_unknown_modes_never_prove_empty(mode: str) -> None:
    file = VcsFile("pkg/__init__.py", "added", _EMPTY_BLOB, None, 0, 0, 0, None, git_mode=mode)
    assert is_proven_empty_addition(file) is False


def test_executable_regular_empty_file_is_also_excluded() -> None:
    file = VcsFile("pkg/run", "added", _EMPTY_BLOB, None, 0, 0, 0, None, git_mode="100755")
    assert is_proven_empty_addition(file) is True


@pytest.mark.parametrize(
    "snapshot",
    [
        DiffSnapshot(
            "pkg/__init__.py",
            "garbage",
            blob_sha=_EMPTY_BLOB,
            status="added",
            omission_reason="empty_file",
        ),
        DiffSnapshot(
            "pkg/__init__.py",
            "diff --git a/x b/x\nold mode 100644\nnew mode 120000\n",
            blob_sha=_EMPTY_BLOB,
            status="added",
            omission_reason="empty_file",
        ),
        DiffSnapshot(
            "pkg/__init__.py",
            "@@ -0,0 +1 @@\n+x",
            blob_sha=_EMPTY_BLOB,
            status="added",
            omission_reason="empty_file",
        ),
        DiffSnapshot(
            "pkg/__init__.py",
            None,
            blob_sha=_EMPTY_BLOB,
            status="added",
            omission_reason="empty_file",
            summary_only=True,
        ),
        DiffSnapshot(
            "pkg/__init__.py",
            None,
            blob_sha=_EMPTY_BLOB,
            status="added",
            omission_reason="empty_file",
            review_patch="@@ -0,0 +1 @@\n+x",
        ),
        DiffSnapshot(
            "pkg/__init__.py",
            None,
            blob_sha=_EMPTY_BLOB,
            status="added",
            omission_reason="empty_file",
            additions=1,
        ),
        DiffSnapshot(
            "pkg/__init__.py", None, blob_sha=None, status="added", omission_reason="empty_file"
        ),
        DiffSnapshot(
            "pkg/__init__.py",
            None,
            blob_sha=_EMPTY_BLOB,
            status="modified",
            omission_reason="empty_file",
        ),
        DiffSnapshot(
            "../escape.py", None, blob_sha=_EMPTY_BLOB, status="added", omission_reason="empty_file"
        ),
    ],
)
def test_persisted_marker_does_not_override_conflicting_snapshot_evidence(
    snapshot: DiffSnapshot,
) -> None:
    assert is_proven_empty_snapshot(snapshot) is False


def test_processor_does_not_fetch_empty_blob_for_optional_cache_on_first_run_or_retry() -> None:
    class Repository(SnapshotRepository):
        async def get_run_vcs_input(self, run_id: UUID) -> RunVcsInput:
            return RunVcsInput(UUID(int=2), UUID(int=3), _HEAD, "b" * 40, _LOCATOR)

        async def get_run_snapshots(self, run_id: UUID) -> list[DiffSnapshot] | None:
            return None if self.rows == "[]" else await self.get_run_diff(run_id)

        async def get_run_diff_input(self, run_id: UUID) -> None:
            raise AssertionError("only the VCS path is used")

    async def scenario() -> None:
        provider = EmptyAdditionProvider(
            (
                VcsFile(
                    "pkg/__init__.py", "added", _EMPTY_BLOB, None, 0, 0, 0, None, git_mode="100644"
                ),
            )
        )
        repository = Repository()
        processor = ReviewRunProcessor(
            repository, None, vcs_provider=provider, uow_factory=processing_uow(repository)
        )

        assert await processor.execute(UUID(int=1)) is True
        assert await processor.execute(UUID(int=1)) is True
        assert provider.blob_requests == []

    asyncio.run(scenario())


@pytest.mark.parametrize("collapsed", [False, True])
def test_gitlab_empty_added_blob_without_regular_file_proof_stays_missing_patch(
    collapsed: bool,
) -> None:
    import httpx

    from app.modules.integrations.gitlab.infrastructure.run_http import GitLabRunHttp
    from app.modules.reviews.infrastructure.gitlab_vcs import HttpGitLabVcsProvider
    from tests.test_gitlab_run_http import INPUT, POLICY, Credentials, Stream
    from tests.test_gitlab_vcs import Router

    async def scenario() -> None:
        router = Router()
        router.canonical["changes_count"] = "1"
        router.files = [
            {
                "old_path": "pkg/__init__.py",
                "new_path": "pkg/__init__.py",
                "new_file": True,
                "deleted_file": False,
                "renamed_file": False,
                "diff": "",
                "collapsed": collapsed,
            }
        ]

        def respond(request: httpx.Request) -> httpx.Response:
            if "/repository/files/" in request.url.path:
                router.requests.append(request)
                return httpx.Response(
                    200,
                    headers={
                        "X-Gitlab-Blob-Id": _EMPTY_BLOB,
                        "X-Gitlab-Size": "0",
                        "X-Gitlab-Ref": INPUT.head_sha,
                    },
                    stream=Stream((b"",)),
                )
            if "/repository/blobs/" in request.url.path:
                router.requests.append(request)
                return httpx.Response(200, stream=Stream((b"",)))
            return router.response(request)

        async with GitLabRunHttp(
            credentials=Credentials(),
            origin_policy=POLICY,
            transport=httpx.MockTransport(respond),
        ) as http:
            result = await FetchVcsReviewInput(
                HttpGitLabVcsProvider(http=http),
                checkpoint=provider_checkpoint,
            ).execute(INPUT.locator, INPUT.head_sha, INPUT.base_sha)
        assert result.changed_files == ()
        assert [(item.file.filename, item.reason.value) for item in result.omissions] == [
            ("pkg/__init__.py", "missing_patch")
        ]
        assert result.files[0].blob_sha == _EMPTY_BLOB
        assert result.files[0].size == 0
        assert result.files[0].git_mode is None
        assert all(request.url.host == "gitlab.com" for request in router.requests)
        assert not any("/git/trees/" in request.url.path for request in router.requests)
        blobs = [r for r in router.requests if "/repository/blobs/" in r.url.path]
        assert len(blobs) == (0 if collapsed else 1)

    asyncio.run(scenario())
