"""The run worker persists all provider files but prompts only on reviewable hunks."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import cast
from uuid import UUID

import pytest

from app.modules.reviews.application.conventions import (
    ActiveConventionsPrompt,
    CachedConventions,
    GeneratedConventions,
    GenerateRepoConventions,
)
from app.modules.reviews.application.get_run_diff import DiffSnapshot, review_files_from_snapshots
from app.modules.reviews.application.get_run_file_lines import BlobCacheKey, GetRunFileLines
from app.modules.reviews.application.process_run import (
    ReviewRunProcessor,
    RunConventionsInput,
    RunVcsInput,
)
from app.modules.reviews.application.prompt_builder import DiffLine, PullRequestMeta
from app.modules.reviews.application.vcs_diff import (
    PullRequestLocator,
    VcsFile,
    VcsPullRequest,
)
from app.modules.reviews.infrastructure.blob_cache import InMemoryBlobCache

RUN_ID = UUID("00000000-0000-0000-0000-000000000101")
CODE_CHANGE_ID = UUID("00000000-0000-0000-0000-000000000102")
REPOSITORY_ID = UUID("00000000-0000-0000-0000-000000000103")
HEAD = "a" * 40
BASE = "b" * 40
LOCATOR = PullRequestLocator(17, "octo/repo", 7)


def test_worker_snapshot_preserves_every_file_and_filters_model_input() -> None:
    files = (
        VcsFile(
            "src/new.py", "renamed", "1" * 40, "src/old.py", 1, 1, 2, "@@ -1 +1 @@\n-old\n+new"
        ),
        VcsFile(
            "package-lock.json", "modified", "2" * 40, None, 1, 0, 1, "@@ -0,0 +1 @@\n+generated"
        ),
        VcsFile("assets/logo.png", "modified", "3" * 40, None, 1, 1, 2, None),
    )

    class GitHub:
        async def get_pull_request(self, locator: PullRequestLocator) -> VcsPullRequest:
            assert locator == LOCATOR
            return VcsPullRequest(
                locator,
                HEAD,
                BASE,
                PullRequestMeta("PR", None, "octo", "feature", "main", (), 3, 3, 2, False, False),
            )

        async def get_diff(self, pull_request: VcsPullRequest) -> tuple[VcsFile, ...]:
            assert pull_request.head_sha == HEAD
            return files

        async def get_blob(self, locator: PullRequestLocator, sha: str) -> bytes:
            assert locator == LOCATOR
            return b"\x89PNG\r\n\x1a\n" if sha == "3" * 40 else b"new\n"

    @dataclass
    class Repository:
        snapshots: list[DiffSnapshot] = field(default_factory=list)

        async def get_run_diff_input(self, run_id: UUID) -> None:
            raise AssertionError("VCS path must use the full repository locator")

        async def get_run_vcs_input(self, run_id: UUID) -> RunVcsInput:
            assert run_id == RUN_ID
            return RunVcsInput(CODE_CHANGE_ID, REPOSITORY_ID, HEAD, BASE, LOCATOR)

        async def get_run_snapshots(self, run_id: UUID) -> None:
            return None

        async def store_diff_snapshots(
            self, run_id: UUID, code_change_id: UUID, head_sha: str, snapshots: list[DiffSnapshot]
        ) -> list[DiffSnapshot]:
            assert run_id == RUN_ID
            assert (code_change_id, head_sha) == (CODE_CHANGE_ID, HEAD)
            self.snapshots = snapshots
            return snapshots

        async def get_run_file_key(self, run_id: UUID, path: str) -> BlobCacheKey | None:
            if run_id != RUN_ID:
                return None
            for snapshot in self.snapshots:
                if snapshot.filename == path and snapshot.blob_sha is not None:
                    return BlobCacheKey(REPOSITORY_ID, snapshot.blob_sha)
            return None

        async def get_run_conventions_input(self, run_id: UUID) -> RunConventionsInput:
            assert run_id == RUN_ID
            return RunConventionsInput(REPOSITORY_ID, ActiveConventionsPrompt(RUN_ID, "prompt"))

    class Conventions:
        async def execute(
            self,
            *,
            repository_id: UUID,
            conventions_prompt: ActiveConventionsPrompt,
            run_id: UUID,
            changed_files: tuple[str, ...],
            rules: tuple[object, ...],
        ) -> GeneratedConventions:
            assert repository_id == REPOSITORY_ID
            assert changed_files == ("src/new.py",)
            return GeneratedConventions(
                CachedConventions(REPOSITORY_ID, None, RUN_ID, (), (), {}), None, False
            )

    repository = Repository()
    cache = InMemoryBlobCache()
    assert asyncio.run(
        ReviewRunProcessor(
            repository,
            None,
            blob_cache=cache,
            conventions=cast(GenerateRepoConventions, Conventions()),
            vcs_provider=GitHub(),
        ).execute(RUN_ID)
    )
    assert [item.filename for item in repository.snapshots] == [
        "src/new.py",
        "package-lock.json",
        "assets/logo.png",
    ]
    assert repository.snapshots[0].blob_sha == "1" * 40
    assert repository.snapshots[0].status == "renamed"
    assert repository.snapshots[0].previous_filename == "src/old.py"
    assert repository.snapshots[1].omission_reason == "generated"
    assert repository.snapshots[2].omission_reason == "binary"
    assert repository.snapshots[2].patch is None

    changed, omitted = review_files_from_snapshots(repository.snapshots)
    assert len(changed) == 1
    assert changed[0].path == "src/new.py"
    assert changed[0].status == "renamed"
    assert changed[0].lines == (DiffLine(1, "removed", "old"), DiffLine(1, "added", "new"))
    assert omitted == ("package-lock.json", "assets/logo.png")
    page = asyncio.run(GetRunFileLines(repository, cache).execute(RUN_ID, "src/new.py", 0, 10))
    assert page.lines == ["new"]


def test_large_generated_file_keeps_blob_sha_without_fetching_blob() -> None:
    class GitHub:
        async def get_pull_request(self, locator: PullRequestLocator) -> VcsPullRequest:
            return VcsPullRequest(
                LOCATOR,
                HEAD,
                BASE,
                PullRequestMeta("PR", None, "octo", "feature", "main", (), 1, 0, 0, False, False),
            )

        async def get_diff(self, pull_request: VcsPullRequest) -> tuple[VcsFile, ...]:
            return (
                VcsFile(
                    "dist/app.min.js",
                    "modified",
                    "2" * 40,
                    None,
                    1,
                    0,
                    1,
                    None,
                    size=1_048_577,
                ),
            )

        async def get_blob(self, locator: PullRequestLocator, sha: str) -> bytes:
            raise AssertionError("omitted generated blob must not be downloaded")

    @dataclass
    class Repository:
        snapshots: list[DiffSnapshot] = field(default_factory=list)

        async def get_run_diff_input(self, run_id: UUID) -> None:
            raise AssertionError("VCS Run cannot use a legacy diff input")

        async def get_run_vcs_input(self, run_id: UUID) -> RunVcsInput:
            return RunVcsInput(CODE_CHANGE_ID, REPOSITORY_ID, HEAD, BASE, LOCATOR)

        async def get_run_snapshots(self, run_id: UUID) -> None:
            return None

        async def store_diff_snapshots(
            self, run_id: UUID, code_change_id: UUID, head_sha: str, snapshots: list[DiffSnapshot]
        ) -> list[DiffSnapshot]:
            self.snapshots = snapshots
            return snapshots

    repository = Repository()
    assert asyncio.run(
        ReviewRunProcessor(
            repository, None, blob_cache=InMemoryBlobCache(), vcs_provider=GitHub()
        ).execute(RUN_ID)
    )
    assert repository.snapshots[0].blob_sha == "2" * 40
    assert repository.snapshots[0].omission_reason == "generated"


def test_github_failure_cannot_fall_back_to_mutable_path_ref_provider() -> None:
    class FailingGitHub:
        async def get_pull_request(self, locator: PullRequestLocator) -> VcsPullRequest:
            raise RuntimeError("GitHub unavailable")

        async def get_diff(self, pull_request: VcsPullRequest) -> tuple[VcsFile, ...]:
            raise AssertionError("no diff after failed PR fetch")

        async def get_blob(self, locator: PullRequestLocator, sha: str) -> bytes:
            raise AssertionError("no blob after failed PR fetch")

    class MutableProvider:
        async def fetch_diff(self, *, code_change_id: UUID, head_sha: str) -> list[DiffSnapshot]:
            raise AssertionError("mutable-ref fallback is forbidden")

        async def fetch_file_content(
            self, *, code_change_id: UUID, head_sha: str, path: str
        ) -> str:
            raise AssertionError("mutable-ref fallback is forbidden")

    class Repository:
        async def get_run_vcs_input(self, run_id: UUID) -> RunVcsInput:
            return RunVcsInput(CODE_CHANGE_ID, REPOSITORY_ID, HEAD, BASE, LOCATOR)

        async def get_run_snapshots(self, run_id: UUID) -> None:
            return None

        async def get_run_diff_input(self, run_id: UUID) -> None:
            raise AssertionError("VCS path cannot fall back")

        async def store_diff_snapshots(
            self, run_id: UUID, code_change_id: UUID, head_sha: str, snapshots: list[DiffSnapshot]
        ) -> list[DiffSnapshot]:
            raise AssertionError("failed fetch cannot persist a snapshot")

    with pytest.raises(RuntimeError, match="GitHub unavailable"):
        asyncio.run(
            ReviewRunProcessor(
                Repository(), MutableProvider(), vcs_provider=FailingGitHub()
            ).execute(RUN_ID)
        )


def test_retry_uses_persisted_run_snapshot_after_provider_head_and_base_change() -> None:
    stored = [
        DiffSnapshot(
            "src/a.py",
            "diff --git a/src/a.py b/src/a.py\n--- a/src/a.py\n+++ b/src/a.py\n"
            "@@ -1 +1 @@\n-old\n+saved",
            blob_sha="1" * 40,
            review_patch="@@ -1 +1 @@\n-old\n+saved",
        )
    ]

    class GitHub:
        async def get_pull_request(self, locator: PullRequestLocator) -> VcsPullRequest:
            raise AssertionError("retry must not fetch mutable PR metadata")

        async def get_diff(self, pull_request: VcsPullRequest) -> tuple[VcsFile, ...]:
            raise AssertionError("retry must not fetch a mutable file list")

        async def get_blob(self, locator: PullRequestLocator, sha: str) -> bytes:
            assert (locator, sha) == (LOCATOR, "1" * 40)
            return b"saved\n"

    class Repository:
        async def get_run_vcs_input(self, run_id: UUID) -> RunVcsInput:
            return RunVcsInput(CODE_CHANGE_ID, REPOSITORY_ID, HEAD, BASE, LOCATOR)

        async def get_run_snapshots(self, run_id: UUID) -> list[DiffSnapshot]:
            return stored

        async def get_run_diff_input(self, run_id: UUID) -> None:
            raise AssertionError("VCS run cannot use mutable legacy path")

        async def store_diff_snapshots(
            self, run_id: UUID, code_change_id: UUID, head_sha: str, snapshots: list[DiffSnapshot]
        ) -> list[DiffSnapshot]:
            raise AssertionError("retry must not rewrite the stored snapshot")

    cache = InMemoryBlobCache()
    assert asyncio.run(
        ReviewRunProcessor(Repository(), None, blob_cache=cache, vcs_provider=GitHub()).execute(
            RUN_ID
        )
    )
    assert asyncio.run(cache.get(BlobCacheKey(REPOSITORY_ID, "1" * 40))).content == "saved\n"
