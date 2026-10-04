"""Pure GitHub file patch parsing and review-input omission policy."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import pytest

from app.modules.reviews.application.prompt_builder import ChangedFile, DiffLine, PullRequestMeta
from app.modules.reviews.application.vcs_diff import (
    FetchVcsReviewInput,
    OmissionReason,
    PullRequestLocator,
    VcsFile,
    VcsPullRequest,
    parse_file_patch,
)

_HEAD = "a" * 40
_BASE = "b" * 40


def _locator() -> PullRequestLocator:
    return PullRequestLocator(17, "octo/repo", 7)


def _pull_request() -> VcsPullRequest:
    return VcsPullRequest(
        locator=_locator(),
        head_sha=_HEAD,
        base_sha=_BASE,
        meta=PullRequestMeta(
            title="Fix parser",
            description=None,
            author="octo",
            source_branch="feature",
            target_branch="main",
            labels=(),
            files_changed=9,
            lines_added=4,
            lines_removed=2,
            is_draft=False,
            is_fork=False,
        ),
    )


def test_parser_uses_provider_rename_and_literal_old_new_hunk_lines() -> None:
    patch = (
        "@@ -3,3 +3,4 @@ def f():\n"
        " context\n"
        "-old\n"
        "+new\n"
        "+extra\n"
        " tail\n"
        "@@ -10,1 +20,1 @@\n"
        "-gone\n"
        "+added\n"
    )

    assert parse_file_patch("src/new.py", "renamed", patch) == ChangedFile(
        path="src/new.py",
        status="renamed",
        lines=(
            DiffLine(3, "context", "context"),
            DiffLine(4, "removed", "old"),
            DiffLine(4, "added", "new"),
            DiffLine(5, "added", "extra"),
            DiffLine(6, "context", "tail"),
            DiffLine(10, "removed", "gone"),
            DiffLine(20, "added", "added"),
        ),
    )


def test_parser_preserves_unicode_separators_inside_crlf_added_lines() -> None:
    patch = "@@ -1 +1,3 @@\r\n context\r\n+left\u2028middle\u0085right\r\n+next\r\n"

    assert parse_file_patch("src/a.py", "modified", patch) == ChangedFile(
        path="src/a.py",
        status="modified",
        lines=(
            DiffLine(1, "context", "context"),
            DiffLine(2, "added", "left\u2028middle\u0085right"),
            DiffLine(3, "added", "next"),
        ),
    )


@pytest.mark.parametrize(
    "patch",
    ["@@ -1,2 +1,2 @@\n-old\n+new", "@@ -1,1 +1,1 @@\n?invalid"],
)
def test_incomplete_or_unknown_hunk_fails_closed(patch: str) -> None:
    with pytest.raises(ValueError, match="hunk"):
        parse_file_patch("src/a.py", "modified", patch)


def test_review_input_keeps_all_file_metadata_but_omits_nonreviewable_paths() -> None:
    files = (
        VcsFile("src/main.py", "modified", "1" * 40, None, 1, 1, 2, "@@ -1 +1 @@\n-old\n+new"),
        VcsFile("package-lock.json", "modified", "2" * 40, None, 21001, 0, 21001, None),
        VcsFile("dist/app.js", "added", "3" * 40, None, 1, 0, 1, "@@ -0,0 +1 @@\n+x"),
        VcsFile("src/generated.min.js", "modified", "4" * 40, None, 1, 1, 2, None),
        VcsFile("proto/a.pb.go", "added", "5" * 40, None, 1, 0, 1, None),
        VcsFile("__snapshots__/a.snap", "added", "6" * 40, None, 1, 0, 1, None),
        VcsFile("migrations/0001_snapshot.py", "modified", "7" * 40, None, 1, 1, 2, None),
        VcsFile("assets/logo.png", "modified", "8" * 40, None, 1, 1, 2, None),
        VcsFile("src/missing.py", "modified", "9" * 40, None, 1, 1, 2, None),
        VcsFile("locales/pl.json", "modified", "a" * 40, None, 1, 1, 2, "@@ -1 +1 @@\n-old\n+new"),
    )

    @dataclass
    class Provider:
        blobs_requested: list[str] = field(default_factory=list)

        async def get_pull_request(self, locator: PullRequestLocator) -> VcsPullRequest:
            return _pull_request()

        async def get_diff(self, pull_request: VcsPullRequest) -> tuple[VcsFile, ...]:
            return files

        async def get_blob(self, locator: PullRequestLocator, sha: str) -> bytes:
            self.blobs_requested.append(sha)
            return b"\x89PNG\r\n\x1a\nimage" if sha == "8" * 40 else b"text without a patch\n"

    provider = Provider()
    result = asyncio.run(FetchVcsReviewInput(provider).execute(_locator(), _HEAD))

    assert result.pull_request == _pull_request()
    assert result.files == files
    assert result.changed_files == (
        ChangedFile(
            "src/main.py",
            "modified",
            (DiffLine(1, "removed", "old"), DiffLine(1, "added", "new")),
        ),
        ChangedFile(
            "locales/pl.json",
            "modified",
            (DiffLine(1, "removed", "old"), DiffLine(1, "added", "new")),
        ),
    )
    assert result.omitted_files == (
        "package-lock.json",
        "dist/app.js",
        "src/generated.min.js",
        "proto/a.pb.go",
        "__snapshots__/a.snap",
        "migrations/0001_snapshot.py",
        "assets/logo.png",
        "src/missing.py",
    )
    assert [item.reason for item in result.omissions] == [
        OmissionReason.GENERATED,
        OmissionReason.GENERATED,
        OmissionReason.GENERATED,
        OmissionReason.GENERATED,
        OmissionReason.GENERATED,
        OmissionReason.GENERATED,
        OmissionReason.BINARY,
        OmissionReason.MISSING_PATCH,
    ]
    assert provider.blobs_requested == ["8" * 40, "9" * 40]


def test_generated_heuristics_narrowing() -> None:
    from app.modules.reviews.application.vcs_diff import _is_generated

    # Should NOT be classified as generated:
    assert not _is_generated("locales/en.json")
    assert not _is_generated("src/locales/messages.po")
    assert not _is_generated("i18n/fr.json")
    assert not _is_generated("src/l10n/translations.ts")
    assert not _is_generated("arbitrary.lock")
    assert not _is_generated("component.snap")
    assert not _is_generated("src/features/dist/bundle.js")

    # Should be classified as generated:
    assert _is_generated("package-lock.json")
    assert _is_generated("uv.lock")
    assert _is_generated("pnpm-lock.yaml")
    assert _is_generated("dist/app.min.js")
    assert _is_generated("dist/nested/file.js")
    assert _is_generated("app.min.js")
    assert _is_generated("proto/service.pb.go")
    assert _is_generated("__snapshots__/component.test.js.snap")
    assert _is_generated("src/migrations/0001_snapshot_test.py")


def test_oversize_and_stale_head_fail_closed() -> None:
    huge = VcsFile("src/huge.py", "modified", "1" * 40, None, 20001, 0, 20001, None)

    @dataclass
    class Provider:
        diff_calls: int = 0

        async def get_pull_request(self, locator: PullRequestLocator) -> VcsPullRequest:
            return _pull_request()

        async def get_diff(self, pull_request: VcsPullRequest) -> tuple[VcsFile, ...]:
            self.diff_calls += 1
            return (huge,)

        async def get_blob(self, locator: PullRequestLocator, sha: str) -> bytes:
            raise AssertionError("oversize patch must not request blob")

    provider = Provider()
    with pytest.raises(ValueError, match="head SHA"):
        asyncio.run(FetchVcsReviewInput(provider).execute(_locator(), "c" * 40))
    assert provider.diff_calls == 0

    result = asyncio.run(FetchVcsReviewInput(provider).execute(_locator(), _HEAD))
    assert result.changed_files == ()
    assert result.omissions[0].reason == OmissionReason.TOO_LARGE


def test_large_patch_and_non_utf8_text_without_patch_are_distinct_omissions() -> None:
    files = (
        VcsFile(
            "src/huge.py",
            "added",
            "1" * 40,
            None,
            20001,
            0,
            20001,
            "@@ -0,0 +1,20001 @@\n" + "+x\n" * 20001,
        ),
        VcsFile("assets/icon.dat", "modified", "2" * 40, None, 1, 1, 2, None),
    )

    class Provider:
        async def get_pull_request(self, locator: PullRequestLocator) -> VcsPullRequest:
            return _pull_request()

        async def get_diff(self, pull_request: VcsPullRequest) -> tuple[VcsFile, ...]:
            return files

        async def get_blob(self, locator: PullRequestLocator, sha: str) -> bytes:
            assert sha == "2" * 40
            return b"\xff\xfe"

    result = asyncio.run(FetchVcsReviewInput(Provider()).execute(_locator(), _HEAD))

    assert result.changed_files == ()
    assert result.omitted_files == ("src/huge.py", "assets/icon.dat")
    assert [item.reason for item in result.omissions] == [
        OmissionReason.TOO_LARGE,
        OmissionReason.MISSING_PATCH,
    ]


def test_missing_patch_blob_failures_do_not_hide_later_files(
    caplog: pytest.LogCaptureFixture,
) -> None:
    files = (
        VcsFile("src/oversized.py", "modified", "1" * 40, None, 1, 1, 2, None),
        VcsFile("src/unavailable.py", "modified", "2" * 40, None, 1, 1, 2, None),
        VcsFile(
            "src/reviewable.py", "modified", "3" * 40, None, 1, 1, 2, "@@ -1 +1 @@\n-old\n+new"
        ),
    )

    class Provider:
        async def get_pull_request(self, locator: PullRequestLocator) -> VcsPullRequest:
            return _pull_request()

        async def get_diff(self, pull_request: VcsPullRequest) -> tuple[VcsFile, ...]:
            return files

        async def get_blob(self, locator: PullRequestLocator, sha: str) -> bytes:
            if sha == "1" * 40:
                return b"x" * 1_048_577
            raise RuntimeError("one blob is unavailable")

    result = asyncio.run(FetchVcsReviewInput(Provider()).execute(_locator(), _HEAD))

    assert result.files == files
    assert result.omitted_files == ("src/oversized.py", "src/unavailable.py")
    assert [item.reason for item in result.omissions] == [
        OmissionReason.TOO_LARGE,
        OmissionReason.MISSING_PATCH,
    ]
    assert result.changed_files == (
        ChangedFile(
            "src/reviewable.py",
            "modified",
            (
                DiffLine(1, "removed", "old"),
                DiffLine(1, "added", "new"),
            ),
        ),
    )
    assert any(
        "src/unavailable.py" in record.getMessage()
        and "2" * 40 in record.getMessage()
        and "octo/repo" in record.getMessage()
        for record in caplog.records
    )


def test_unicode_separators_do_not_count_as_patch_lines() -> None:
    content = "x\u2028\u0085" * 10001
    file = VcsFile("src/a.py", "added", "1" * 40, None, 1, 0, 1, f"@@ -0,0 +1 @@\n+{content}")

    class Provider:
        async def get_pull_request(self, locator: PullRequestLocator) -> VcsPullRequest:
            return _pull_request()

        async def get_diff(self, pull_request: VcsPullRequest) -> tuple[VcsFile, ...]:
            return (file,)

        async def get_blob(self, locator: PullRequestLocator, sha: str) -> bytes:
            raise AssertionError("patch is present")

    result = asyncio.run(FetchVcsReviewInput(Provider()).execute(_locator(), _HEAD))
    assert result.omissions == ()
    assert result.changed_files == (
        ChangedFile("src/a.py", "added", (DiffLine(1, "added", content),)),
    )


def test_same_head_with_changed_base_is_rejected_before_file_fetch() -> None:
    class Provider:
        async def get_pull_request(self, locator: PullRequestLocator) -> VcsPullRequest:
            return VcsPullRequest(locator, _HEAD, "c" * 40, _pull_request().meta)

        async def get_diff(self, pull_request: VcsPullRequest) -> tuple[VcsFile, ...]:
            raise AssertionError("base drift must stop before file fetch")

        async def get_blob(self, locator: PullRequestLocator, sha: str) -> bytes:
            raise AssertionError("base drift must stop before blob fetch")

    with pytest.raises(ValueError, match="base SHA"):
        asyncio.run(FetchVcsReviewInput(Provider()).execute(_locator(), _HEAD, _BASE))
