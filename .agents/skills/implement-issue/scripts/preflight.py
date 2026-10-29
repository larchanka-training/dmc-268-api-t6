#!/usr/bin/env python3
"""preflight.py — can this GitHub issue be taken, and which part of it?

The pre-start check of the implement-issue skill. It collects what the skill needs
before any branch is cut and states it as facts plus a verdict: the issue's state,
its native blockers, its parts («Ч.N» / «Part N» / «Teil N») with the pull requests that
reference them, the Kickoff, the board Status, and the part a run should take.

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
    verdict   go | stop; reasons: why it stops; warnings: what to know on a go.

A pull request counts only when its title or body has a `Refs` line naming this
issue: a line starting with `Refs` (any case, an optional list bullet, an optional
colon) followed by `#N` or `OWNER/NAME#N` references separated by commas, `;`,
`&`, `and` or blanks; `#N` counts for a pull request in the issue's own repo, the
full form from any repo. Anything else is not recognised. Pull requests come from
the issue's timeline (both repos, every page). A pull request names the parts
found in its TITLE (`Part K`, `Ч.K`, `Ч. K`, `Teil K`, and `Часть K` in any case form:
`Части K`, `Частью K`; case-insensitive, whole numbers, any horizontal whitespace
after the label), all of them: a title that names several is deliberate. A range
(`Parts 1–3`, `Part 1-3`, `Части 1–3`: the dash is `-`, `–` or `—`, the upper end a
whole number not followed by a letter, a digit or `-`) names no single part: a title
with a range anywhere names none. If the title names none, the FIRST part named in the
first non-empty line of its `## What` section (the whole body when it has no `## What`):
a later mention there is context ("Part 1, which this extends, is merged"), not scope,
and when that first mention is a range the line names no part. A pull request that
ends up naming more than one part counts for each and adds a warning. Lines in fenced
code and HTML comments never count. A quoted line never counts as a `Refs` line (it
does not start with `Refs`), but quotes are not skipped for the first line of `## What`:
a pull request that opens with a quote is read from the quote.

Decisions, in this order: the issue is CLOSED -> stop; any OPEN blocker -> stop;
parts not numbered 1..k -> stop; `--part K` must exist (exit 2) and is taken
as is; no `--part`: an OPEN pull request with `Refs` that names no part of the
issue stops (whichever part it belongs to is not known: "say which part it covers,
or pass --part"), else the first part that is not done — open_pr stops, todo is
taken, all done stops; no parts: the whole task, unless a pull request with
`Refs` is OPEN or MERGED (stop). With `--part K`, or when the pull request that
names no part is MERGED or CLOSED, it is only a warning.

Exit 0 go; 1 stop (the document is still on stdout); 2 usage, bad REF or a
`--part` the issue does not have; 3 gh / API error (non-zero gh exit, timeout,
GraphQL errors, issue not found, unexpected shape, missing fixture, fixture dir
set but empty). On 2 and 3 stderr carries one JSON line {"error": "..."} and
stdout stays empty. Read-only: the only requests are `gh api graphql` queries.

Offline seam: with IMPLEMENT_ISSUE_FIXTURE_DIR set, gh is never called and every
request reads <dir>/<name>.json (the full {"data": ...} response) instead; set
but empty exits 3, never falls through to gh. Names: issue-p1 (the issue, its
first blocker page and its first timeline page), blocked-p<k> and timeline-p<k>
(the follow-up pages, the first is p2).
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

REF = re.compile(r"(api|ui|[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#([1-9][0-9]*)")
NUMBER = re.compile(r"[1-9][0-9]*")

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
# (ru), `Part K` (en), `Teil K` (de) — a whole number, so `Ч.1` is not `Ч.12`.
_LABEL = r"(?:Ч\.[ \t]*|Part[ \t]+|Teil[ \t]+)"
_PART = rf"{_LABEL}([0-9]+)(?![0-9])"
# The same in a pull request's prose: horizontal whitespace of any kind (a no-break space
# included) after the label, and «Часть K» too (in any case form: «Части K», «Частью K»).
# A range — «Parts 1–3», «Части 1–3», «Part 1-3», the dash `-`, `–` or `—` — is an alternative
# of its own, with the plural labels, so that it is the match "first by position" lands on
# and a later single mention cannot stand in for it. Its upper end is a whole number not
# followed by a letter, a digit or `-`: «Part 1 – 2nd step» and «Part 1 — 2026-10-29» are
# not ranges.
_H = r"[^\S\r\n]"  # horizontal whitespace
_SINGLE_LABEL = rf"(?:Ч\.{_H}*|(?:Part|Teil|Част(?:ь|и|ью)){_H}+)"
_RANGE_LABEL = rf"(?:Ч\.{_H}*|(?:Parts?|Teil(?:e|en)?|Част(?:ь|и|ью|ей|ям|ями|ях)){_H}+)"
_RANGE = rf"{_RANGE_LABEL}[0-9]+{_H}*[-–—]{_H}*[0-9]+(?![^\W_]|-)"
_SINGLE = rf"{_SINGLE_LABEL}(?P<n>[0-9]+)(?![0-9])"
PART_NAMED = re.compile(rf"(?<!\w)(?:(?P<range>{_RANGE})|{_SINGLE})", re.IGNORECASE)
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
              ... on PullRequest { number title url body state repository { nameWithOwner } }
            }
          }
        }
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
              ... on PullRequest { number title url body state repository { nameWithOwner } }
            }
          }
        }
      }
    }
  }
}
"""


def fail(message: str, code: int) -> NoReturn:
    sys.stderr.write(json.dumps({"error": message}, ensure_ascii=False) + "\n")
    sys.exit(code)


def parse_ref(text: str) -> tuple[str, int]:
    """`api#N | ui#N | OWNER/NAME#N | N` -> (nameWithOwner, number); else exit 2."""
    if NUMBER.fullmatch(text):
        return (ALIASES["api"], int(text))
    m = REF.fullmatch(text)
    if not m:
        fail(f"bad REF {text!r}: expected api#N, ui#N, OWNER/NAME#N or N", 2)
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
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    """The issue's own fields, every blocker and every timeline node, all pages."""
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
    return issue, blocked, timeline


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
        n = int(m.group(1))
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
        elif int(m.group(1)) in by_number:
            by_number[int(m.group(1))]["acceptance_criteria"].append(m.group(2).strip())
        else:
            warnings.append(f"acceptance criterion names Part {m.group(1)}, not in Scope")
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
def names_issue(title: str, body: str, pr_repo: str, issue: tuple[str, int]) -> bool:
    """True when the PR title or body has a `Refs` line naming `issue`: a bare
    `#N` means the PR's own repo, so it counts only when that is the issue's repo."""
    repo, number = issue
    for part in (title, body):
        for line in visible_lines(part, skip_quotes=False):
            m = REFS_LINE.match(line)
            if not m:
                continue
            for token in REFS_TOKEN.finditer(m.group(1)):
                token_repo = (token.group(1) or pr_repo).lower()
                if token_repo == repo.lower() and int(token.group(2)) == number:
                    return True
    return False


def mentions(text: str) -> list[int | None]:
    """Every part mention of `text`, first by position: its number, None for a range."""
    return [None if m.group("range") else int(m.group("n")) for m in PART_NAMED.finditer(text)]


def title_parts(title: str) -> list[int]:
    """Every part a title names. A range anywhere in it makes the title name no part:
    «Parts 1–2 (Part 3 follows)» is not a title of Part 3."""
    numbers: list[int] = []
    for n in mentions(title):
        if n is None:
            return []
        if n not in numbers:
            numbers.append(n)
    return numbers


def what_parts(line: str) -> list[int]:
    """The first part a line names. When that is a range the line names no part: a single
    mention after it («Parts 1–2 of #13: both. Part 3 is next.») is context."""
    found = mentions(line)
    return [found[0]] if found and found[0] is not None else []


def first_line(body: str) -> str:
    """The first non-empty line of the `## What` section, or of the whole body when
    it has no `## What` heading."""
    what = section_lines(body, named("what"), skip_quotes=False)
    lines = what if what is not None else list(visible_lines(body, skip_quotes=False))
    return next((line for line in lines if line.strip()), "")


def parts_named(title: str, body: str) -> list[int]:
    """The parts a PR names. In its title every part counts: a title that names
    several is deliberate (a title with a range names none). In the first line of its
    What only the first part named does, the rest is context ("Part 1, which this
    extends, is merged in #357"), and a range there names none."""
    return title_parts(title) or what_parts(first_line(body))


def build_prs(timeline: list[dict[str, Any]], issue: tuple[str, int]) -> list[dict[str, Any]]:
    """The pull requests of the timeline with a `Refs` line naming the issue,
    deduplicated, sorted by (repo, number); `named` is the parts each one names."""
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
        if not names_issue(title, body, pr_repo, issue):
            continue
        found[key] = {
            "ref": ref_str(pr_repo, pr_number),
            "url": text_of(source, "url", f"{where}.source"),
            "title": title,
            "state": text_of(source, "state", f"{where}.source"),
            "named": parts_named(title, body),
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
def decide(
    facts: dict[str, Any],
    part: int | None,
    numbering_error: str | None,
    stray_open: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, list[str]]:
    """(selected, reasons): reasons non-empty means stop. `stray_open` are the OPEN
    pull requests with `Refs` that name no part of an issue that has parts."""
    if facts["issue"]["state"] == "CLOSED":
        return None, [f"issue {facts['issue']['repo']}#{facts['issue']['number']} is CLOSED"]
    open_blockers = [b for b in facts["blockers"] if b["state"] == "OPEN"]
    if open_blockers:
        return None, [f"blocked by {b['ref']} — {b['title']}" for b in open_blockers]
    if numbering_error:
        return None, [numbering_error]
    parts = facts["parts"]
    if part is not None:
        if not any(p["n"] == part for p in parts):
            have = f"parts 1..{len(parts)}" if parts else "no parts"
            ref = f"{facts['issue']['repo']}#{facts['issue']['number']}"
            fail(f"--part {part}: issue {ref} has {have}", 2)
        return {"part": part, "reason": "explicit"}, []
    if not parts:
        live = [pr for pr in facts["task_prs"] if pr["state"] in ("OPEN", "MERGED")]
        if live:
            return None, [
                f"the task has no parts and already has a pull request with Refs: {listing(live)}"
                " — a rework PR is a human decision"
            ]
        return {"part": None, "reason": "whole-task"}, []
    if stray_open:
        return None, [
            "open pull requests with Refs that name no part of this issue: "
            f"{listing(stray_open)} — say which part each covers, or pass --part"
        ]
    for p in parts:
        if p["status"] == "done":
            continue
        if p["status"] == "open_pr":
            open_prs = [pr for pr in p["prs"] if pr["state"] == "OPEN"]
            return None, [
                f"Part {p['n']} already has an open pull request: {listing(open_prs)}"
                " — finish it or pass --part"
            ]
        return {"part": p["n"], "reason": "first-todo"}, []
    return None, ["every part has a merged pull request — acceptance is next"]


def collect(repo: str, number: int, part: int | None) -> dict[str, Any]:
    issue, blocked, timeline = fetch_issue(repo, number)
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
                f"pull request {pr['ref']} names several parts in its title ({named}):"
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
    selected, reasons = decide(facts, part, numbering_error, stray_open)
    if selected is not None and selected["reason"] == "explicit":
        chosen = next(p for p in parts if p["n"] == selected["part"])
        if chosen["status"] != "todo":
            kind = "a merged" if chosen["status"] == "done" else "an open"
            warnings.append(
                f"Part {chosen['n']} already has {kind} pull request: {listing(chosen['prs'])}"
            )
    facts["selected"] = selected
    facts["verdict"] = "stop" if reasons else "go"
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
