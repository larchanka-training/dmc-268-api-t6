"""review-ignore: the block grammar, the mask matcher and decisions per changed file."""

from __future__ import annotations

from uuid import UUID

import pytest

from app.modules.reviews.application import review_ignore
from app.modules.reviews.application.diff_map import DiffMap, VcsProviderName, build_diff_map
from app.modules.reviews.application.policy_errors import PolicyError, PolicyErrorCode
from app.modules.reviews.application.prompt_builder import FileStatus, PullRequestMeta
from app.modules.reviews.application.review_ignore import (
    IgnoreDecision,
    IgnoreMatcher,
    IgnorePatternLine,
    apply_ignore,
    parse_review_ignore,
)
from app.modules.reviews.application.vcs_diff import PullRequestLocator, VcsFile, VcsPullRequest


def _block(*lines: str, fence: str = "```", info: str = "review-ignore") -> str:
    return "\n".join(["# Ignore", "", f"{fence}{info}", *lines, fence, ""])


def _matcher(*patterns: str) -> IgnoreMatcher:
    return IgnoreMatcher(parse_review_ignore(_block(*patterns)))


def _matches(pattern: str, path: str) -> bool:
    return _matcher(pattern).match(path) == pattern


def _fail(text: str) -> PolicyError:
    with pytest.raises(PolicyError) as caught:
        parse_review_ignore(text, policy_base_sha="b" * 40)
    return caught.value


def test_no_document_no_block_and_an_empty_block_give_an_empty_policy() -> None:
    for text in (None, "", "# Rules\n\nJust prose.\n", _block(), _block("", "   ", "\t")):
        policy = parse_review_ignore(text)
        assert policy.patterns == ()
    assert parse_review_ignore(None).digest == parse_review_ignore(_block()).digest


def test_lines_are_trimmed_deduplicated_sorted_and_keep_their_source_lines() -> None:
    policy = parse_review_ignore(_block("  generated/**  ", "", "dist/**", "generated/**"))

    assert policy.patterns == ("dist/**", "generated/**")
    assert policy.source_lines == (
        IgnorePatternLine("generated/**", 4),
        IgnorePatternLine("dist/**", 6),
        IgnorePatternLine("generated/**", 7),
    )


def test_digest_depends_on_the_masks_and_not_on_their_order() -> None:
    first = parse_review_ignore(_block("a/**", "b/**"))

    assert first.digest == parse_review_ignore(_block("b/**", "a/**", "a/**")).digest
    assert first.digest != parse_review_ignore(_block("a/**")).digest
    assert len(first.digest) == 64


def test_tilde_fence_longer_closing_and_indented_fence_are_the_block() -> None:
    assert parse_review_ignore(_block("a/**", fence="~~~~")).patterns == ("a/**",)
    assert parse_review_ignore("```review-ignore\na/**\n`````\n").patterns == ("a/**",)
    assert parse_review_ignore("   ```review-ignore  \n   a/**\n   ```  \n").patterns == ("a/**",)


def test_other_labels_examples_and_indented_code_are_ordinary_markdown() -> None:
    for text in (
        _block("a/**", info="Review-ignore"),
        _block("a/**", info="review-ignore-example"),
        _block("a/**", info="text"),
        "````markdown\n```review-ignore\na/**\n```\n````\n",
        "    ```review-ignore\n    a/**\n    ```\n",
        "Use `review-ignore` inline.\n",
        "<div>\n```review-ignore\na/**\n```\n</div>\n",
    ):
        assert parse_review_ignore(text).patterns == (), text


def test_block_structure_errors_point_at_the_opening_fence() -> None:
    duplicate = _fail(_block("a/**") + _block("b/**"))
    assert duplicate.code is PolicyErrorCode.IGNORE_BLOCK_DUPLICATE
    assert duplicate.line == 8

    for text in (
        "- item\n\n  ```review-ignore\n  a/**\n  ```\n",
        "> ```review-ignore\n> a/**\n> ```\n",
    ):
        nested = _fail(text)
        assert nested.code is PolicyErrorCode.IGNORE_BLOCK_NESTED

    for text in ("```review-ignore\na/**\n", "```review-ignore", "````review-ignore\na/**\n```\n"):
        unclosed = _fail(text)
        assert unclosed.code is PolicyErrorCode.IGNORE_BLOCK_UNCLOSED
        assert unclosed.line == 1

    info = _fail(_block("a/**", info="review-ignore extra"))
    assert info.code is PolicyErrorCode.IGNORE_INFO_INVALID
    assert (info.line, info.source_path, info.policy_base_sha) == (3, ".review/rules.md", "b" * 40)


def test_invalid_masks_of_the_specification_are_rejected_with_a_position() -> None:
    for pattern in (
        "!src/**",
        "a?.py",
        "[ab].py",
        "*.{ts,tsx}",
        "a\\b",
        "foo**bar",
        "***",
        "a/**b",
        "a/",
        "/a",
        "a//b",
        ".",
        "./foo",
        "a/../b",
        "C:/foo",
        "a\u00a0b",
        "a\tb",
    ):
        error = _fail(_block("ok/**", pattern))
        assert error.code is PolicyErrorCode.IGNORE_PATTERN_INVALID, pattern
        assert (error.line, error.pattern_index) == (5, 1)
        assert error.retryable is False

    assert _fail(_block("src/a?.py")).column == 6
    assert _fail(_block("a//b")).column == 3


def test_valid_masks_with_spaces_hashes_dots_and_unicode() -> None:
    policy = parse_review_ignore(_block("foo bar/**", "# comment", ".env", "docs/файл.md", "**"))

    assert policy.patterns == ("# comment", "**", ".env", "docs/файл.md", "foo bar/**")


def test_limits_of_mask_count_length_and_segments() -> None:
    assert len(parse_review_ignore(_block(*[f"d{i}/**" for i in range(256)])).patterns) == 256
    assert len(parse_review_ignore(_block(*["same/**"] * 256)).patterns) == 1

    count = _fail(_block(*["same/**"] * 257))
    assert count.code is PolicyErrorCode.IGNORE_LIMIT_EXCEEDED
    assert (count.limit, count.actual) == (256, 257)

    assert parse_review_ignore(_block("a" * 256)).patterns == ("a" * 256,)
    length = _fail(_block("a" * 257))
    assert (length.code, length.limit, length.actual) == (
        PolicyErrorCode.IGNORE_LIMIT_EXCEEDED,
        256,
        257,
    )

    assert parse_review_ignore(_block("/".join(["a"] * 32))).patterns
    segments = _fail(_block("/".join(["a"] * 33)))
    assert (segments.limit, segments.actual) == (32, 33)


def test_normative_matching_examples() -> None:
    assert _matches("*.py", "main.py")
    assert not _matches("*.py", "a/main.py")
    for path in ("main.py", "a/main.py", ".hidden/main.py"):
        assert _matches("**/*.py", path)
    for path in ("a/b.py", "a/x/b.py", "a/x/y/b.py"):
        assert _matches("a/**/b.py", path)
    assert _matches("generated/**", "generated/a.py")
    assert _matches("generated/**", "generated")
    assert not _matches("generated/**", "src/generated/a.py")
    assert _matches("**/*", ".env")
    assert _matches("**/*", ".github/workflows/ci.yml")
    assert _matches("src/*", "src/a.ts")
    assert not _matches("src/*", "src/a/b.ts")
    assert not _matches("Src/**", "src/a.py")
    assert _matches("foo bar/**", "foo bar/x.py")
    assert _matches("**", "any/depth/file.py")
    assert _matches("a*c*e", "abcde")
    assert not _matches("a*c*e", "abcdf")
    assert not _matches("src/app.py", "src/app.pyc")
    assert _matches("# comment", "# comment")


def test_matcher_returns_the_first_mask_and_memoizes_a_path() -> None:
    matcher = _matcher("src/**", "**/*.py")

    assert matcher.match("src/a.py") == "**/*.py"
    assert matcher.match("src/a.ts") == "src/**"
    assert matcher.match("docs/a.md") is None
    spent = matcher._run_transitions
    assert matcher.match("src/a.py") == "**/*.py"
    assert matcher._run_transitions == spent


def test_path_outside_the_contract_is_rejected_not_matched() -> None:
    with pytest.raises(PolicyError) as caught:
        _matcher("**").match("a/../b.py")
    assert caught.value.code is PolicyErrorCode.PATH_INVALID


def test_exhausted_budget_stops_the_run(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(review_ignore, "MAX_TRANSITIONS_PER_PATH", 20)
    matcher = _matcher("*a*a*a*b")

    with pytest.raises(PolicyError) as caught:
        matcher.match("a" * 40)
    assert caught.value.code is PolicyErrorCode.MATCH_BUDGET_EXCEEDED
    assert caught.value.limit == 20


def _file(
    filename: str, *, status: FileStatus = "modified", previous_filename: str | None = None
) -> VcsFile:
    return VcsFile(
        filename=filename,
        status=status,
        blob_sha=None if status == "removed" else "d" * 40,
        previous_filename=previous_filename,
        additions=1,
        deletions=1,
        changes=2,
        patch="@@ -1 +1 @@\n-old\n+new",
    )


def _diff_map(*files: VcsFile) -> DiffMap:
    pull_request = VcsPullRequest(
        locator=PullRequestLocator(17, "octo/repo", 7),
        head_sha="a" * 40,
        base_sha="b" * 40,
        meta=PullRequestMeta(
            title="Fix parser",
            description=None,
            author="octo",
            source_branch="feature",
            target_branch="main",
            labels=(),
            files_changed=len(files),
            lines_added=0,
            lines_removed=0,
            is_draft=False,
            is_fork=False,
        ),
    )
    return build_diff_map(
        pull_request,
        files,
        provider=VcsProviderName.GITHUB,
        repository_id=UUID("00000000-0000-0000-0000-000000000001"),
        diff_old_sha="c" * 40,
    )


def test_decisions_are_keyed_by_file_and_leave_the_map_untouched() -> None:
    diff_map = _diff_map(_file("generated/a.py"), _file("src/b.py"), _file("dist/c.js"))
    digest = diff_map.digest

    result = apply_ignore(_matcher("generated/**", "dist/**"), diff_map)

    generated, source, dist = diff_map.files
    assert result.decisions == (
        IgnoreDecision(generated.file_id, "generated/**", "generated/a.py"),
        IgnoreDecision(dist.file_id, "dist/**", "dist/c.js"),
    )
    assert result.decision_for(source.file_id) is None
    assert result.denied_paths == {"generated/a.py", "dist/c.js"}
    assert diff_map.digest == digest
    assert generated.hunks != ()


def test_rename_and_copy_out_of_an_ignored_path_stay_excluded_under_the_new_name() -> None:
    diff_map = _diff_map(
        _file("src/client.py", status="renamed", previous_filename="generated/client.py"),
        _file("src/copy.py", status="copied", previous_filename="generated/base.py"),
        _file("generated/new.py", status="renamed", previous_filename="src/old.py"),
        _file("src/keep.py", status="renamed", previous_filename="src/was.py"),
    )

    result = apply_ignore(_matcher("generated/**"), diff_map)

    client, copy, moved_in, keep = diff_map.files
    assert result.decision_for(client.file_id) == IgnoreDecision(
        client.file_id, "generated/**", "generated/client.py"
    )
    assert result.decision_for(copy.file_id) is not None
    assert result.decision_for(moved_in.file_id) is not None
    assert result.decision_for(keep.file_id) is None
    assert {"src/client.py", "generated/client.py", "src/copy.py", "src/old.py"} <= (
        result.denied_paths
    )
    assert "src/keep.py" not in result.denied_paths


def test_deleted_file_is_matched_by_its_old_path_and_all_files_can_be_excluded() -> None:
    diff_map = _diff_map(_file("generated/gone.py", status="removed"), _file("docs/a.md"))

    result = apply_ignore(_matcher("**"), diff_map)

    assert len(result.decisions) == len(diff_map.files) == 2


def test_file_with_an_unsupported_path_is_skipped_by_the_matcher() -> None:
    diff_map = _diff_map(_file("src\\bad.py"), _file("src/ok.py"))

    result = apply_ignore(_matcher("**"), diff_map)

    assert [item.matched_path for item in result.decisions] == ["src/ok.py"]
