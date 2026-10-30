#!/usr/bin/env python3
"""preflight.py — can this GitHub issue be taken, and which part of it?

The pre-start check of the implement-issue skill. It collects what the skill needs
before any branch is cut and states it as facts plus a verdict: the issue's state,
its native blockers, its parts («Ч.N» / «Part N» / «Teil N») with the pull requests that
reference them, the Kickoff, the board Status, and the part a run should take — or that
the work is done and acceptance is next.

Usage
-----
    preflight.py REF [--part K]
    REF := api#N | ui#N | OWNER/NAME#N | N        (a bare N is api#N)

Contract
--------
stdout carries one JSON document (`sort_keys`, indent 2, no ASCII escaping, one
trailing newline), schema 1:

    issue     {repo, number, title, url, state, assignees, board_status}
              board_status: the Status of the issue on board 12 of
              larchanka-training, null when it is not on that board.
    blockers  [{ref, title, state}] — the native `blockedBy` links, any repo,
              sorted by ref. Only state OPEN blocks.
    parts     [{n, label, scope, boundary, done_when, acceptance_criteria, prs,
              status}] — top-level bullets under `## Scope` that start with
              `Ч.N` / `Part N` / `Teil N`, an optional `(label)`, then `:`.
              Bullets that repeat a number are one part (scope lines joined by a
              newline). The nested `Граница PR:` / `PR boundary:` / `PR-Grenze:`
              and `Готово, когда:` / `Done when:` / `Fertig, wenn:` bullets give
              boundary and done_when (null when absent). acceptance_criteria: the
              items under `## Acceptance Criteria` prefixed `Ч.N:` / `Part N:` /
              `Teil N:`, the prefix removed.
              prs: [{ref, url, title, state}] of the pull requests that name
              the part. status: done (a MERGED pull request names it), else
              open_pr (an OPEN one does), else todo; CLOSED ones never count.
    common_acceptance_criteria  the criteria without a part prefix.
    task_prs  the pull requests with `Refs` when the issue has no parts, else [].
    selected  {part, reason} or null. part is null for the whole task; reason is
              first-todo | explicit | whole-task.
    kickoff   {present, text} — the `## Kickoff…` section, fences and HTML
              comments left out.
    verdict   go | stop | accept; reasons: why it is not a go; warnings: what to
              know on a go.

A number read from text (a part, an issue in `Refs`, `of #N`) has at most 9 digits;
a longer one names nothing, whatever it stands next to.

A pull request counts only when its title or body has a `Refs` line naming this
issue: a line starting with `Refs` (any case, an optional list bullet, an optional
colon) followed by `#N` or `OWNER/NAME#N` references separated by commas, `;`,
`&`, `and` or blanks; `#N` counts for a pull request in the issue's own repo, the
full form from any repo. Anything else is not recognised. Pull requests come from
the issue's timeline (both repos, every page). A pull request names the parts
found in its TITLE (`Part K`, `Ч.K`, `Ч. K`, `Teil K`, and `Часть K` in any case form:
`Части K`, `Частью K`; case-insensitive, whole numbers, any horizontal whitespace
after the label), all of them: a title that names several is deliberate. A range
(`Parts 1–3`, `Part 1-3`, `Части 1–3`: the dash is `-`, `–` or `—`) names no single
part when its lower end is below its upper end, and the upper end is a whole number
not followed by a letter, a digit, `-`, or `.` / `:` / `/` and a digit (`Part 2 — 28.10`
names Part 2, `Part 2 - 3 files` is a range): a title with a range anywhere names
none. A plural label (`Parts`, `Teile`, `Части`) with `K, M` / `K and M` / `K и M` /
`K und M` names every listed part. If the title names none, the FIRST mention in the
first non-empty line of its `## What` section (a heading `What` followed by the end
of the line, a blank or a dash; the whole body when it has no such section) names
its parts: a later mention there is context ("Part 1, which this extends, is merged"),
not scope, and when that first mention is a range the line names no part. A mention
followed by `of <ref>` (`von`, `из` too; `#N`, `api#N`, `ui#N` or `OWNER/NAME#N`, a
bare `#N` being the pull request's own repo; the ref may be wrapped in `**…**`,
backticks or a markdown link) belongs to that issue: when it is not the one checked it
is skipped as if absent — the next mention decides in a title, and a What line whose
first mention is skipped names no part. A pull request whose `Refs` name two or
more issues has to tie each mention to its issue: `Part K of <ref>`, or `<ref> part K`
(`Ч.K`, `часть K`; the ref wrapped as above) in front of it. An untied mention there
is skipped like a mention of another issue. A pull request that ends up naming more
than one part counts for each and adds a warning. Lines in fenced code and HTML
comments never count. A quoted line never counts as a `Refs` line (it does not start
with `Refs`), but quotes are not skipped for the first line of `## What`: a pull
request that opens with a quote is read from the quote.

Decisions, in this order: the issue is CLOSED -> stop; any OPEN blocker -> stop;
parts not numbered 1..k -> stop; `--part K` must exist (exit 2) and is taken
as is; no `--part`: an OPEN pull request with `Refs` that names no part of the
issue stops (whichever part it belongs to is not known: "say which part it covers,
or pass --part"), else the first part that is not done — open_pr stops, todo is
taken; every part done and none with an OPEN pull request -> accept (acceptance is
next), unless the issue was returned at acceptance (see below) -> stop; no parts: the
whole task, unless a pull request with `Refs` is OPEN (stop) or MERGED (accept, or
stop when returned). With `--part K`, or when the pull request that names no part is
MERGED or CLOSED, it is only a warning.

Returned at acceptance: accept-issue marks its comments (`<!-- accept-issue:table v1 -->`,
`accepted v1`, `returned v1`, at the start of the comment). When the latest of those
comments is `returned` and no pull request with `Refs` for this issue was merged after
it, the issue is not ready for acceptance again: the verdict is stop, and the reason
links the comment.

Exit 0 go; 1 stop or accept (the document is still on stdout); 2 usage, bad REF or a
`--part` the issue does not have; 3 gh / API error (non-zero gh exit, timeout,
GraphQL errors, issue not found, unexpected shape, missing fixture, fixture dir
set but empty). On 2 and 3 stderr carries one JSON line {"error": "..."} and
stdout stays empty. Read-only: the only requests are `gh api graphql` queries.

Offline seam: with IMPLEMENT_ISSUE_FIXTURE_DIR set, gh is never called and every
request reads <dir>/<name>.json (the full {"data": ...} response) instead; set
but empty exits 3, never falls through to gh. Names: issue-p1 (the issue, its
first blocker, timeline and comment pages), blocked-p<k>, timeline-p<k> and
comments-p<k> (the follow-up pages, the first is p2).
"""
# SYNC: mirrored in dmc-268-{ui,api}-t6/.agents/skills/implement-issue/scripts/preflight.py

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable, Iterator
from datetime import datetime
from pathlib import Path
from typing import Any, NoReturn

SCHEMA = 1
ALIASES = {
    "api": "larchanka-training/dmc-268-api-t6",
    "ui": "larchanka-training/dmc-268-ui-t6",
}
BOARD_OWNER = "larchanka-training"
BOARD_NUMBER = 12
FIXTURE_ENV = "IMPLEMENT_ISSUE_FIXTURE_DIR"
GH_TIMEOUT = 120  # seconds per gh call
MAX_PAGES = 200  # a page that always has a next page must not loop forever
MAX_DIGITS = 9  # a number read from text is at most this long; a longer one names nothing

# The CLI REF: at most MAX_DIGITS digits, so that a longer one is a bad REF (exit 2).
REF = re.compile(r"(api|ui|[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#([1-9][0-9]{0,8})")
NUMBER = re.compile(r"[1-9][0-9]{0,8}")

HEADING = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.*?)[ \t]*#*[ \t]*$")
# CommonMark fences: at most 3 spaces of indent; a backtick opener's info string
# has no backtick; a closer is the same character, at least as long, and bare.
FENCE_OPEN = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
FENCE_CLOSE = re.compile(r"^ {0,3}(`{3,}|~{3,})[ \t]*$")

# A `Refs` line: the keyword, then a list of references (`#12`, `owner/repo#12`)
# separated by commas / `and` / `&` / blanks; text after the list is ignored.
_TOKEN = r"(?:[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)?#[1-9][0-9]*"
REFS_LINE = re.compile(
    rf"^\s*(?:[-*+]\s+)?Refs?\b:?\s*({_TOKEN}(?:\s*(?:,|;|&|\band\b)?\s*{_TOKEN})*)",
    re.IGNORECASE,
)
REFS_TOKEN = re.compile(r"([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)?#([1-9][0-9]*)")

# A part number as the Scope bullets and the criteria of create-issue write it: `Ч.K`
# (ru), `Part K` (en), `Teil K` (de) — a whole number, so `Ч.1` is not `Ч.12`. Digit runs are
# matched whole (`[0-9]+`) and turned into numbers by `to_int`, which returns None past
# MAX_DIGITS: a regex that capped the run would let `Part 2 of #<5000 digits>` fall back to an
# untied `Part 2`.
_LABEL = r"(?:Ч\.[ \t]*|Part[ \t]+|Teil[ \t]+)"
_PART = rf"{_LABEL}([0-9]+)(?![0-9])"
# The same in a pull request's prose: horizontal whitespace of any kind (a no-break space
# included) after the label, and «Часть K» too (in any case form: «Части K», «Частью K»).
# `mentions` finds a label and a number, then reads what follows: a range («Parts 1–3»,
# «Части 1–3», «Part 1-3», the dash `-`, `–` or `—`), whose upper end is a whole number not
# followed by a letter, a digit, `-`, or `.` / `:` / `/` and a digit («Part 1 – 2nd step»,
# «Part 1 — 2026-10-29» and «Part 2 — 28.10» are not ranges); a list after a plural label
# («Parts 2 and 3», «Части 1, 2 и 3»); `of <ref>` (also «von», «из»), the issue the part
# belongs to: `#N` (the pull request's own repo), `api#N`, `ui#N` or `owner/name#N`, bare or
# wrapped in `**…**`, backticks or a markdown link; and, in front of the label, a ref the
# mention is tied to (`#168 part 6`).
_H = r"[^\S\r\n]"  # horizontal whitespace
_ANY_LABEL = rf"(?:Ч\.{_H}*|(?:Parts?|Teil(?:e|en)?|Част(?:ь|и|ью|ей|ям|ями|ях)){_H}+)"
SINGLE_LABELS = {"part", "teil", "часть", "части", "частью"}  # `Ч.` is single too
PLURAL_LABELS = {"parts", "teile", "teilen", "части", "частей", "частям", "частями", "частях"}
_OPEN = r"(?:\*\*|`|\[)?"
_CLOSE = r"(?:\*\*|`|\]\([^)\s]*\))?"
_REPO = r"(?:[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+|api|ui)"
_AND = rf"(?:and|и|und){_H}+"
LABEL_AT = re.compile(rf"(?<!\w)(?P<label>{_ANY_LABEL})(?P<k>[0-9]+)", re.IGNORECASE)
RANGE_TAIL = re.compile(rf"{_H}*[-–—]{_H}*(?P<m>[0-9]+)(?![^\W_]|-)(?![.:/][0-9])", re.IGNORECASE)
LIST_TAIL = re.compile(
    rf"(?:{_H}*,{_H}*(?:{_AND})?|{_H}+{_AND})(?P<m>[0-9]+)(?![0-9]|[.:/][0-9])", re.IGNORECASE
)
OF_TAIL = re.compile(
    rf"{_H}+(?:of|von|из){_H}+{_OPEN}(?P<repo>{_REPO})?#(?P<n>[1-9][0-9]*)(?![0-9]){_CLOSE}",
    re.IGNORECASE,
)
PRE_REF = re.compile(
    rf"(?<!\w){_OPEN}(?P<repo>{_REPO})?#(?P<n>[1-9][0-9]*)(?![0-9]){_CLOSE}{_H}+\Z", re.IGNORECASE
)
PRE_WINDOW = 160  # characters in front of a label searched for the ref it is tied to
# The markers accept-issue puts at the start of its comments (accept-issue/scripts/acceptance.py:
# TABLE_MARK, ACCEPTED_MARK, RETURNED_MARK). Each skill is self-contained, so they are copied;
# a test compares them with the original.
ACCEPT_MARKS = {
    "<!-- accept-issue:table v1 -->": "table",
    "<!-- accept-issue:accepted v1 -->": "accepted",
    "<!-- accept-issue:returned v1 -->": "returned",
}
PART_START = re.compile(_PART, re.IGNORECASE)
PART_BULLET = re.compile(rf"{_PART}[ \t]*(?:\((.*?)\))?[ \t]*:[ \t]*(.*)", re.IGNORECASE)
PART_CRITERION = re.compile(rf"{_PART}[ \t]*:[ \t]*(.*)", re.IGNORECASE)
CHECKBOX_PREFIX = re.compile(r"\[[ xX]\][ \t]+")
TOP_BULLET = re.compile(r"[-*+][ \t]+(.*)$")
NESTED_BULLET = re.compile(r"[ \t]+[-*+][ \t]+(.*)$")
BOUNDARY_LABELS = ("граница pr", "pr boundary", "pr-grenze")
DONE_WHEN_LABELS = ("готово, когда", "done when", "fertig, wenn")

ISSUE_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    issue(number: $number) {
      title url state body
      assignees(first: 20) { nodes { login } }
      projectItems(first: 100) {
        pageInfo { hasNextPage endCursor }
        nodes {
          project {
            number
            owner { __typename ... on Organization { login } ... on User { login } }
          }
          fieldValueByName(name: "Status") {
            __typename
            ... on ProjectV2ItemFieldSingleSelectValue { name }
          }
        }
      }
      blockedBy(first: 100) {
        pageInfo { hasNextPage endCursor }
        nodes { number title state repository { nameWithOwner } }
      }
      timelineItems(first: 100, itemTypes: [CROSS_REFERENCED_EVENT]) {
        pageInfo { hasNextPage endCursor }
        nodes {
          __typename
          ... on CrossReferencedEvent {
            source {
              __typename
              ... on PullRequest {
                number title url body state mergedAt repository { nameWithOwner }
              }
            }
          }
        }
      }
      comments(first: 100) {
        pageInfo { hasNextPage endCursor }
        nodes { url createdAt body }
      }
    }
  }
}
"""
BLOCKED_QUERY = """
query($owner: String!, $name: String!, $number: Int!, $c: String) {
  repository(owner: $owner, name: $name) {
    issue(number: $number) {
      blockedBy(first: 100, after: $c) {
        pageInfo { hasNextPage endCursor }
        nodes { number title state repository { nameWithOwner } }
      }
    }
  }
}
"""
TIMELINE_QUERY = """
query($owner: String!, $name: String!, $number: Int!, $c: String) {
  repository(owner: $owner, name: $name) {
    issue(number: $number) {
      timelineItems(first: 100, after: $c, itemTypes: [CROSS_REFERENCED_EVENT]) {
        pageInfo { hasNextPage endCursor }
        nodes {
          __typename
          ... on CrossReferencedEvent {
            source {
              __typename
              ... on PullRequest {
                number title url body state mergedAt repository { nameWithOwner }
              }
            }
          }
        }
      }
    }
  }
}
"""
COMMENTS_QUERY = """
query($owner: String!, $name: String!, $number: Int!, $c: String) {
  repository(owner: $owner, name: $name) {
    issue(number: $number) {
      comments(first: 100, after: $c) {
        pageInfo { hasNextPage endCursor }
        nodes { url createdAt body }
      }
    }
  }
}
"""


def fail(message: str, code: int) -> NoReturn:
    sys.stderr.write(json.dumps({"error": message}, ensure_ascii=False) + "\n")
    sys.exit(code)


def to_int(digits: str) -> int | None:
    """A whole number read from text: None past MAX_DIGITS digits, so that a huge run
    never reaches `int()` (which raises past 4300 digits) and names nothing."""
    significant = digits.lstrip("0") or "0"
    return int(significant) if len(significant) <= MAX_DIGITS else None


def parse_ref(text: str) -> tuple[str, int]:
    """`api#N | ui#N | OWNER/NAME#N | N` -> (nameWithOwner, number); else exit 2."""
    if NUMBER.fullmatch(text):
        return (ALIASES["api"], int(text))
    m = REF.fullmatch(text)
    if not m:
        shown = text if len(text) <= 60 else text[:60] + "…"
        fail(f"bad REF {shown!r}: expected api#N, ui#N, OWNER/NAME#N or N", 2)
    return (ALIASES.get(m.group(1), m.group(1)), int(m.group(2)))


# ─── transport ──────────────────────────────────────────────────────────────
def fixture_dir() -> str | None:
    """The offline seam, or None for live gh. Set but empty is a mistake: it must
    never fall through to a live call."""
    value = os.environ.get(FIXTURE_ENV)
    if value == "":
        fail(f"{FIXTURE_ENV} is set but empty", 3)
    return value


def request(name: str, query: str, repo: str, number: int, cursor: str | None) -> dict[str, Any]:
    """One GraphQL request: gh, or the fixture `<name>.json` when the seam is set."""
    seam = fixture_dir()
    gh_failure: str | None = None
    if seam is not None:
        path = Path(seam) / f"{name}.json"
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            fail(f"cannot read fixture {path}: {exc.strerror or exc}", 3)
        except ValueError as exc:
            fail(f"cannot read fixture {path}: {exc}", 3)
    else:
        owner, repo_name = repo.split("/", 1)
        argv = ["gh", "api", "graphql", "-f", f"query={query}"]
        argv += ["-f", f"owner={owner}", "-f", f"name={repo_name}", "-F", f"number={number}"]
        if cursor is not None:
            argv += ["-f", f"c={cursor}"]
        try:
            proc = subprocess.run(argv, capture_output=True, encoding="utf-8", timeout=GH_TIMEOUT)
        except subprocess.TimeoutExpired:
            fail(f"gh api graphql ({name}) timed out after {GH_TIMEOUT} s", 3)
        except OSError as exc:
            fail(f"cannot run gh: {exc}", 3)
        except ValueError as exc:  # gh output that is not UTF-8
            fail(f"gh api graphql ({name}): cannot read the output: {exc}", 3)
        if proc.returncode != 0:
            gh_failure = f"gh api graphql ({name}) exited {proc.returncode}: {proc.stderr.strip()}"
        raw = proc.stdout
    try:
        response = json.loads(raw)
    except json.JSONDecodeError as exc:
        fail(gh_failure or f"{name}: response is not JSON: {exc}", 3)
    if gh_failure:
        fail(gh_failure, 3)
    if not isinstance(response, dict):
        fail(f"{name}: response is not a JSON object", 3)
    errors = response.get("errors")
    if errors:
        messages = [e.get("message", e) if isinstance(e, dict) else e for e in errors]
        fail(f"{name}: GraphQL errors: {'; '.join(str(m) for m in messages)}", 3)
    data = response.get("data")
    if not isinstance(data, dict):
        fail(f"{name}: response has no data", 3)
    return data


# ─── shape helpers ──────────────────────────────────────────────────────────
def obj(value: Any, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        fail(f"unexpected shape: {where} is not an object", 3)
    return value


def text_of(node: dict[str, Any], key: str, where: str) -> str:
    value = node.get(key)
    if not isinstance(value, str):
        fail(f"unexpected shape: {where}.{key} is not a string", 3)
    return value


def opt_text(node: dict[str, Any], key: str, where: str) -> str | None:
    value = node.get(key)
    if value is not None and not isinstance(value, str):
        fail(f"unexpected shape: {where}.{key} is not a string or null", 3)
    return value


def moment(value: str, where: str) -> datetime:
    """A GitHub DateTime (`2026-10-29T12:34:56Z`, always UTC) as a naive UTC datetime."""
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        fail(f"unexpected shape: {where} is not a UTC timestamp", 3)


def nodes_of(connection: Any, where: str) -> tuple[list[dict[str, Any]], bool, str | None]:
    """(nodes, hasNextPage, endCursor) of a connection."""
    conn = obj(connection, where)
    nodes = conn.get("nodes")
    if not isinstance(nodes, list):
        fail(f"unexpected shape: {where}.nodes is not a list", 3)
    info = obj(conn.get("pageInfo"), f"{where}.pageInfo")
    cursor = info.get("endCursor")
    more = info.get("hasNextPage") is True
    if more and not isinstance(cursor, str):
        fail(f"unexpected shape: {where} has a next page but no endCursor", 3)
    return ([obj(n, f"{where}.nodes[]") for n in nodes], more, cursor)


def ref_str(repo: str, number: int) -> str:
    return f"{repo}#{number}"


# ─── fetch ──────────────────────────────────────────────────────────────────
def issue_of(data: dict[str, Any], repo: str, number: int) -> dict[str, Any]:
    repository = data.get("repository")
    node = obj(repository, "repository") if repository is not None else {}
    raw_issue = node.get("issue")
    if raw_issue is None:
        fail(f"issue {ref_str(repo, number)} not found", 3)
    return obj(raw_issue, "issue")


def follow_pages(
    first: Any, field: str, name: str, query: str, repo: str, number: int, where: str
) -> list[dict[str, Any]]:
    """Every node of a connection of the issue: the page already read, then the
    `<name>-p<k>` pages from the second on."""
    nodes, more, cursor = nodes_of(first, where)
    page = 1
    while more:
        page += 1
        if page > MAX_PAGES:
            fail(f"{where} has more than {MAX_PAGES} pages", 3)
        data = request(f"{name}-p{page}", query, repo, number, cursor)
        more_nodes, more, cursor = nodes_of(issue_of(data, repo, number).get(field), where)
        nodes.extend(more_nodes)
    return nodes


def fetch_issue(
    repo: str, number: int
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """The issue's own fields, every blocker, timeline node and comment, all pages."""
    issue = issue_of(request("issue-p1", ISSUE_QUERY, repo, number, None), repo, number)
    blocked = follow_pages(
        issue.get("blockedBy"),
        "blockedBy",
        "blocked",
        BLOCKED_QUERY,
        repo,
        number,
        "issue.blockedBy",
    )
    timeline = follow_pages(
        issue.get("timelineItems"),
        "timelineItems",
        "timeline",
        TIMELINE_QUERY,
        repo,
        number,
        "issue.timelineItems",
    )
    comments = follow_pages(
        issue.get("comments"),
        "comments",
        "comments",
        COMMENTS_QUERY,
        repo,
        number,
        "issue.comments",
    )
    return issue, blocked, timeline, comments


# ─── markdown scanning ──────────────────────────────────────────────────────
def visible_lines(text: str, *, skip_quotes: bool) -> Iterator[str]:
    """The lines of `text` that render as prose: outside fenced code and HTML
    comments (single- and multi-line; the text around a comment stays) and, when
    `skip_quotes`, outside blockquotes. Shared by every scan below so that all of
    them see the same text."""
    fence: str | None = None  # the opening run of the open fenced block
    in_comment = False
    for raw in text.splitlines():
        if fence is not None:
            closer = FENCE_CLOSE.match(raw)
            if closer and closer.group(1)[0] == fence[0] and len(closer.group(1)) >= len(fence):
                fence = None
            continue
        line = raw
        kept: list[str] = []
        while line:
            if in_comment:
                end = line.find("-->")
                if end < 0:
                    line = ""
                else:
                    in_comment, line = False, line[end + 3 :]
            else:
                start = line.find("<!--")
                if start < 0:
                    kept.append(line)
                    line = ""
                else:
                    kept.append(line[:start])
                    in_comment, line = True, line[start + 4 :]
        line = "".join(kept)
        if line != raw and not line.strip():
            continue  # nothing but comment on this line
        opener = FENCE_OPEN.match(line)
        if opener and not (opener.group(1)[0] == "`" and "`" in opener.group(2)):
            fence = opener.group(1)
            continue
        if skip_quotes and line.lstrip().startswith(">"):
            continue
        yield line


def section_lines(
    body: str, wanted: Callable[[str], bool], *, skip_quotes: bool
) -> list[str] | None:
    """The visible lines of the first section whose heading satisfies `wanted`, or
    None. A section ends at the next heading of the same or a higher level.
    Headings are ATX (`## Name`) only."""
    lines: list[str] | None = None
    inside = False
    level = 0
    for line in visible_lines(body, skip_quotes=skip_quotes):
        heading = HEADING.match(line)
        if heading:
            depth = len(heading.group(1))
            if inside and depth <= level:
                inside = False
            if lines is None and wanted(heading.group(2)):
                inside, level, lines = True, depth, []
            continue
        if inside and lines is not None:
            lines.append(line)
    return lines


def is_kickoff(heading: str) -> bool:
    return re.match(r"kickoff\b", heading.strip(), re.IGNORECASE) is not None


def is_what(heading: str) -> bool:
    """`What`, alone or followed by a blank or a dash (`What — Что изменено`)."""
    return re.match(r"what(?:$|[ \t\-–—])", heading.strip(), re.IGNORECASE) is not None


def named(heading: str) -> Callable[[str], bool]:
    return lambda text: text.strip().lower() == heading


def bullets(lines: list[str]) -> list[dict[str, Any]]:
    """Top-level list items {text, children}: a continuation line is joined to the
    item (or nested item) above it; a paragraph ends the item."""
    items: list[dict[str, Any]] = []
    open_item = False
    in_child = False
    for line in lines:
        if not line.strip():
            continue
        top = TOP_BULLET.match(line)
        if top:
            items.append({"text": top.group(1).strip(), "children": []})
            open_item, in_child = True, False
            continue
        nested = NESTED_BULLET.match(line)
        if nested and open_item:
            items[-1]["children"].append(nested.group(1).strip())
            in_child = True
            continue
        if line[0] in " \t" and open_item:
            if in_child:
                items[-1]["children"][-1] += " " + line.strip()
            else:
                items[-1]["text"] += " " + line.strip()
            continue
        open_item = False
    return items


def labelled(children: list[str], labels: tuple[str, ...]) -> str | None:
    for child in children:
        head, sep, rest = child.partition(":")
        if sep and head.strip().lower() in labels:
            return rest.strip() or None
    return None


# ─── the issue's facts ──────────────────────────────────────────────────────
def build_parts(body: str) -> tuple[list[dict[str, Any]], str | None]:
    """The parts of `## Scope` (without prs / status) and a numbering error, if any."""
    scope = section_lines(body, named("scope"), skip_quotes=True)
    grouped: dict[int, dict[str, Any]] = {}
    for item in bullets(scope or []):
        text = item["text"]
        if not PART_START.match(text):
            continue
        m = PART_BULLET.fullmatch(text)
        if not m:
            return [], f"Scope bullet {text[:60]!r} starts with a part number but has no ':'"
        n = to_int(m.group(1))
        if n is None:  # out of range: names no part
            continue
        part = grouped.setdefault(
            n,
            {
                "n": n,
                "label": None,
                "scope": [],
                "boundary": None,
                "done_when": None,
                "acceptance_criteria": [],
            },
        )
        part["label"] = part["label"] or (m.group(2) or "").strip() or None
        part["scope"].append(m.group(3).strip())
        part["boundary"] = part["boundary"] or labelled(item["children"], BOUNDARY_LABELS)
        part["done_when"] = part["done_when"] or labelled(item["children"], DONE_WHEN_LABELS)
    numbers = sorted(grouped)
    if numbers != list(range(1, len(numbers) + 1)):
        found = ", ".join(str(n) for n in numbers)
        return [], f"parts in Scope are not numbered 1..k contiguously (found: {found})"
    parts = [grouped[n] for n in numbers]
    for part in parts:
        part["scope"] = "\n".join(part["scope"])
    return parts, None


def build_criteria(body: str, parts: list[dict[str, Any]], warnings: list[str]) -> list[str]:
    """Fill `acceptance_criteria` of each part; return the criteria without a prefix."""
    by_number = {p["n"]: p for p in parts}
    common: list[str] = []
    section = section_lines(body, named("acceptance criteria"), skip_quotes=True)
    for item in bullets(section or []):
        text = CHECKBOX_PREFIX.sub("", item["text"], count=1)
        m = PART_CRITERION.fullmatch(text)
        if not m:
            common.append(text)
            continue
        n = to_int(m.group(1))
        if n is None:
            warnings.append("acceptance criterion names a part number out of range")
        elif n in by_number:
            by_number[n]["acceptance_criteria"].append(m.group(2).strip())
        else:
            warnings.append(f"acceptance criterion names Part {n}, not in Scope")
    return common


def blockers_of(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for i, node in enumerate(nodes):
        where = f"issue.blockedBy[{i}]"
        repository = obj(node.get("repository"), f"{where}.repository")
        number = node.get("number")
        if not isinstance(number, int):
            fail(f"unexpected shape: {where}.number is not an integer", 3)
        out.append(
            {
                "ref": ref_str(text_of(repository, "nameWithOwner", where), number),
                "title": text_of(node, "title", where),
                "state": text_of(node, "state", where),
            }
        )
    return sorted(out, key=lambda b: (b["ref"].split("#")[0].lower(), int(b["ref"].split("#")[1])))


def board_item(issue: dict[str, Any]) -> dict[str, Any] | None:
    """The issue's item on the course board, None when it is not on it."""
    nodes, more, _ = nodes_of(issue.get("projectItems"), "issue.projectItems")
    if more:
        fail("issue.projectItems has more than one page: the board item cannot be told", 3)
    for node in nodes:
        project = obj(node.get("project"), "issue.projectItems[].project")
        owner = obj(project.get("owner"), "issue.projectItems[].project.owner")
        if project.get("number") == BOARD_NUMBER and owner.get("login") == BOARD_OWNER:
            return node
    return None


def board_status(item: dict[str, Any] | None) -> str | None:
    """The Status of a board item, None when it has none."""
    if item is None or item.get("fieldValueByName") is None:
        return None
    value = obj(item["fieldValueByName"], "issue.projectItems[].fieldValueByName")
    return text_of(value, "name", "issue.projectItems[].fieldValueByName")


def assignees_of(issue: dict[str, Any]) -> list[str]:
    nodes = obj(issue.get("assignees"), "issue.assignees").get("nodes")
    if not isinstance(nodes, list):
        fail("unexpected shape: issue.assignees.nodes is not a list", 3)
    return sorted(text_of(obj(n, "issue.assignees[]"), "login", "issue.assignees[]") for n in nodes)


# ─── pull requests ──────────────────────────────────────────────────────────
def refs_of(title: str, body: str, pr_repo: str) -> set[tuple[str, int]]:
    """The issues the `Refs` lines of a PR's title and body name, as (repo lowercased,
    number): a bare `#N` means the PR's own repo. A number past MAX_DIGITS names none."""
    found: set[tuple[str, int]] = set()
    for part in (title, body):
        for line in visible_lines(part, skip_quotes=False):
            m = REFS_LINE.match(line)
            if not m:
                continue
            for token in REFS_TOKEN.finditer(m.group(1)):
                number = to_int(token.group(2))
                if number is not None:
                    found.add(((token.group(1) or pr_repo).lower(), number))
    return found


def target_of(repo: str | None, digits: str, pr_repo: str) -> tuple[str, int | None]:
    """The issue a ref names: (repo lowercased, number or None when out of range); a
    missing repo is the pull request's own."""
    owner = repo or pr_repo
    return (ALIASES.get(owner.lower(), owner).lower(), to_int(digits))


def tie_in_front(text: str, start: int, pr_repo: str) -> tuple[str, int | None] | None:
    """The ref that `text` has right in front of the label at `start` (`#168 part 6`,
    `**api#168** part 6`), or None. Only a window before the label is searched, from its
    last `#`, so that a long line never costs more than the window per mention."""
    head = text[max(0, start - PRE_WINDOW) : start]
    at = head.rfind("#")
    if at < 0:
        return None
    m = PRE_REF.search(head, max(0, at - 80))
    return target_of(m.group("repo"), m.group("n"), pr_repo) if m else None


def mentions(
    text: str, pr_repo: str, issue: tuple[str, int], multi: bool
) -> list[tuple[str, list[int]]]:
    """Every part mention of `text`, first by position, as (kind, numbers). Kinds: `range`
    (no numbers); `part`, a mention that belongs to `issue`; `other`, one that does not (it
    names nothing). A mention is a label and a number, and what follows it: a range when the
    lower end is below the upper one; after a plural label a list of numbers (all of them
    are named); `of <ref>`, which decides whose it is. A pull request whose Refs name
    several issues (`multi`) has to tie a mention to the issue: `of <ref>` after it or a ref
    in front of it; an untied mention is `other`. Elsewhere an untied mention is `part`. A
    number past MAX_DIGITS names nothing."""
    me = (issue[0].lower(), issue[1])
    found: list[tuple[str, list[int]]] = []
    pos = 0
    while True:
        m = LABEL_AT.search(text, pos)
        if m is None:
            return found
        pos = m.end()
        label = m.group("label").strip().lower()
        low = to_int(m.group("k"))
        tail = RANGE_TAIL.match(text, pos)
        if tail is not None:
            high = to_int(tail.group("m"))
            if low is not None and high is not None and low < high:
                found.append(("range", []))
                pos = tail.end()
                continue
        numbers = [low]
        if label in PLURAL_LABELS:
            while (more := LIST_TAIL.match(text, pos)) is not None:
                numbers.append(to_int(more.group("m")))
                pos = more.end()
        if len(numbers) == 1 and not (label in SINGLE_LABELS or label.startswith("ч.")):
            continue  # `Parts 1` alone is not a mention
        named = [n for n in dict.fromkeys(numbers) if n is not None]
        of = OF_TAIL.match(text, pos)
        if of is not None:
            pos = of.end()
            tie: tuple[str, int | None] | None = target_of(of.group("repo"), of.group("n"), pr_repo)
        else:
            tie = tie_in_front(text, m.start(), pr_repo) if multi else None
        mine = tie == me if tie is not None else not multi
        found.append(("part" if mine and named else "other", named))


def title_parts(title: str, pr_repo: str, issue: tuple[str, int], multi: bool) -> list[int]:
    """Every part a title names. A range anywhere in it makes the title name no part
    («Parts 1–2 (Part 3 follows)» is not a title of Part 3); a «Part K of <another
    issue>» is skipped, as if it were not there."""
    numbers: list[int] = []
    for kind, listed in mentions(title, pr_repo, issue, multi):
        if kind == "range":
            return []
        if kind == "part":
            numbers.extend(n for n in listed if n not in numbers)
    return numbers


def what_parts(line: str, pr_repo: str, issue: tuple[str, int], multi: bool) -> list[int]:
    """The parts of the first mention of a line (a list names all of them). When that
    mention is a range, or a «Part K of <another issue>», the line names no part: a single
    mention after it («Parts 1–2 of #13: both. Part 3 is next.») is context."""
    found = mentions(line, pr_repo, issue, multi)
    return found[0][1] if found and found[0][0] == "part" else []


def first_line(body: str) -> str:
    """The first non-empty line of the `## What` section, or of the whole body when
    it has no `## What` heading."""
    what = section_lines(body, is_what, skip_quotes=False)
    lines = what if what is not None else list(visible_lines(body, skip_quotes=False))
    return next((line for line in lines if line.strip()), "")


def parts_named(
    title: str, body: str, pr_repo: str, issue: tuple[str, int]
) -> tuple[list[int], str]:
    """The parts a PR names for `issue`, and where: `title` or `What`. In its title every
    part counts: a title that names several is deliberate (a title with a range names
    none). In the first line of its What only the first mention does, the rest is context
    ("Part 1, which this extends, is merged in #357"), and a range there names none. A
    «Part K of <ref>» whose ref is another issue is not a part of this one."""
    multi = len(refs_of(title, body, pr_repo)) >= 2
    in_title = title_parts(title, pr_repo, issue, multi)
    if in_title:
        return in_title, "title"
    return what_parts(first_line(body), pr_repo, issue, multi), "What"


def build_prs(timeline: list[dict[str, Any]], issue: tuple[str, int]) -> list[dict[str, Any]]:
    """The pull requests of the timeline with a `Refs` line naming the issue,
    deduplicated, sorted by (repo, number); `named` is the parts each one names (`named_in`:
    where), `merged_at` the time a MERGED one was merged."""
    found: dict[tuple[str, int], dict[str, Any]] = {}
    for i, node in enumerate(timeline):
        if node.get("__typename") != "CrossReferencedEvent":
            continue
        where = f"timeline[{i}]"
        source = obj(node.get("source"), f"{where}.source")
        if source.get("__typename") != "PullRequest":
            continue
        pr_repo = text_of(
            obj(source.get("repository"), f"{where}.source.repository"),
            "nameWithOwner",
            f"{where}.source.repository",
        )
        pr_number = source.get("number")
        if not isinstance(pr_number, int):
            fail(f"unexpected shape: {where}.source.number is not an integer", 3)
        key = (pr_repo.lower(), pr_number)
        if key in found:
            continue
        title = text_of(source, "title", f"{where}.source")
        body = opt_text(source, "body", f"{where}.source") or ""
        if (issue[0].lower(), issue[1]) not in refs_of(title, body, pr_repo):
            continue
        state = text_of(source, "state", f"{where}.source")
        merged_at = opt_text(source, "mergedAt", f"{where}.source")
        if state == "MERGED" and merged_at is None:
            fail(f"unexpected shape: {where}.source.mergedAt is null for a MERGED pull request", 3)
        named, named_in = parts_named(title, body, pr_repo, issue)
        found[key] = {
            "ref": ref_str(pr_repo, pr_number),
            "url": text_of(source, "url", f"{where}.source"),
            "title": title,
            "state": state,
            "named": named,
            "named_in": named_in,
            "merged_at": moment(merged_at, f"{where}.source.mergedAt") if merged_at else None,
        }
    return [found[key] for key in sorted(found)]


def public(pr: dict[str, Any]) -> dict[str, Any]:
    return {k: pr[k] for k in ("ref", "url", "title", "state")}


def status_of(prs: list[dict[str, Any]]) -> str:
    states = {pr["state"] for pr in prs}
    return "done" if "MERGED" in states else "open_pr" if "OPEN" in states else "todo"


def listing(prs: list[dict[str, Any]]) -> str:
    return ", ".join(f"{pr['ref']} ({pr['state']})" for pr in prs)


# ─── the decision ───────────────────────────────────────────────────────────
def acceptance_comments(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The comments accept-issue wrote (a body that starts with one of its markers), in
    the order of the issue, as {kind, url, created_at}."""
    out = []
    for i, node in enumerate(nodes):
        where = f"issue.comments[{i}]"
        body = opt_text(node, "body", where) or ""
        kind = next((k for mark, k in ACCEPT_MARKS.items() if body.lstrip().startswith(mark)), None)
        if kind is not None:
            out.append(
                {
                    "kind": kind,
                    "url": text_of(node, "url", where),
                    "created_at": moment(text_of(node, "createdAt", where), f"{where}.createdAt"),
                }
            )
    return out


def returned_at_acceptance(
    comments: list[dict[str, Any]], prs: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """The latest accept-issue comment when it is `returned` and no pull request with
    Refs was merged after it, else None."""
    if not comments or comments[-1]["kind"] != "returned":
        return None
    latest = comments[-1]
    if any(pr["merged_at"] is not None and pr["merged_at"] > latest["created_at"] for pr in prs):
        return None
    return latest


def acceptance_next(
    reason: str, returned: dict[str, Any] | None, has_parts: bool
) -> tuple[None, list[str], str]:
    """The work is done: `accept` (acceptance is next), or `stop` when the issue came back
    from acceptance and nothing has been merged since."""
    if returned is None:
        return None, [reason], "accept"
    back = (
        f"the issue was returned at acceptance ({returned['url']}) and no pull request with "
        "Refs has been merged since"
    )
    if has_parts:
        return None, [f"{back} — rework a part with --part K"], "stop"
    return None, [f"{back} — the rework of a task without parts is manual: ask the user"], "stop"


def decide(
    facts: dict[str, Any],
    part: int | None,
    numbering_error: str | None,
    stray_open: list[dict[str, Any]],
    returned: dict[str, Any] | None,
) -> tuple[dict[str, Any] | None, list[str], str]:
    """(selected, reasons, verdict): the verdict is go, stop or accept; reasons non-empty
    means it is not a go. `stray_open` are the OPEN pull requests with `Refs` that name no
    part of an issue that has parts; `returned` is the accept-issue comment that sent the
    issue back, if no pull request was merged after it."""
    if facts["issue"]["state"] == "CLOSED":
        return (
            None,
            [f"issue {facts['issue']['repo']}#{facts['issue']['number']} is CLOSED"],
            "stop",
        )
    open_blockers = [b for b in facts["blockers"] if b["state"] == "OPEN"]
    if open_blockers:
        return None, [f"blocked by {b['ref']} — {b['title']}" for b in open_blockers], "stop"
    if numbering_error:
        return None, [numbering_error], "stop"
    parts = facts["parts"]
    if part is not None:
        if not any(p["n"] == part for p in parts):
            have = f"parts 1..{len(parts)}" if parts else "no parts"
            ref = f"{facts['issue']['repo']}#{facts['issue']['number']}"
            fail(f"--part {part}: issue {ref} has {have}", 2)
        return {"part": part, "reason": "explicit"}, [], "go"
    if not parts:
        live = [pr for pr in facts["task_prs"] if pr["state"] in ("OPEN", "MERGED")]
        if any(pr["state"] == "OPEN" for pr in live):
            return (
                None,
                [
                    "the task has no parts and already has a pull request with Refs: "
                    f"{listing(live)} — a rework PR is a human decision"
                ],
                "stop",
            )
        if live:
            reason = (
                f"the task has no parts and its pull request with Refs is merged: {listing(live)}"
                " — acceptance is next"
            )
            return acceptance_next(reason, returned, has_parts=False)
        return {"part": None, "reason": "whole-task"}, [], "go"
    if stray_open:
        return (
            None,
            [
                "open pull requests with Refs that name no part of this issue: "
                f"{listing(stray_open)} — say which part each covers, or pass --part"
            ],
            "stop",
        )
    for p in parts:
        if p["status"] == "done":
            continue
        if p["status"] == "open_pr":
            open_prs = [pr for pr in p["prs"] if pr["state"] == "OPEN"]
            return (
                None,
                [
                    f"Part {p['n']} already has an open pull request: {listing(open_prs)}"
                    " — finish it or pass --part"
                ],
                "stop",
            )
        return {"part": p["n"], "reason": "first-todo"}, [], "go"
    # Every part has a merged pull request. A part may also have an open one (a rework):
    # acceptance waits for it.
    reworks = {pr["ref"]: pr for p in parts for pr in p["prs"] if pr["state"] == "OPEN"}
    if reworks:
        return (
            None,
            [
                "every part has a merged pull request, but pull requests are still open: "
                f"{listing(list(reworks.values()))} — finish them first"
            ],
            "stop",
        )
    reason = "every part has a merged pull request — acceptance is next"
    return acceptance_next(reason, returned, has_parts=True)


def collect(repo: str, number: int, part: int | None) -> dict[str, Any]:
    issue, blocked, timeline, comments = fetch_issue(repo, number)
    body = issue.get("body")
    if body is not None and not isinstance(body, str):
        fail("unexpected shape: issue.body is not a string or null", 3)
    body = body or ""
    warnings: list[str] = []

    parts, numbering_error = build_parts(body)
    common = build_criteria(body, parts, warnings)
    prs = build_prs(timeline, (repo, number))
    for p in parts:
        p["prs"] = [public(pr) for pr in prs if p["n"] in pr["named"]]
        p["status"] = status_of(p["prs"])
    numbers = {p["n"] for p in parts}
    unnamed = [pr for pr in prs if not numbers & set(pr["named"])]
    if parts and unnamed:
        warnings.append(
            "pull requests with Refs that name no part of this issue: " + listing(unnamed)
        )
    for pr in prs if parts else []:
        if len(pr["named"]) > 1:
            named = ", ".join(str(n) for n in pr["named"])
            warnings.append(
                f"pull request {pr['ref']} names several parts in its {pr['named_in']} ({named}):"
                " it counts for each of them"
            )

    kickoff_lines = section_lines(body, is_kickoff, skip_quotes=False)
    kickoff_text = "\n".join(kickoff_lines or []).strip()
    if not kickoff_text:
        warnings.append("the issue has no Kickoff section: read its Background and References")
    item = board_item(issue)
    if item is None:
        warnings.append(f"the issue is not on board {BOARD_NUMBER}: the Status step will fail")

    facts: dict[str, Any] = {
        "schema": SCHEMA,
        "issue": {
            "repo": repo,
            "number": number,
            "title": text_of(issue, "title", "issue"),
            "url": text_of(issue, "url", "issue"),
            "state": text_of(issue, "state", "issue"),
            "assignees": assignees_of(issue),
            "board_status": board_status(item),
        },
        "blockers": blockers_of(blocked),
        "parts": parts,
        "common_acceptance_criteria": common,
        "task_prs": [] if parts else [public(pr) for pr in prs],
        "kickoff": {"present": bool(kickoff_text), "text": kickoff_text},
    }
    stray_open = [pr for pr in unnamed if pr["state"] == "OPEN"]
    returned = returned_at_acceptance(acceptance_comments(comments), prs)
    selected, reasons, verdict = decide(facts, part, numbering_error, stray_open, returned)
    if selected is not None and selected["reason"] == "explicit":
        chosen = next(p for p in parts if p["n"] == selected["part"])
        if chosen["status"] != "todo":
            kind = "a merged" if chosen["status"] == "done" else "an open"
            warnings.append(
                f"Part {chosen['n']} already has {kind} pull request: {listing(chosen['prs'])}"
            )
    facts["selected"] = selected
    facts["verdict"] = verdict
    facts["reasons"] = reasons
    facts["warnings"] = warnings
    return facts


# ─── main ───────────────────────────────────────────────────────────────────
class Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        fail(message, 2)


def main(argv: list[str] | None = None) -> int:
    parser = Parser(prog="preflight.py", description="Pre-start check of one issue, as JSON.")
    parser.add_argument(
        "ref", metavar="REF", help="api#N | ui#N | OWNER/NAME#N | N (bare N = api#N)"
    )
    parser.add_argument("--part", type=int, metavar="K", help="take part K (1-based) explicitly")
    args = parser.parse_args(argv)
    if args.part is not None and args.part < 1:
        fail(f"--part {args.part}: parts are numbered from 1", 2)
    repo, number = parse_ref(args.ref)
    facts = collect(repo, number, args.part)
    sys.stdout.write(json.dumps(facts, sort_keys=True, indent=2, ensure_ascii=False) + "\n")
    return 0 if facts["verdict"] == "go" else 1


if __name__ == "__main__":
    sys.exit(main())
