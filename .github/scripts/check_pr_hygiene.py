"""Check PR title, template sections, issue refs, and commit subjects against repo rules.

Reads a GitHub ``pull_request`` event payload (``GITHUB_EVENT_PATH``) or CLI arguments
(--title, --body/--body-file, --draft, --rev-range) and inspects:
1. Forbidden auto-closing keywords (``Closes``/``Fixes``/``Resolves`` + ``#N`` or
   ``owner/repo#N`` or issue URL) in PR title, non-comment/non-code PR body, and commit messages.
2. PR title length (<= 72 characters) and Conventional Commits header format.
3. Non-merge/non-revert commit subject lengths (<= 72 characters).
4. Required PR body sections in order: ``What``, ``Why``, ``How to verify``, ``Refs``.
5. For ready (non-draft) PRs: non-empty content in each section and at least one concrete
   ``Refs #N`` or ``Refs owner/repo#N`` reference (not bare ``Refs #``).

Exit 0: clean; 1: hygiene check failed; 2: invalid CLI arguments, unreadable JSON, or git failure.
"""

# SYNC: mirrored in dmc-268-{ui,api}-t6/.github/scripts/check_pr_hygiene.py

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import re
import subprocess
import sys
from pathlib import Path

MAX_SUBJECT_LEN = 72
REQUIRED_SECTIONS = ("What", "Why", "How to verify", "Refs")

REPO_SLUG = r"(?:[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)"
ISSUE_REF = rf"(?:{REPO_SLUG}?#[0-9]+|https?://github\.com/{REPO_SLUG}/issues/[0-9]+)"
CLOSING_KEYWORD_RE = re.compile(
    rf"(?i)\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\b:?\s+{ISSUE_REF}"
)
CONVENTIONAL_TITLE_RE = re.compile(
    r"^(?:feat|fix|docs|chore|refactor|perf|test|build|ci|style|revert)"
    r"(?:\([^()\n]+\))?!?: .+$"
)
REVERT_SUBJECT_RE = re.compile(r'^Revert ".+"$')
MERGE_SUBJECT_RE = re.compile(r"^Merge .+$")
REFS_VALUE_RE = re.compile(rf"(?im)^(?:[-*]\s+)?Refs:?\s+(?:{REPO_SLUG})?#[0-9]+\b")
EMPTY_REFS_PLACEHOLDER_RE = re.compile(r"(?im)^(?:[-*]\s+)?Refs:?\s+#\s*$")
FENCE_RE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})")
INLINE_CODE_RE = re.compile(r"`[^`\n]+`")
SECTION_HEADING_RE = re.compile(r"^#{1,2}\s+(.+?)\s*$")
THEMATIC_BREAK_RE = re.compile(r"^\s*---+\s*$")


@dataclasses.dataclass(frozen=True)
class CommitInfo:
    """Commit hash and raw message read from git."""

    sha: str
    message: str

    @property
    def subject(self) -> str:
        """First line of the commit message."""
        return self.message.splitlines()[0] if self.message else ""


@dataclasses.dataclass(frozen=True)
class CheckResult:
    """Outcome of PR hygiene validation."""

    errors: tuple[str, ...]
    warnings: tuple[str, ...]


def find_closing_keyword_error(text: str, where: str) -> str | None:
    """Return error message if forbidden closing keyword is found in text, else None."""
    match = CLOSING_KEYWORD_RE.search(text)
    if not match:
        return None
    return (
        f"Forbidden closing keyword in {where}: {match.group(0)!r}. "
        "Use `Refs #N` / `Refs owner/repo#N` instead of Closes/Fixes/Resolves."
    )


def strip_html_comments(text: str) -> str:
    """Remove HTML comments linearly without regex backtracking on unclosed markers."""
    out: list[str] = []
    pos = 0
    while True:
        start = text.find("<!--", pos)
        if start == -1:
            out.append(text[pos:])
            break
        end = text.find("-->", start + 4)
        if end == -1:
            # Unclosed comment: retain marker and content to prevent bypassing checks.
            out.append(text[pos:])
            break
        out.append(text[pos:start])
        pos = end + 3
    return "".join(out)


def strip_comments_and_fences(text: str) -> str:
    """Remove HTML comments and fenced code blocks while preserving line structure."""
    without_comments = strip_html_comments(text)
    kept: list[str] = []
    fence_char: str | None = None
    fence_len = 0
    fence_buffer: list[str] = []
    for line in without_comments.splitlines():
        match = FENCE_RE.match(line)
        if fence_char is None:
            if match:
                fence_char = match.group(1)[0]
                fence_len = len(match.group(1))
                fence_buffer = [line]
                continue
            kept.append(line)
        else:
            token = match.group(1) if match else ""
            if token and token[0] == fence_char and len(token) >= fence_len:
                fence_char = None
                fence_len = 0
                fence_buffer = []
            else:
                fence_buffer.append(line)
    if fence_char is not None:
        # Unclosed code fence: retain lines so closing keywords and content are not concealed.
        kept.extend(fence_buffer)
    return "\n".join(kept)


def extract_sections(stripped_body: str) -> tuple[list[str], dict[str, str], list[str]]:
    """Return seen section order, mapping name -> content, and duplicate section names."""
    seen: list[str] = []
    duplicates: list[str] = []
    sections: dict[str, list[str]] = {}
    canonical_by_lower = {name.lower(): name for name in REQUIRED_SECTIONS}
    current: str | None = None
    for line in stripped_body.splitlines():
        heading_match = SECTION_HEADING_RE.match(line)
        if heading_match:
            heading_text = heading_match.group(1).strip()
            canonical = canonical_by_lower.get(heading_text.lower())
            if canonical is not None:
                if canonical in seen and canonical not in duplicates:
                    duplicates.append(canonical)
                seen.append(canonical)
                current = canonical
                sections.setdefault(canonical, [])
            else:
                current = None
            continue
        if THEMATIC_BREAK_RE.match(line):
            current = None
            continue
        if current is not None:
            sections[current].append(line)
    joined = {name: "\n".join(lines).strip() for name, lines in sections.items()}
    return seen, joined, duplicates


def check_commits_data(commits: list[CommitInfo]) -> list[str]:
    """Validate commit subjects and messages for length and closing keywords."""
    errors: list[str] = []
    for commit in commits:
        short_sha = commit.sha[:8] if commit.sha else "<unknown>"
        subject = commit.subject.strip()
        if (
            subject
            and not MERGE_SUBJECT_RE.match(subject)
            and not REVERT_SUBJECT_RE.match(subject)
            and len(subject) > MAX_SUBJECT_LEN
        ):
            errors.append(
                f"Commit {short_sha} subject exceeds {MAX_SUBJECT_LEN} characters "
                f"({len(subject)} chars): {subject!r}."
            )
        # Check raw subject line for closing keywords
        err = find_closing_keyword_error(subject, f"commit {short_sha}")
        if err:
            errors.append(err)
        # Check commit body (after subject) with comments, fences, and inline code stripped
        lines = commit.message.splitlines()
        if len(lines) > 1:
            body_lines = "\n".join(lines[1:])
            stripped_commit_body = strip_comments_and_fences(body_lines)
            prose_commit_body = INLINE_CODE_RE.sub("", stripped_commit_body)
            err = find_closing_keyword_error(prose_commit_body, f"commit {short_sha}")
            if err:
                errors.append(err)
    return errors


def check_title_data(title: str) -> list[str]:
    """Validate PR title length, Conventional Commits format, and absence of closing keywords."""
    errors: list[str] = []
    clean_title = title.strip()
    if not clean_title:
        errors.append("PR title must not be empty.")
    else:
        if len(clean_title) > MAX_SUBJECT_LEN:
            errors.append(
                f"PR title exceeds {MAX_SUBJECT_LEN} characters ({len(clean_title)} chars)."
            )
        if not (CONVENTIONAL_TITLE_RE.match(clean_title) or REVERT_SUBJECT_RE.match(clean_title)):
            errors.append(
                "PR title must follow Conventional Commits (`<type>(<scope>): <subject>`)."
            )
        err = find_closing_keyword_error(clean_title, "PR title")
        if err:
            errors.append(err)
    return errors


def check_sections_data(body: str, *, draft: bool = False) -> tuple[list[str], list[str]]:
    """Validate PR body sections, ordering, non-empty content, and closing keywords."""
    errors: list[str] = []
    warnings: list[str] = []

    stripped_body = strip_comments_and_fences(body)
    prose_body = INLINE_CODE_RE.sub("", stripped_body)
    err = find_closing_keyword_error(prose_body, "PR body")
    if err:
        errors.append(err)

    seen_sections, section_bodies, duplicate_sections = extract_sections(stripped_body)
    if duplicate_sections:
        errors.append(
            "PR body contains duplicate required section(s): "
            + ", ".join(f"`## {name}`" for name in duplicate_sections)
            + "."
        )

    missing_sections = [name for name in REQUIRED_SECTIONS if name not in section_bodies]
    if missing_sections:
        errors.append(
            "PR body is missing required section(s): "
            + ", ".join(f"`## {name}`" for name in missing_sections)
            + "."
        )
    elif not duplicate_sections and seen_sections != list(REQUIRED_SECTIONS):
        errors.append(
            "PR body sections must appear in order: What -> Why -> How to verify -> Refs."
        )

    empty_sections = [
        name
        for name in REQUIRED_SECTIONS
        if name in section_bodies and not section_bodies[name].strip()
    ]
    if empty_sections:
        msg = "PR body section(s) are empty: " + ", ".join(
            f"`## {name}`" for name in empty_sections
        )
        if draft:
            warnings.append(f"{msg} (allowed while PR is Draft).")
        else:
            errors.append(f"{msg}.")

    refs_body = section_bodies.get("Refs", "")
    if "Refs" in section_bodies:
        has_concrete_ref = bool(REFS_VALUE_RE.search(refs_body))
        only_placeholder = (
            bool(EMPTY_REFS_PLACEHOLDER_RE.search(refs_body)) and not has_concrete_ref
        )
        if not has_concrete_ref:
            reason = (
                "`## Refs` still has unfilled `Refs #` placeholder."
                if only_placeholder
                else "`## Refs` must include `Refs #N` or `Refs owner/repo#N`."
            )
            if draft:
                warnings.append(f"{reason} (allowed while PR is Draft).")
            else:
                errors.append(reason)

    return errors, warnings


def check_pr_data(
    *,
    title: str,
    body: str,
    commits: list[CommitInfo],
    draft: bool = False,
) -> CheckResult:
    """Validate PR metadata and commit messages against repository hygiene rules."""
    errors: list[str] = []
    warnings: list[str] = []

    errors.extend(check_title_data(title))
    sec_errs, sec_warns = check_sections_data(body, draft=draft)
    errors.extend(sec_errs)
    warnings.extend(sec_warns)
    errors.extend(check_commits_data(commits))

    return CheckResult(errors=tuple(errors), warnings=tuple(warnings))


def read_commits_from_git(rev_range: str, cwd: Path | None = None) -> list[CommitInfo]:
    """Read commits in ``rev_range`` from git."""
    proc = subprocess.run(
        ["git", "log", "--format=%H%x1f%B%x1e", rev_range],
        cwd=cwd or Path.cwd(),
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or f"git log failed for {rev_range}")
    commits: list[CommitInfo] = []
    for record in proc.stdout.split("\x1e"):
        stripped = record.strip("\n\r")
        if not stripped or "\x1f" not in stripped:
            continue
        sha, msg = stripped.split("\x1f", 1)
        commits.append(CommitInfo(sha=sha.strip(), message=msg.strip()))
    return commits


def resolve_pr_rev_range(cwd: Path | None = None) -> str | None:
    """Detect ``HEAD^1..HEAD^2`` on a GitHub PR merge commit, or ``origin/main..HEAD``."""
    repo_cwd = cwd or Path.cwd()
    is_github_pr = (
        os.environ.get("GITHUB_ACTIONS") == "true"
        and os.environ.get("GITHUB_EVENT_NAME") == "pull_request"
    )
    candidates = ("HEAD^1..HEAD^2", "origin/main..HEAD") if is_github_pr else ("origin/main..HEAD",)
    for candidate in candidates:
        base, rev = candidate.split("..", 1)
        base_ok = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", base],
            cwd=repo_cwd,
            capture_output=True,
            check=False,
        )
        head_ok = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", rev],
            cwd=repo_cwd,
            capture_output=True,
            check=False,
        )
        if base_ok.returncode == 0 and head_ok.returncode == 0:
            return candidate
    return None


def _emit(level: str, message: str, *, actions: bool) -> None:
    if actions:
        print(f"::{level}::{message}")
    else:
        print(f"{level.upper()}: {message}")


def _write_summary(summary_path: Path | None, result: CheckResult, title: str, draft: bool) -> None:
    if summary_path is None:
        return
    lines = [
        "### PR hygiene check",
        "",
        f"- Title ({len(title.strip())} chars, draft={draft}): `{title.strip()}`",
        f"- Errors: {len(result.errors)}",
        f"- Warnings: {len(result.warnings)}",
    ]
    if result.errors:
        lines.extend(["", "**Errors:**"])
        lines.extend(f"- {err}" for err in result.errors)
    if result.warnings:
        lines.extend(["", "**Warnings:**"])
        lines.extend(f"- {warn}" for warn in result.warnings)
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    with summary_path.open("a", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def _load_inputs(args: argparse.Namespace) -> tuple[str | None, str, bool]:
    """Resolve (title, body, draft) from explicit CLI arguments or event payload."""
    cli_title_mode = args.title is not None or args.body is not None or args.body_file is not None

    if args.event_path is not None and cli_title_mode:
        print(
            "Cannot pass both explicit --event-path and --title/--body/--body-file.",
            file=sys.stderr,
        )
        sys.exit(2)

    if args.body is not None and args.body_file is not None:
        print("Cannot pass both --body and --body-file.", file=sys.stderr)
        sys.exit(2)

    event_path = args.event_path
    if not cli_title_mode and event_path is None:
        env_event = os.environ.get("GITHUB_EVENT_PATH")
        if env_event:
            event_path = Path(env_event)

    if cli_title_mode:
        if args.title is None:
            print("Missing --title when passing --body or --body-file.", file=sys.stderr)
            sys.exit(2)
        title = args.title
        if args.body_file is not None:
            if not args.body_file.is_file():
                print(
                    f"Error reading inputs: --body-file not found: {args.body_file}",
                    file=sys.stderr,
                )
                sys.exit(2)
            try:
                body = args.body_file.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError) as exc:
                print(f"Error reading inputs: {exc}", file=sys.stderr)
                sys.exit(2)
        else:
            body = args.body or ""
        draft = args.draft
        return title, body, draft

    if event_path is not None:
        if not event_path.is_file():
            print(
                f"Error reading inputs: --event-path not found: {event_path}",
                file=sys.stderr,
            )
            sys.exit(2)
        try:
            raw_payload = json.loads(event_path.read_text(encoding="utf-8-sig"))
        except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
            print(f"Error reading inputs: {exc}", file=sys.stderr)
            sys.exit(2)
        if not isinstance(raw_payload, dict):
            print("Event payload is not a JSON object.", file=sys.stderr)
            sys.exit(2)
        pr = raw_payload.get("pull_request")
        if not isinstance(pr, dict):
            print("Event payload has no `pull_request` object.", file=sys.stderr)
            sys.exit(2)
        title = str(pr.get("title") or "")
        body = str(pr.get("body") or "")
        draft = bool(pr.get("draft", False))
        return title, body, draft

    if args.rev_range is None:
        print(
            "Missing pull_request event payload (--event-path, GITHUB_EVENT_PATH, "
            "or --title / --body-file / --rev-range).",
            file=sys.stderr,
        )
        sys.exit(2)

    return None, "", False


def _resolve_commits(args: argparse.Namespace) -> tuple[list[CommitInfo], bool]:
    """Read commits for explicit --rev-range or auto-detected PR range."""
    try:
        rev_range = args.rev_range or resolve_pr_rev_range(cwd=Path.cwd())
        if rev_range:
            return read_commits_from_git(rev_range, cwd=Path.cwd()), False
        return [], True
    except (RuntimeError, OSError) as exc:
        print(f"Error reading inputs: {exc}", file=sys.stderr)
        sys.exit(2)


def _report(
    result: CheckResult,
    *,
    title: str | None,
    draft: bool,
    commits: list[CommitInfo],
    actions: bool,
) -> int:
    """Emit warnings and errors to console / GitHub Actions and summary."""
    for warn in result.warnings:
        _emit("warning", warn, actions=actions)
    for err in result.errors:
        _emit("error", err, actions=actions)

    summary_env = os.environ.get("GITHUB_STEP_SUMMARY")
    if title is not None:
        _write_summary(Path(summary_env) if summary_env else None, result, title, draft)

    if not result.errors:
        target_info = f"title={title!r}, " if title is not None else ""
        count = len(commits)
        print(f"PR hygiene check passed ({target_info}{count} commit(s) scanned, draft={draft}).")
        return 0
    return 1


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint for PR hygiene validation."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--event-path",
        type=Path,
        default=None,
        help=(
            "Path to GitHub Actions event JSON "
            "(falls back to $GITHUB_EVENT_PATH if no CLI title passed)."
        ),
    )
    parser.add_argument(
        "--rev-range",
        default=None,
        help=(
            "Explicit git commit range to check "
            "(defaults to HEAD^1..HEAD^2 in Actions PRs, else origin/main..HEAD)."
        ),
    )
    parser.add_argument(
        "--title",
        default=None,
        help="PR title string when checking without event payload.",
    )
    parser.add_argument(
        "--body",
        default=None,
        help="PR body string when checking without event payload.",
    )
    parser.add_argument(
        "--body-file",
        type=Path,
        default=None,
        help="Path to file containing PR body markdown.",
    )
    parser.add_argument(
        "--draft",
        action="store_true",
        help="Treat PR as Draft (allows empty sections and placeholder refs).",
    )
    args = parser.parse_args(argv)

    actions = os.environ.get("GITHUB_ACTIONS") == "true"
    title, body, draft = _load_inputs(args)
    commits, unresolved_rev = _resolve_commits(args)

    errors: list[str] = []
    warnings: list[str] = []

    if unresolved_rev:
        msg = (
            "Could not resolve git commit range "
            "(neither HEAD^1..HEAD^2 nor origin/main..HEAD found)."
        )
        if actions:
            errors.append(msg)
        else:
            warnings.append(msg)

    if title is not None:
        pr_result = check_pr_data(title=title, body=body, commits=commits, draft=draft)
        errors.extend(pr_result.errors)
        warnings.extend(pr_result.warnings)
    else:
        commit_errors = check_commits_data(commits)
        errors.extend(commit_errors)

    result = CheckResult(errors=tuple(errors), warnings=tuple(warnings))
    return _report(result, title=title, draft=draft, commits=commits, actions=actions)


if __name__ == "__main__":
    sys.exit(main())
