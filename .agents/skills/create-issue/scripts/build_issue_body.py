#!/usr/bin/env python3
"""build_issue_body.py — turn a structured spec into a GitHub issue body
(GitHub-Flavored Markdown) plus the CLI argument vector for `gh issue create`.

The model never hand-writes the body. Two things it reliably gets wrong when it
does: the section order drifts between issues, and user-supplied text is pasted
raw — so a value containing a line that starts with `## Acceptance Criteria`
silently forges a section heading and the checklist below it. Both are handled
here, once.

Usage
-----
    build_issue_body.py <spec.json>              # markdown body on stdout
    build_issue_body.py - < spec.json            # spec from stdin
    build_issue_body.py <spec.json> --emit-args  # JSON argv array on stdout

Contract
--------
stdout carries the deliverable only. Errors go to stderr as {"error": "..."}.
Exit 0 ok, 2 invalid spec. Never exits 0 on a spec it could not honour — a key
the emitter does not know is a bug in the caller, not a no-op.
"""
# SYNC: mirrored in dmc-268-{ui,api}-t6/.agents/skills/create-issue/scripts/build_issue_body.py

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any, NoReturn

# Definition of Done: the team's merge gate (`.agents/rules/git-workflow.md` — one
# approving review, all review threads resolved) plus the repo gates and the board.
# Override per issue with `dod` in the spec.
DEFAULT_DOD = [
    "Gates green (AGENTS.md → Commands)",
    "Docs updated",
    "PR approved, all review threads resolved",
    "Issue status updated on board 12",
]

# Issue type is a repo label, not a field. The repo has no `type:` labels and none
# should be created. `Docs` renders with the Task layout.
TYPES = {"Story", "Bug", "Task", "Docs"}
TYPE_LABEL = {
    "Bug": "bug",
    "Story": "enhancement",
    "Task": "enhancement",
    "Docs": "documentation",
}

# Kickoff — the prose the emitter writes itself, so the place `lang` has to show.
# `lang` records which language the body is written in — the composer decides it
# from the repository, not from the request; an unknown value falls back to English.
# An issue is picked up in a fresh session, or by a contributor who
# was never in the conversation, and the body is then the whole briefing: these
# fields carry the entry points and the hazards. The plan is deliberately
# absent — the assignee writes it against a live checkout, and a stale plan is
# worse than none.
KICKOFF_PROSE = ("repo", "branch", "problem_shape")
KICKOFF_LISTS = ("read_first", "before_writing_code", "mines")
KICKOFF_LABEL = {
    "en": {
        "_heading": "Kickoff — where to start",
        "repo": "Repo",
        "branch": "Branch",
        "problem_shape": "The shape of the problem",
        "read_first": "Read first",
        "before_writing_code": "Before writing code",
        "mines": "Mines",
    },
    "ru": {
        "_heading": "Kickoff — с чего начать",
        "repo": "Репозиторий",
        "branch": "Ветка",
        "problem_shape": "Форма проблемы",
        "read_first": "Что прочитать первым",
        "before_writing_code": "Перед тем как писать код",
        "mines": "Мины",
    },
    "de": {
        "_heading": "Kickoff — wo anfangen",
        "repo": "Repository",
        "branch": "Branch",
        "problem_shape": "Worum es geht",
        "read_first": "Zuerst lesen",
        "before_writing_code": "Vor dem Coden",
        "mines": "Fallstricke",
    },
}

# Whitelist of top-level keys. Anything else — including keys of the sibling
# skills this one was ported from — is rejected, never silently dropped.
ALLOWED_KEYS = {
    "repo",
    "type",
    "lang",
    "summary",
    "as_role",
    "i_want",
    "so_that",
    "background",
    "scope",
    "acceptance_criteria",
    "dependencies",
    "dod",
    "references",
    "environment",
    "steps",
    "expected",
    "actual",
    "severity",
    "labels",
    "assignees",
    "milestone",
    "kickoff",
}
KICKOFF_KEYS = set(KICKOFF_PROSE) | set(KICKOFF_LISTS)
BUG_KEYS = {"environment", "steps", "expected", "actual", "severity"}
# The Bug layout has no place for these; the other layouts have none for BUG_KEYS.
STORY_KEYS = {"as_role", "i_want", "so_that", "background", "scope"}

STR_KEYS = (
    "repo",
    "summary",
    "type",
    "lang",
    "as_role",
    "i_want",
    "so_that",
    "background",
    "environment",
    "expected",
    "actual",
    "severity",
    "milestone",
)
LIST_KEYS = (
    "scope",
    "acceptance_criteria",
    "references",
    "steps",
    "dod",
    "labels",
    "assignees",
)
# These end up as one argv element each (read back line by line by issue-create.sh).
ONE_LINE_KEYS = ("summary", "milestone")
# `repo` names the target of every later `gh` call: exactly OWNER/REPO, nothing else
# (same charset as issue-preflight.sh; a host prefix would 404 in the REST paths).
REPO_NAME = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")


def err(msg: str) -> NoReturn:
    print(json.dumps({"error": msg}, ensure_ascii=False), file=sys.stderr)
    sys.exit(2)


# ─── escaping ───────────────────────────────────────────────────────────────
# Markdown has no equivalent of HTML escaping: almost every character is safe.
# The hazard is *structural* — a line that begins with a markdown construct
# turns user text into document structure. So we neutralise line beginnings and
# leave the rest of the text untouched, which keeps bodies readable.

# `---` / `===` on their own line are setext underlines: they retroactively turn
# the *previous* line into a heading, so trailing whitespace must not let them
# through.
_STRUCTURAL = re.compile(r"^(\s*)([#>]|[-*+](?=\s)|\d+\.(?=\s)|-{3,}\s*$|={3,}\s*$)")


# An unbalanced code fence swallows every section after it into a code block.
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")


def has_unclosed_fence(text: str) -> bool:
    """CommonMark fences: >=3 backticks or tildes opening a line (up to three spaces
    of indent), closed by a line of the same character, at least as long, with
    nothing after it. A backtick run followed by more backticks is inline code."""
    char, width = "", 0
    for line in text.split("\n"):
        m = _FENCE.match(line)
        if not m:
            continue
        run, rest = m.groups()
        if not char:
            if run[0] == "`" and "`" in rest:
                continue
            char, width = run[0], len(run)
        elif run[0] == char and len(run) >= width and not rest.strip():
            char = ""
    return bool(char)


def safe_block(text: object) -> str:
    """Multi-line field (background, expected, ...). Keeps paragraph breaks,
    defuses any line that would start a heading, list, quote or rule."""
    out = []
    for line in str(text).split("\n"):
        out.append(_STRUCTURAL.sub(r"\1\\\2", line))
    return "\n".join(out)


def safe_inline(text: object) -> str:
    """Single list item. A bullet is one line by definition, so newlines
    collapse to spaces — that also removes the only way to escape the item."""
    return " ".join(str(text).split())


# ─── body ───────────────────────────────────────────────────────────────────
def _bullets(items: Any) -> str:
    return "\n".join(f"- {safe_inline(x)}" for x in items)


def _tasks(items: Any) -> str:
    return "\n".join(f"- [ ] {safe_inline(x)}" for x in items)


def _dependencies(s: dict[str, Any]) -> str:
    """Prose, a flat list, or labelled groups."""
    dep = s.get("dependencies", "None")
    if isinstance(dep, dict):
        blocks = []
        for group, value in dep.items():
            if not value:
                continue
            body = _bullets(value) if isinstance(value, (list, tuple)) else safe_block(value)
            blocks.append(f"**{safe_inline(group)}:**\n\n{body}")
        return "\n\n".join(blocks) if blocks else safe_block("None")
    if isinstance(dep, (list, tuple)):
        return _bullets(dep) if dep else safe_block("None")
    return safe_block(dep)


def render_body(s: dict[str, Any]) -> str:
    """Fixed section order. Dependencies and the DoD checklist are emitted
    unconditionally: an issue without them is missing information, not being
    concise."""
    typ = s.get("type", "Story")
    out: list[str] = []

    if typ == "Bug":
        if s.get("environment"):
            out.append(f"**Environment:** {safe_inline(s['environment'])}")
        if s.get("steps"):
            out.append(
                "## Steps to reproduce\n\n"
                + "\n".join(f"{i}. {safe_inline(x)}" for i, x in enumerate(s["steps"], 1))
            )
        if s.get("expected"):
            out.append(f"**Expected:** {safe_inline(s['expected'])}")
        if s.get("actual"):
            out.append(f"**Actual:** {safe_inline(s['actual'])}")
        if s.get("severity"):
            out.append(f"**Severity:** {safe_inline(s['severity'])}")
    else:
        if s.get("as_role") or s.get("i_want") or s.get("so_that"):
            out.append(
                "## User Story\n\n"
                f"**AS** {safe_inline(s.get('as_role', ''))}\n"
                f"**I WANT** {safe_inline(s.get('i_want', ''))}\n"
                f"**SO THAT** {safe_inline(s.get('so_that', ''))}"
            )
        if s.get("background"):
            out.append(f"## Background\n\n{safe_block(s['background'])}")
        if s.get("scope"):
            out.append(f"## Scope\n\n{_bullets(s['scope'])}")

    if s.get("acceptance_criteria"):
        out.append("## Acceptance Criteria\n\n" + _tasks(s["acceptance_criteria"]))

    out.append(f"## Dependencies\n\n{_dependencies(s)}")
    out.append("## DoD Checklist\n\n" + _tasks(s.get("dod") or DEFAULT_DOD))

    if s.get("references"):
        out.append(f"## References\n\n{_bullets(s['references'])}")

    # Last section. An absent or empty block renders nothing at all rather than
    # a bare heading.
    kickoff = s.get("kickoff") or {}
    if any(kickoff.get(k) for k in KICKOFF_KEYS):
        klabel = KICKOFF_LABEL.get(s.get("lang", "en"), KICKOFF_LABEL["en"])
        body = []
        for key in KICKOFF_PROSE:
            if kickoff.get(key):
                body.append(f"**{klabel[key]}:** {safe_block(kickoff[key])}")
        for key in KICKOFF_LISTS:
            if kickoff.get(key):
                body.append(f"**{klabel[key]}:**\n\n{_bullets(kickoff[key])}")
        out.append(f"## {klabel['_heading']}\n\n" + "\n\n".join(body))

    return "\n\n".join(out) + "\n"


# ─── CLI argv ───────────────────────────────────────────────────────────────
def render_args(s: dict[str, Any]) -> list[str]:
    args = ["--title", s["summary"]]
    # The type label goes first; spec labels follow, without duplicates.
    labels = [TYPE_LABEL[s.get("type", "Story")]]
    for extra in s.get("labels", []):
        if extra not in labels:
            labels.append(extra)
    for name in labels:
        args += ["--label", name]
    for who in s.get("assignees", []):
        args += ["--assignee", who]
    if s.get("milestone"):
        args += ["--milestone", str(s["milestone"])]
    return args


# ─── validation ─────────────────────────────────────────────────────────────
def _kind(value: Any) -> str:
    return "null" if value is None else type(value).__name__


def _is_str_list(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(x, str) for x in value)


def _check_str(name: str, value: Any) -> None:
    if not isinstance(value, str):
        err(f'"{name}" must be a string, got {_kind(value)}')


def _check_str_list(name: str, value: Any) -> None:
    if not _is_str_list(value):
        err(f'"{name}" must be a list of strings, got {_kind(value)}')
    # A blank entry is never meaningful, and a blank label or assignee would be
    # lost on the way to `gh` as a bare flag.
    if any(not item.strip() for item in value):
        err(f'"{name}" must not contain blank entries')


def _check_fence(name: str, value: str) -> None:
    if has_unclosed_fence(value):
        err(
            f'"{name}" has an unclosed code fence (``` or ~~~) — it would swallow '
            "the sections after it; close it or remove it"
        )


def _check_dependencies(dep: Any) -> None:
    if isinstance(dep, str):
        _check_fence("dependencies", dep)
    elif isinstance(dep, list):
        _check_str_list("dependencies", dep)
    elif isinstance(dep, dict):
        for group, value in dep.items():
            name = f"dependencies.{group}"
            if isinstance(value, list):
                _check_str_list(name, value)
            else:
                _check_str(name, value)
                _check_fence(name, value)
    else:
        err(f'"dependencies" must be a string, a list or an object, got {_kind(dep)}')


def validate(s: Any) -> None:
    if not isinstance(s, dict):
        err("spec must be a JSON object")

    # A key the emitter does not know means the caller built the spec for some
    # other emitter. Silently ignoring it is how a field goes missing with exit 0,
    # which is exactly the failure this script exists to prevent.
    unknown = set(s) - ALLOWED_KEYS
    if unknown:
        err(f"unknown top-level key(s): {', '.join(sorted(unknown))}")

    for field in ("repo", "summary"):
        if not s.get(field):
            err(f"spec requires {field}")

    # Every value is type-checked: a wrong type is a clear exit 2, never a
    # traceback, and a string given where a list belongs is never iterated per
    # character.
    for key in STR_KEYS:
        if key in s:
            _check_str(key, s[key])
    for key in LIST_KEYS:
        if key in s:
            _check_str_list(key, s[key])
    if "dependencies" in s:
        _check_dependencies(s["dependencies"])

    repo = s["repo"]
    if not REPO_NAME.fullmatch(repo) or any(set(part) == {"."} for part in repo.split("/")):
        err(f'"repo" must be exactly OWNER/REPO, got {repo!r}')

    typ = s.get("type", "Story")
    if typ not in TYPES:
        err(f"type must be one of {sorted(TYPES)}, got {typ!r}")

    for key in ONE_LINE_KEYS:
        if any(c in s.get(key, "") for c in "\r\n"):
            err(f'"{key}" must be a single line')
    for key in ("labels", "assignees"):
        if any(c in item for item in s.get(key, []) for c in "\r\n"):
            err(f'"{key}" entries must be single lines')

    # Same reasoning as unknown keys: a misspelt kickoff key would otherwise drop
    # the hazard the composer meant to pass on, silently and with exit 0.
    kickoff = s.get("kickoff")
    if kickoff is not None:
        if not isinstance(kickoff, dict):
            err('"kickoff" must be an object')
        bad = set(kickoff) - KICKOFF_KEYS
        if bad:
            err(f'unknown key(s) in "kickoff" block: {", ".join(sorted(bad))}')
        for key in KICKOFF_PROSE:
            if key in kickoff:
                _check_str(f"kickoff.{key}", kickoff[key])
                _check_fence(f"kickoff.{key}", kickoff[key])
        for key in KICKOFF_LISTS:
            if key in kickoff:
                _check_str_list(f"kickoff.{key}", kickoff[key])

    if "background" in s:
        _check_fence("background", s["background"])

    # Each layout silently ignores the other's fields; refuse them instead.
    if typ == "Bug":
        stray = STORY_KEYS & set(s)
        if stray:
            err(f"story-only field(s) on a Bug spec: {', '.join(sorted(stray))}")
    else:
        stray = BUG_KEYS & set(s)
        if stray:
            err(f"bug-only field(s) on a {typ} spec: {', '.join(sorted(stray))}")


def main(argv: list[str]) -> int:
    args = list(argv[1:])
    emit_args = "--emit-args" in args
    if emit_args:
        args.remove("--emit-args")
    if len(args) != 1:
        err("usage: build_issue_body.py <spec.json|-> [--emit-args]")

    path = args[0]
    try:
        raw = sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        err(f"cannot read spec: {exc}")
    try:
        spec = json.loads(raw)
    except json.JSONDecodeError as exc:
        err(f"spec is not valid JSON: {exc}")

    validate(spec)
    if emit_args:
        print(json.dumps(render_args(spec), ensure_ascii=False))
    else:
        sys.stdout.write(render_body(spec))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
