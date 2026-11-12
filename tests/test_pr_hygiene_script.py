"""Unit tests for ``.github/scripts/check_pr_hygiene.py``."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import NamedTuple

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


class GitRepo(NamedTuple):
    path: Path
    env: dict[str, str]

    def git(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args], cwd=self.path, env=self.env, check=True, capture_output=True, text=True
        )


@pytest.fixture
def _git_repo(tmp_path: Path) -> GitRepo:
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
    repo_obj = GitRepo(repo, git_env)
    repo_obj.git("init", "-q", "--initial-branch=main")
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    repo_obj.git("add", "README.md")
    repo_obj.git("commit", "-qm", "chore: base")
    return repo_obj


def test_cli_reads_github_event_and_git_merge_commits(_git_repo: GitRepo, tmp_path: Path) -> None:
    repo = _git_repo.path
    git_env = dict(_git_repo.env)
    summary_file = tmp_path / "step_summary.md"
    git_env |= {
        "GITHUB_STEP_SUMMARY": str(summary_file),
        "GITHUB_ACTIONS": "true",
        "GITHUB_EVENT_NAME": "pull_request",
    }
    _git_repo.git("checkout", "-qb", "pr")
    (repo / "f.txt").write_text("pr\n", encoding="utf-8")
    _git_repo.git("add", "f.txt")
    _git_repo.git("commit", "-qm", "feat: add change\n\nCloses #99")
    _git_repo.git("checkout", "-q", "main")
    _git_repo.git("merge", "-q", "--no-ff", "-m", "merge", "pr")

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


def test_cli_exit_codes_and_rev_range(_git_repo: GitRepo, tmp_path: Path) -> None:
    repo = _git_repo.path
    git_env = _git_repo.env
    _git_repo.git("checkout", "-qb", "feature")
    (repo / "f.txt").write_text("feat\n", encoding="utf-8")
    _git_repo.git("add", "f.txt")
    _git_repo.git("commit", "-qm", "feat(calc): implement add")

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
    assert "1 commit(s) scanned" in proc_ok.stdout

    # Exit 1: commit with subject > 72 chars in rev-range
    (repo / "f_long.txt").write_text("long\n", encoding="utf-8")
    _git_repo.git("add", "f_long.txt")
    _git_repo.git(
        "commit",
        "-qm",
        "feat(calc): this subject line is far too long and definitely exceeds 72 characters limit",
    )
    proc_fail_len = subprocess.run(
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
    assert proc_fail_len.returncode == 1
    assert "subject exceeds 72 characters" in proc_fail_len.stdout

    # Exit 1: commit with closing keyword in rev-range
    (repo / "f_close.txt").write_text("close\n", encoding="utf-8")
    _git_repo.git("add", "f_close.txt")
    _git_repo.git("commit", "-qm", "feat(calc): close issue\n\nCloses #1")
    proc_fail_close = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "--title",
            "feat(calc): implement addition",
            "--body-file",
            str(body_file),
            "--rev-range",
            "HEAD~1..HEAD",
        ],
        cwd=repo,
        env=git_env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc_fail_close.returncode == 1
    assert "Forbidden closing keyword" in proc_fail_close.stdout


def test_cli_body_file_errors(_git_repo: GitRepo, tmp_path: Path) -> None:
    repo = _git_repo.path
    git_env = _git_repo.env

    # 1. Nonexistent body file -> exit code 2
    proc_missing = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "--title",
            "feat(calc): implement addition",
            "--body-file",
            str(tmp_path / "nonexistent.md"),
            "--rev-range",
            "HEAD^1..HEAD",
        ],
        cwd=repo,
        env=git_env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc_missing.returncode == 2
    assert "Error reading inputs:" in proc_missing.stderr
    assert "--body-file not found" in proc_missing.stderr

    # 2. Corrupted UTF-8 in body file -> exit code 2
    corrupted = tmp_path / "corrupted.md"
    corrupted.write_bytes(b"\xff\xfe\x00\x88\x88")
    proc_corrupt = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "--title",
            "feat(calc): implement addition",
            "--body-file",
            str(corrupted),
            "--rev-range",
            "HEAD^1..HEAD",
        ],
        cwd=repo,
        env=git_env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc_corrupt.returncode == 2
    assert "Error reading inputs:" in proc_corrupt.stderr

    # 3. Passing --body-file without --title -> exit code 2
    valid_body_file = tmp_path / "valid.md"
    valid_body_file.write_text(VALID_BODY, encoding="utf-8")
    proc_no_title = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "--body-file",
            str(valid_body_file),
            "--rev-range",
            "HEAD^1..HEAD",
        ],
        cwd=repo,
        env=git_env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc_no_title.returncode == 2
    assert "Missing --title when passing --body or --body-file." in proc_no_title.stderr


def test_cli_event_path_errors(tmp_path: Path) -> None:
    # 1. Missing event file -> exit code 2
    proc_missing = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "--event-path",
            str(tmp_path / "nonexistent_event.json"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc_missing.returncode == 2
    assert "--event-path not found" in proc_missing.stderr

    # 2. Broken JSON -> exit code 2
    broken_json = tmp_path / "broken.json"
    broken_json.write_text("{broken json", encoding="utf-8")
    proc_broken = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--event-path", str(broken_json)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc_broken.returncode == 2
    assert "Error reading inputs:" in proc_broken.stderr

    # 3. Payload without pull_request object -> exit code 2
    no_pr = tmp_path / "no_pr.json"
    no_pr.write_text(json.dumps({"action": "opened"}), encoding="utf-8")
    proc_no_pr = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--event-path", str(no_pr)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc_no_pr.returncode == 2
    assert "Event payload has no `pull_request` object." in proc_no_pr.stderr


def test_cli_invalid_rev_range(_git_repo: GitRepo) -> None:
    repo = _git_repo.path
    git_env = _git_repo.env

    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "--title",
            "feat(calc): implement addition",
            "--body",
            VALID_BODY,
            "--rev-range",
            "nonexistent_rev_1234..HEAD",
        ],
        cwd=repo,
        env=git_env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 2
    assert "Error reading inputs:" in proc.stderr
    assert "nonexistent_rev_1234..HEAD" in proc.stderr


def test_cli_body_and_draft_flags(_git_repo: GitRepo) -> None:
    repo = _git_repo.path
    git_env = _git_repo.env
    _git_repo.git("checkout", "-qb", "feature")
    (repo / "f2.txt").write_text("ok2", encoding="utf-8")
    _git_repo.git("add", "f2.txt")
    _git_repo.git("commit", "-qm", "feat: extra")

    draft_body = "## What\n\n## Why\n\n## How to verify\n\n## Refs\nRefs #1\n"
    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "--title",
            "feat(calc): implement addition",
            "--body",
            draft_body,
            "--draft",
            "--rev-range",
            "main..HEAD",
        ],
        cwd=repo,
        env=git_env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0
    assert "PR hygiene check passed" in proc.stdout


def test_cli_unresolved_rev_branch_and_actions_formatting(_git_repo: GitRepo) -> None:
    repo = _git_repo.path
    # Repo with single commit on master (neither HEAD^1 nor origin/main exists)
    _git_repo.git("checkout", "-qb", "master")
    _git_repo.git("branch", "-D", "main")
    base_env = dict(_git_repo.env)

    # 1. Local environment (GITHUB_ACTIONS not set): unresolved rev is a warning -> returncode 0
    proc_local = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "--title",
            "feat(calc): implement addition",
            "--body",
            VALID_BODY,
        ],
        cwd=repo,
        env=base_env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc_local.returncode == 0
    assert "WARNING: Could not resolve git commit range" in proc_local.stdout

    # 2. CI environment (GITHUB_ACTIONS=true): unresolved rev is an error -> returncode 1
    ci_env = dict(base_env)
    ci_env["GITHUB_ACTIONS"] = "true"
    proc_ci = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "--title",
            "feat(calc): implement addition",
            "--body",
            VALID_BODY,
        ],
        cwd=repo,
        env=ci_env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc_ci.returncode == 1
    assert "::error::Could not resolve git commit range" in proc_ci.stdout


def test_cli_payload_not_dict(tmp_path: Path) -> None:
    event_file = tmp_path / "event.json"
    event_file.write_text("[]", encoding="utf-8")

    proc = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--event-path", str(event_file)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 2
    assert "Event payload is not a JSON object." in proc.stderr
    assert "Traceback" not in proc.stderr


def test_cli_precedence_over_github_event_path(tmp_path: Path) -> None:
    event_file = tmp_path / "event.json"
    payload = {"pull_request": {"title": "invalid title without conventional format", "body": ""}}
    event_file.write_text(json.dumps(payload), encoding="utf-8")
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("GIT_") and not k.startswith("GITHUB_")
    }
    env["GITHUB_EVENT_PATH"] = str(event_file)

    body_file = tmp_path / "pr_body.md"
    body_file.write_text(VALID_BODY, encoding="utf-8")

    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "--title",
            "ci(pr hygiene): override event payload via CLI",
            "--body-file",
            str(body_file),
            "--rev-range",
            "HEAD~0..HEAD",
        ],
        cwd=Path.cwd(),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0
    assert "PR hygiene check passed" in proc.stdout


def test_cli_conflicting_event_path_and_title(tmp_path: Path) -> None:
    event_file = tmp_path / "event.json"
    event_file.write_text("{}", encoding="utf-8")

    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "--event-path",
            str(event_file),
            "--title",
            "feat(calc): addition",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 2
    assert "Cannot pass both explicit --event-path and --title/--body/--body-file." in proc.stderr


def test_cli_conflicting_body_and_body_file(tmp_path: Path) -> None:
    body_file = tmp_path / "body.md"
    body_file.write_text("body", encoding="utf-8")

    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "--title",
            "feat(calc): addition",
            "--body",
            "inline",
            "--body-file",
            str(body_file),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 2
    assert "Cannot pass both --body and --body-file." in proc.stderr


def test_duplicate_sections_in_body() -> None:
    dup_body = (
        "## What\nFirst what\n\n"
        "## Why\nWhy reason\n\n"
        "## What\nDuplicate what\n\n"
        "## How to verify\nVerify step\n\n"
        "## Refs\nRefs #1"
    )
    res = MOD.check_pr_data(
        title="feat(calc): addition",
        body=dup_body,
        commits=[],
        draft=False,
    )
    assert any("duplicate required section(s): `## What`" in err for err in res.errors)


def test_unclosed_comment_and_fence_closing_keyword() -> None:
    body_unclosed_comment = (
        "## What\n<!-- unclosed comment Closes #1\n\n"
        "## Why\nWhy reason\n\n"
        "## How to verify\nVerify step\n\n"
        "## Refs\nRefs #1"
    )
    res_comment = MOD.check_pr_data(
        title="feat(calc): addition",
        body=body_unclosed_comment,
        commits=[],
        draft=False,
    )
    assert any(
        "Forbidden closing keyword in PR body: 'Closes #1'" in err for err in res_comment.errors
    )

    body_unclosed_fence = (
        "## What\n```\nCloses #1\n\n"
        "## Why\nWhy reason\n\n"
        "## How to verify\nVerify step\n\n"
        "## Refs\nRefs #1"
    )
    res_fence = MOD.check_pr_data(
        title="feat(calc): addition",
        body=body_unclosed_fence,
        commits=[],
        draft=False,
    )
    assert any(
        "Forbidden closing keyword in PR body: 'Closes #1'" in err for err in res_fence.errors
    )


def test_commit_body_code_fence_and_inline_closing_keyword() -> None:
    msg_inline = "chore: rule explanation\n\nNever write `Closes #1` in body."
    c_inline = MOD.CommitInfo(sha="abc12345", message=msg_inline)
    assert MOD.check_commits_data([c_inline]) == []

    msg_raw = "chore: bad message\n\nThis Closes #1 right now."
    c_raw = MOD.CommitInfo(sha="abc12345", message=msg_raw)
    errs = MOD.check_commits_data([c_raw])
    assert len(errs) == 1
    assert "Forbidden closing keyword in commit abc12345: 'Closes #1'" in errs[0]
