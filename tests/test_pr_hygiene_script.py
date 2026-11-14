"""Unit tests for ``.github/scripts/check_pr_hygiene.py``."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / ".github/scripts/check_pr_hygiene.py"


def _load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_pr_hygiene", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


MOD = _load_module()
VALID_BODY = (
    "## What\n\n"
    "Add PR hygiene checks.\n\n"
    "### Details\n\n"
    "- Validate closing keywords\n\n"
    "## Why\n\n"
    "Stop auto-close regressions early.\n\n"
    "## How to verify\n\n"
    "- uv run pytest\n\n"
    "## Refs\n\n"
    "<!-- `Refs owner/repo#N` for cross-repo. Never Closes/Fixes/Resolves. -->\n\n"
    "Refs #270\n\n"
    "---\n\n"
    "Before merge:\n"
    "- [ ] No closing keyword\n"
)


def test_valid_ready_pr_with_template_comment_and_subheading_passes() -> None:
    commits = [MOD.CommitInfo(sha="abc12345", message="ci: add hygiene script (#270)")]
    result = MOD.check_pr_data(
        title="ci(hygiene): validate PR metadata (#270)",
        body=VALID_BODY,
        commits=commits,
        draft=False,
    )
    assert result.errors == ()
    assert result.warnings == ()


def test_cross_repo_refs_and_backticked_rule_citations_pass() -> None:
    body = VALID_BODY.replace("Refs #270", "Refs larchanka-training/dmc-268-ui-t6#286").replace(
        "Validate closing keywords",
        "Disallow `Closes #286` in body prose examples",
    )
    result = MOD.check_pr_data(
        title="chore(rules): clarify template checks",
        body=body,
        commits=[MOD.CommitInfo(sha="11111111", message="chore: update rules")],
        draft=False,
    )
    assert result.errors == ()


def test_comment_and_fence_exclusions_for_closing_keywords() -> None:
    body_with_comment_and_fence = (
        "## What\n\n"
        "Valid text describing the change.\n\n"
        "<!-- Closes #1 -->\n\n"
        "```\n"
        "Fixes #2\n"
        "```\n\n"
        "## Why\n\n"
        "Comment and fence content must not trigger false positive.\n\n"
        "## How to verify\n\n"
        "- uv run pytest\n\n"
        "## Refs\n\n"
        "Refs #336\n"
    )
    res_clean = MOD.check_pr_data(
        title="feat(rules): allow closing phrases inside comments and fences",
        body=body_with_comment_and_fence,
        commits=[],
        draft=False,
    )
    assert res_clean.errors == ()

    naked_body = body_with_comment_and_fence + "\nCloses #1\n"
    res_naked = MOD.check_pr_data(
        title="feat(rules): fail on naked closing phrase",
        body=naked_body,
        commits=[],
        draft=False,
    )
    assert any("Forbidden closing keyword in PR body" in err for err in res_naked.errors)


@pytest.mark.parametrize(
    "bad_phrase",
    [
        "Closes #286",
        "closes larchanka-training/dmc-268-api-t6#42",
        "Fixes: #278",
        "Fixed #12",
        "Resolves https://github.com/larchanka-training/dmc-268-ui-t6/issues/296",
    ],
)
def test_closing_keywords_fail_in_body_and_commits(bad_phrase: str) -> None:
    body = VALID_BODY.replace("Refs #270", bad_phrase)
    res_body = MOD.check_pr_data(
        title="chore(rules): update rules",
        body=body,
        commits=[],
        draft=True,
    )
    assert any("Forbidden closing keyword in PR body" in err for err in res_body.errors)

    res_commit = MOD.check_pr_data(
        title="chore(rules): update rules",
        body=VALID_BODY,
        commits=[MOD.CommitInfo(sha="deadbeef", message=f"feat: change\n\n{bad_phrase}")],
        draft=False,
    )
    assert any("Forbidden closing keyword in commit" in err for err in res_commit.errors)


def test_empty_and_long_title_fail() -> None:
    res_empty = MOD.check_pr_data(
        title="   ",
        body=VALID_BODY,
        commits=[],
        draft=False,
    )
    assert any("PR title must not be empty" in err for err in res_empty.errors)

    res_close = MOD.check_pr_data(
        title="fix(scope): handle edge case Closes #10",
        body=VALID_BODY,
        commits=[],
        draft=False,
    )
    assert any("Forbidden closing keyword in PR title" in err for err in res_close.errors)

    res_long = MOD.check_pr_data(
        title="feat(scope): " + "x" * 70,
        body=VALID_BODY,
        commits=[MOD.CommitInfo(sha="12345678", message="feat: " + "y" * 70)],
        draft=False,
    )
    assert any("PR title exceeds 72 characters" in err for err in res_long.errors)
    assert any("Commit 12345678 subject exceeds 72 characters" in err for err in res_long.errors)


@pytest.mark.parametrize(
    "nonconventional_title",
    [
        "Update README.md",
        "just random text without type",
        "feat(): empty scope",
        "unknown(scope): unsupported type",
    ],
)
def test_nonconventional_title_fails(nonconventional_title: str) -> None:
    res = MOD.check_pr_data(
        title=nonconventional_title,
        body=VALID_BODY,
        commits=[],
        draft=False,
    )
    assert any("PR title must follow Conventional Commits" in err for err in res.errors)


def test_revert_title_and_merge_or_revert_commit_skips_pass() -> None:
    res_revert = MOD.check_pr_data(
        title='Revert "feat(core): some previous change"',
        body=VALID_BODY,
        commits=[],
        draft=False,
    )
    assert not any("PR title must follow Conventional Commits" in err for err in res_revert.errors)

    long_merge = (
        "Merge branch 'feat/some-very-long-branch-name-exceeding-seventy-two-characters' into main"
    )
    long_revert = (
        'Revert "feat(very-long-scope): very long subject exceeding seventy-two characters"'
    )
    commits = [
        MOD.CommitInfo(sha="11111111", message=long_merge),
        MOD.CommitInfo(sha="22222222", message=long_revert),
    ]
    res_skips = MOD.check_pr_data(
        title="chore(git): merge and revert branch",
        body=VALID_BODY,
        commits=commits,
        draft=False,
    )
    assert not any("subject exceeds" in err for err in res_skips.errors)


def test_missing_or_reordered_sections_fail_and_draft_warns_on_empty_refs() -> None:
    res_missing = MOD.check_pr_data(
        title="chore(test): check missing",
        body="## What\nOnly what section is present\n",
        commits=[],
        draft=False,
    )
    assert any("PR body is missing required section(s)" in err for err in res_missing.errors)

    reordered = "## Why\nx\n## What\ny\n## How to verify\nz\n## Refs\nRefs #1\n"
    res_order = MOD.check_pr_data(
        title="chore(test): check", body=reordered, commits=[], draft=False
    )
    assert any("must appear in order" in err for err in res_order.errors)

    draft_placeholder = "## What\n\nWIP\n\n## Why\n\nWIP\n\n## How to verify\n\n## Refs\n\nRefs #\n"
    res_draft = MOD.check_pr_data(
        title="chore(test): draft work", body=draft_placeholder, commits=[], draft=True
    )
    assert res_draft.errors == ()
    assert len(res_draft.warnings) == 2

    res_ready = MOD.check_pr_data(
        title="chore(test): ready work", body=draft_placeholder, commits=[], draft=False
    )
    assert any("How to verify" in err for err in res_ready.errors)
    assert any("unfilled `Refs #` placeholder" in err for err in res_ready.errors)


def test_cli_reads_github_event_and_git_merge_commits(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    git_env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("GIT_") and not k.startswith("GITHUB_")
    }
    summary_file = tmp_path / "step_summary.md"
    git_env |= {
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.test",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.test",
        "GITHUB_STEP_SUMMARY": str(summary_file),
    }

    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=repo, env=git_env, check=True, capture_output=True)

    git("init", "-q", "--initial-branch=main")
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    git("add", "README.md")
    git("commit", "-qm", "chore: base")
    git("checkout", "-qb", "pr")
    (repo / "f.txt").write_text("pr\n", encoding="utf-8")
    git("add", "f.txt")
    git("commit", "-qm", "feat: add change\n\nCloses #99")
    git("checkout", "-q", "main")
    git("merge", "-q", "--no-ff", "-m", "merge", "pr")

    event = tmp_path / "event.json"
    event.write_text(
        json.dumps(
            {"pull_request": {"title": "feat(app): add change", "body": VALID_BODY, "draft": False}}
        ),
        encoding="utf-8",
    )
    proc = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--event-path", str(event)],
        cwd=repo,
        env=git_env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 1
    assert "Forbidden closing keyword in commit" in proc.stdout
    assert summary_file.exists()
    summary_content = summary_file.read_text(encoding="utf-8")
    assert "### PR hygiene check" in summary_content
    assert "Errors: 1" in summary_content


def test_cli_exit_codes_and_rev_range(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    git_env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("GIT_") and not k.startswith("GITHUB_")
    }
    git_env |= {
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.test",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.test",
    }

    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=repo, env=git_env, check=True, capture_output=True)

    git("init", "-q", "--initial-branch=main")
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    git("add", "README.md")
    git("commit", "-qm", "chore: base")
    git("checkout", "-qb", "feature")
    (repo / "f.txt").write_text("feat\n", encoding="utf-8")
    git("add", "f.txt")
    git("commit", "-qm", "feat(calc): implement add")

    # Exit 2: invalid arguments or missing required flags without event path
    proc_err = subprocess.run(
        [sys.executable, str(SCRIPT_PATH)],
        cwd=repo,
        env=git_env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc_err.returncode == 2

    # Exit 0: clean PR using CLI flags and --rev-range
    body_file = tmp_path / "pr_body.md"
    body_file.write_text(VALID_BODY, encoding="utf-8")
    proc_ok = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "--title",
            "feat(calc): implement addition",
            "--body-file",
            str(body_file),
            "--rev-range",
            "main..HEAD",
        ],
        cwd=repo,
        env=git_env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc_ok.returncode == 0
    assert "PR hygiene check passed" in proc_ok.stdout
