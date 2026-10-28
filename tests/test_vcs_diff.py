"""Pure GitHub file patch parsing and review-input omission policy."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, cast
from uuid import UUID

import pytest

from app.modules.reviews.application.check_runs import RunProviderCheckpoint
from app.modules.reviews.application.handle_review_run import (
    AttemptCheckpoint,
    RunLifecycleUnitOfWork,
)
from app.modules.reviews.application.prompt_builder import ChangedFile, DiffLine, PullRequestMeta
from app.modules.reviews.application.run_failures import (
    RequiredSourceUnavailable,
    RetryDelays,
    RunCancelled,
    RunFailure,
    classify_failure,
)
from app.modules.reviews.application.vcs_diff import (
    FetchVcsReviewInput,
    OmissionReason,
    PullRequestLocator,
    VcsFile,
    VcsPullRequest,
    parse_file_patch,
)
from tests.installation_onboarding_fakes import provider_checkpoint

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

        async def get_pull_request(
            self, locator: PullRequestLocator, *, checkpoint: RunProviderCheckpoint
        ) -> VcsPullRequest:
            await checkpoint()
            return _pull_request()

        async def get_diff(
            self, pull_request: VcsPullRequest, *, checkpoint: RunProviderCheckpoint
        ) -> tuple[VcsFile, ...]:
            await checkpoint()
            return files

        async def get_blob(
            self, locator: PullRequestLocator, sha: str, *, checkpoint: RunProviderCheckpoint
        ) -> bytes:
            await checkpoint()
            self.blobs_requested.append(sha)
            return b"\x89PNG\r\n\x1a\nimage" if sha == "8" * 40 else b"text without a patch\n"

    provider = Provider()
    result = asyncio.run(
        FetchVcsReviewInput(provider, checkpoint=provider_checkpoint).execute(_locator(), _HEAD)
    )

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

        async def get_pull_request(
            self, locator: PullRequestLocator, *, checkpoint: RunProviderCheckpoint
        ) -> VcsPullRequest:
            await checkpoint()
            return _pull_request()

        async def get_diff(
            self, pull_request: VcsPullRequest, *, checkpoint: RunProviderCheckpoint
        ) -> tuple[VcsFile, ...]:
            await checkpoint()
            self.diff_calls += 1
            return (huge,)

        async def get_blob(
            self, locator: PullRequestLocator, sha: str, *, checkpoint: RunProviderCheckpoint
        ) -> bytes:
            await checkpoint()
            raise AssertionError("oversize patch must not request blob")

    provider = Provider()
    with pytest.raises(ValueError, match="head SHA"):
        asyncio.run(
            FetchVcsReviewInput(provider, checkpoint=provider_checkpoint).execute(
                _locator(), "c" * 40
            )
        )
    assert provider.diff_calls == 0

    result = asyncio.run(
        FetchVcsReviewInput(provider, checkpoint=provider_checkpoint).execute(_locator(), _HEAD)
    )
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
        async def get_pull_request(
            self, locator: PullRequestLocator, *, checkpoint: RunProviderCheckpoint
        ) -> VcsPullRequest:
            await checkpoint()
            return _pull_request()

        async def get_diff(
            self, pull_request: VcsPullRequest, *, checkpoint: RunProviderCheckpoint
        ) -> tuple[VcsFile, ...]:
            await checkpoint()
            return files

        async def get_blob(
            self, locator: PullRequestLocator, sha: str, *, checkpoint: RunProviderCheckpoint
        ) -> bytes:
            await checkpoint()
            assert sha == "2" * 40
            return b"\xff\xfe"

    result = asyncio.run(
        FetchVcsReviewInput(Provider(), checkpoint=provider_checkpoint).execute(_locator(), _HEAD)
    )

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
        async def get_pull_request(
            self, locator: PullRequestLocator, *, checkpoint: RunProviderCheckpoint
        ) -> VcsPullRequest:
            await checkpoint()
            return _pull_request()

        async def get_diff(
            self, pull_request: VcsPullRequest, *, checkpoint: RunProviderCheckpoint
        ) -> tuple[VcsFile, ...]:
            await checkpoint()
            return files

        async def get_blob(
            self, locator: PullRequestLocator, sha: str, *, checkpoint: RunProviderCheckpoint
        ) -> bytes:
            await checkpoint()
            if sha == "1" * 40:
                return b"x" * 1_048_577
            raise RuntimeError("one blob is unavailable")

    result = asyncio.run(
        FetchVcsReviewInput(Provider(), checkpoint=provider_checkpoint).execute(_locator(), _HEAD)
    )

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
        async def get_pull_request(
            self, locator: PullRequestLocator, *, checkpoint: RunProviderCheckpoint
        ) -> VcsPullRequest:
            await checkpoint()
            return _pull_request()

        async def get_diff(
            self, pull_request: VcsPullRequest, *, checkpoint: RunProviderCheckpoint
        ) -> tuple[VcsFile, ...]:
            await checkpoint()
            return (file,)

        async def get_blob(
            self, locator: PullRequestLocator, sha: str, *, checkpoint: RunProviderCheckpoint
        ) -> bytes:
            await checkpoint()
            raise AssertionError("patch is present")

    result = asyncio.run(
        FetchVcsReviewInput(Provider(), checkpoint=provider_checkpoint).execute(_locator(), _HEAD)
    )
    assert result.omissions == ()
    assert result.changed_files == (
        ChangedFile("src/a.py", "added", (DiffLine(1, "added", content),)),
    )


def test_same_head_with_changed_base_is_rejected_before_file_fetch() -> None:
    class Provider:
        async def get_pull_request(
            self, locator: PullRequestLocator, *, checkpoint: RunProviderCheckpoint
        ) -> VcsPullRequest:
            await checkpoint()
            return VcsPullRequest(locator, _HEAD, "c" * 40, _pull_request().meta)

        async def get_diff(
            self, pull_request: VcsPullRequest, *, checkpoint: RunProviderCheckpoint
        ) -> tuple[VcsFile, ...]:
            await checkpoint()
            raise AssertionError("base drift must stop before file fetch")

        async def get_blob(
            self, locator: PullRequestLocator, sha: str, *, checkpoint: RunProviderCheckpoint
        ) -> bytes:
            await checkpoint()
            raise AssertionError("base drift must stop before blob fetch")

    from app.modules.reviews.application.run_failures import RunFailure

    with pytest.raises(RunFailure, match="pr_base_changed"):
        asyncio.run(
            FetchVcsReviewInput(Provider(), checkpoint=provider_checkpoint).execute(
                _locator(), _HEAD, _BASE
            )
        )


def test_blob_classification_retirement_propagates_without_omissions_or_warnings(
    caplog: pytest.LogCaptureFixture,
) -> None:
    calls: list[str] = []

    class Provider:
        async def get_pull_request(
            self, locator: PullRequestLocator, *, checkpoint: RunProviderCheckpoint
        ) -> VcsPullRequest:
            return _pull_request()

        async def get_diff(
            self, pull_request: VcsPullRequest, *, checkpoint: RunProviderCheckpoint
        ) -> tuple[VcsFile, ...]:
            return tuple(
                VcsFile(f"src/{name}.py", "modified", sha * 40, None, 1, 1, 2, None)
                for name, sha in (("first", "1"), ("second", "2"))
            )

        async def get_blob(
            self, locator: PullRequestLocator, sha: str, *, checkpoint: RunProviderCheckpoint
        ) -> bytes:
            calls.append(sha)
            # The real adapter's pre-request checkpoint detects retirement here.
            raise RunCancelled("installation_unavailable")

    results = []
    with pytest.raises(RunCancelled, match="installation_unavailable"):
        results.append(
            asyncio.run(
                FetchVcsReviewInput(Provider(), checkpoint=provider_checkpoint).execute(
                    _locator(), _HEAD
                )
            )
        )
    assert results == []  # No review input containing fabricated omission entries escapes.
    assert calls == ["1" * 40]
    assert "VCS blob classification failed" not in caplog.text


def test_blob_checkpoint_deadline_stops_fetch_without_an_omission(
    caplog: pytest.LogCaptureFixture,
) -> None:
    deadline = datetime(2026, 10, 19, tzinfo=UTC)
    now = deadline - timedelta(seconds=1)
    calls: list[str] = []

    class CheckpointUow:
        async def __aenter__(self) -> CheckpointUow:
            return self

        async def __aexit__(self, *_args: object) -> None:
            pass

        @property
        def runs(self) -> CheckpointUow:
            return self

        async def cancellation_reason(self, run_id: UUID) -> None:
            return None

    checkpoint = AttemptCheckpoint(
        lambda: cast(RunLifecycleUnitOfWork, CheckpointUow()),
        UUID(int=1),
        deadline,
        lambda: now,
    )

    class Provider:
        async def get_pull_request(
            self, locator: PullRequestLocator, *, checkpoint: RunProviderCheckpoint
        ) -> VcsPullRequest:
            calls.append("metadata")
            await checkpoint()
            return _pull_request()

        async def get_diff(
            self, pull_request: VcsPullRequest, *, checkpoint: RunProviderCheckpoint
        ) -> tuple[VcsFile, ...]:
            calls.append("diff")
            await checkpoint()
            return tuple(
                VcsFile(f"src/{name}.py", "modified", sha * 40, None, 1, 1, 2, None)
                for name, sha in (("first", "1"), ("second", "2"))
            )

        async def get_blob(
            self, locator: PullRequestLocator, sha: str, *, checkpoint: RunProviderCheckpoint
        ) -> bytes:
            nonlocal now
            calls.append(sha)
            now = deadline
            await checkpoint()
            raise AssertionError("deadline must stop before token or HTTP")

    with pytest.raises(RunFailure) as caught:
        asyncio.run(
            FetchVcsReviewInput(Provider(), checkpoint=checkpoint).execute(_locator(), _HEAD)
        )
    assert caught.value.error_code == "deadline_exceeded"
    assert caught.value.message == "attempt deadline passed on a checkpoint"
    assert calls == ["metadata", "diff", "1" * 40]
    assert "VCS blob classification failed" not in caplog.text


@dataclass
class _UnavailableBlobProvider:
    error: BaseException
    requested: list[str] = field(default_factory=list)

    async def get_pull_request(
        self, locator: PullRequestLocator, *, checkpoint: RunProviderCheckpoint
    ) -> VcsPullRequest:
        await checkpoint()
        return _pull_request()

    async def get_diff(
        self, pull_request: VcsPullRequest, *, checkpoint: RunProviderCheckpoint
    ) -> tuple[VcsFile, ...]:
        await checkpoint()
        return (VcsFile("src/app.py", "modified", "1" * 40, None, 1, 1, 2, None),)

    async def get_blob(
        self, locator: PullRequestLocator, sha: str, *, checkpoint: RunProviderCheckpoint
    ) -> bytes:
        await checkpoint()
        self.requested.append(sha)
        raise self.error


@pytest.mark.parametrize(
    "error_code",
    ["github_forbidden", "deadline_exceeded", "policy_fetch_transient", "internal_error"],
)
def test_required_source_rejects_codes_outside_its_fetch_contract(error_code: str) -> None:
    with pytest.raises(ValueError, match="unsupported required source failure code"):
        RequiredSourceUnavailable(cast(Any, error_code))


@pytest.mark.parametrize("error_code", ["diff_fetch_failed", "commit_messages_fetch_failed"])
def test_required_authority_failure_escapes_blob_classification(
    error_code: Literal["diff_fetch_failed", "commit_messages_fetch_failed"],
    caplog: pytest.LogCaptureFixture,
) -> None:
    failure = RequiredSourceUnavailable(error_code, retry_after=timedelta(seconds=180))
    provider = _UnavailableBlobProvider(failure)
    with pytest.raises(RequiredSourceUnavailable) as caught:
        asyncio.run(
            FetchVcsReviewInput(provider, checkpoint=provider_checkpoint).execute(_locator(), _HEAD)
        )
    assert caught.value is failure
    assert caught.value.error_code == error_code
    assert caught.value.message == "Required review source authority is unavailable"
    assert caught.value.retry_after == timedelta(seconds=180)
    assert classify_failure(caught.value) is failure
    assert RetryDelays().for_failure(1, caught.value) == ("10m", timedelta(minutes=10))
    assert provider.requested == ["1" * 40]
    assert "VCS blob classification failed" not in caplog.text


def test_ordinary_fetch_failure_stays_an_optional_blob_omission() -> None:
    provider = _UnavailableBlobProvider(RunFailure("diff_fetch_failed", "optional file"))
    result = asyncio.run(
        FetchVcsReviewInput(provider, checkpoint=provider_checkpoint).execute(_locator(), _HEAD)
    )
    assert result.omitted_files == ("src/app.py",)
    assert result.omissions[0].reason is OmissionReason.MISSING_PATCH
    assert result.failed_blob_shas == frozenset({"1" * 40})


@pytest.mark.parametrize(
    "failure",
    [
        RunFailure("bitbucket_request_budget_exceeded"),
        RunFailure("bitbucket_fetch_failed", rate_limited=True),
    ],
)
def test_bitbucket_blob_budget_and_rate_limit_escape_omission(failure: RunFailure) -> None:
    class TwoFileProvider(_UnavailableBlobProvider):
        async def get_diff(
            self, pull_request: VcsPullRequest, *, checkpoint: RunProviderCheckpoint
        ) -> tuple[VcsFile, ...]:
            await checkpoint()
            return (
                VcsFile("src/app.py", "modified", "1" * 40, None, 1, 1, 2, None),
                VcsFile("src/other.py", "modified", "2" * 40, None, 1, 1, 2, None),
            )

    provider = TwoFileProvider(failure)
    with pytest.raises(RunFailure) as caught:
        asyncio.run(
            FetchVcsReviewInput(provider, checkpoint=provider_checkpoint).execute(_locator(), _HEAD)
        )
    assert caught.value is failure
    assert provider.requested == ["1" * 40]


def test_blob_stage_rechecks_provider_head_after_all_pinned_reads() -> None:
    from app.modules.reviews.infrastructure.vcs_failures import ClassifiedVcsProvider

    verified = 0

    class Provider(_UnavailableBlobProvider):
        async def get_blob(
            self, locator: PullRequestLocator, sha: str, *, checkpoint: RunProviderCheckpoint
        ) -> bytes:
            return b"source bytes"

        async def verify_current(self, *, checkpoint: RunProviderCheckpoint) -> None:
            nonlocal verified
            verified += 1
            raise RunCancelled("superseded")

    with pytest.raises(RunCancelled, match="superseded"):
        asyncio.run(
            FetchVcsReviewInput(
                ClassifiedVcsProvider(cast(Any, Provider(RunFailure("diff_fetch_failed")))),
                checkpoint=provider_checkpoint,
            ).execute(_locator(), _HEAD)
        )
    assert verified == 1


def test_task_cancellation_escapes_blob_classification() -> None:
    failure = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError) as caught:
        asyncio.run(
            FetchVcsReviewInput(
                _UnavailableBlobProvider(failure), checkpoint=provider_checkpoint
            ).execute(_locator(), _HEAD)
        )
    assert caught.value is failure


@pytest.mark.parametrize(
    "reason", [OmissionReason.TOO_LARGE, OmissionReason.MISSING_PATCH, OmissionReason.GENERATED]
)
def test_provider_explicit_omission_does_not_fabricate_size_or_parse_patch(
    reason: OmissionReason,
) -> None:
    class Provider:
        async def get_pull_request(
            self, locator: PullRequestLocator, *, checkpoint: RunProviderCheckpoint
        ) -> VcsPullRequest:
            return _pull_request()

        async def get_diff(
            self, pull_request: VcsPullRequest, *, checkpoint: RunProviderCheckpoint
        ) -> tuple[VcsFile, ...]:
            return (
                VcsFile(
                    "src/app.py",
                    "modified",
                    "1" * 40,
                    None,
                    1,
                    1,
                    2,
                    "@@ -1 +1 @@\n-old\n+new",
                    size=3,
                    omission_reason=reason,
                ),
            )

        async def get_blob(
            self, locator: PullRequestLocator, sha: str, *, checkpoint: RunProviderCheckpoint
        ) -> bytes:
            raise AssertionError("explicit omission must not read a blob")

    result = asyncio.run(
        FetchVcsReviewInput(Provider(), checkpoint=provider_checkpoint).execute(
            _locator(), _HEAD, _BASE
        )
    )
    assert result.changed_files == ()
    assert result.omissions[0].reason is reason
    assert result.files[0].size == 3
