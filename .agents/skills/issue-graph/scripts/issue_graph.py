#!/usr/bin/env python3
"""issue_graph.py — a sprint dependency graph report from GitHub issue relations.

Reads a scope (a milestone across repos, an epic's direct sub-issues, or an
explicit list of issues) through `gh api graphql`, builds the blocking graph
from every in-scope issue's `blockedBy` / `blocking` relations and prints one
Markdown report: a Mermaid flowchart, cycles, In Progress issues waiting on an
open blocker, Kahn layers of what can be taken in parallel, the critical path,
issues without links and the issues outside the scope.

Usage
-----
    issue_graph.py --milestone TITLE [--repo OWNER/NAME ...]
    issue_graph.py --epic REF
    issue_graph.py --issues REF [REF ...]
    common: [--project-owner ORG] [--project NUMBER] [--publish-to N|REF]
    REF := api#N | ui#N | OWNER/NAME#N; a bare N for --publish-to means api#N

With --publish-to the report also goes to that issue as one comment (gh api
REST) whose first line is the marker `<!-- issue-graph: <scope key> -->`: the
oldest comment carrying it is updated in place, else a new one is created. The
search reads at most 100 pages of 100 comments. Without --publish-to no REST
call is made.

Contract
--------
stdout carries the report only (Markdown, Russian, ends with one newline); it is
written before any REST call. stderr carries one JSON line: the error
{"error": "..."}, or after a successful publish {"published": "created" |
"updated", "url": "<html_url>"}. Exit 0 report printed (and published); 1
report printed and the graph has a cycle (never published); 2 usage or input
(bad REF or --publish-to target, milestone found in no repo, epic or issue not
resolvable — including a response whose `errors` are all NOT_FOUND —, publish
target issue or comment not found (REST 404), comment over GitHub's
65536-character limit); 3 gh / GraphQL / REST failure (non-zero gh exit without
such a response, gh timeout after 120 s, any other `errors`, missing fixture,
unexpected shape, ISSUE_GRAPH_FIXTURE_DIR set but empty, a write in fixture mode
without ISSUE_GRAPH_WRITE_LOG, more than 10000 comments without the marker, a
marked comment this account cannot edit (REST 403 — never a new comment instead)).

Offline seam: with ISSUE_GRAPH_FIXTURE_DIR set, gh is never called and every
request reads <dir>/<name>.json (the full {"data": ...} response) instead; set
but empty exits 3 before any request, never falls through to gh:
milestones-<alias>, milestone-<alias>-<m>-p<k>, epic-<alias>-<n>-p<k>,
issues-<alias>, node-<id>-<field>-p<k> (the first follow-up page is p2).
REST reads the response body itself: comments-<alias>-<n>-p<k> (a JSON array,
or the 404 object {"status": "404"}), comment-created-<alias>-<n> and
comment-updated-<id>. A write first appends one JSON line {"method", "path",
"body"} to the file ISSUE_GRAPH_WRITE_LOG names; without it a write exits 3, so
fixtures can never post a fake graph.
"""
# SYNC: mirrored in dmc-268-{ui,api}-t6/.agents/skills/issue-graph/scripts/issue_graph.py

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn

Key = tuple[str, int]  # (nameWithOwner, number): every sort in the report uses it
Edge = tuple[Key, Key]  # (blocker, blocked)

ALIASES = {
    "api": "larchanka-training/dmc-268-api-t6",
    "ui": "larchanka-training/dmc-268-ui-t6",
}
DISPLAY = {repo: alias for alias, repo in ALIASES.items()}
DEFAULT_REPOS = [ALIASES["api"], ALIASES["ui"]]
DEFAULT_PROJECT_OWNER = "larchanka-training"
DEFAULT_PROJECT = 12
FIXTURE_ENV = "ISSUE_GRAPH_FIXTURE_DIR"
WRITE_LOG_ENV = "ISSUE_GRAPH_WRITE_LOG"

REPO_NAME = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
REF = re.compile(r"(api|ui|[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#([1-9][0-9]*)")
NUMBER = re.compile(r"[1-9][0-9]*")
LEADING_TAGS = re.compile(r"^(?:\s*\[[^\]]*\])+")
# Mermaid entity codes, applied in one pass: a sequential replace would turn
# the `#` of an already escaped `#amp;` into `#35;amp;`.
MERMAID_ESCAPES = str.maketrans(
    {"&": "#amp;", "#": "#35;", '"': "#quot;", "<": "#lt;", ">": "#gt;"}
)
LABEL_WIDTH = 40
# GitHub autolinks `#<digit>` and mentions `@<login>` even after `\` or `&#35;`;
# only a zero-width space between them breaks it (literal entity text, not U+200B).
AUTOLINK = re.compile(r"#(?=[0-9])|@(?=[A-Za-z0-9])")
ZERO_WIDTH = "&#8203;"
BACKTICKS = re.compile(r"`+")
GH_TIMEOUT = 120  # seconds per gh call
WRITE_TIMEOUT = "; the comment may have been written — re-run to update it"
REVIEW_STATUSES = ("On Review", "AC Check")
IN_PROGRESS = "In Progress"
CLASS_DEFS = (
    "classDef done fill:#f1f3f5,stroke:#adb5bd,color:#868e96",
    "classDef progress fill:#e7f5ff,stroke:#1c7ed6,color:#212529",
    "classDef review fill:#fff9db,stroke:#f08c00,color:#212529",
    "classDef alarm fill:#fff5f5,stroke:#e03131,stroke-width:3px,color:#212529",
    "classDef external fill:#ffffff,stroke:#868e96,stroke-dasharray:5 5,color:#212529",
)
CLASS_ORDER = ("done", "progress", "review", "alarm", "external")
EXTERNAL = " (вне выборки)"
COMMENTS_PAGE = 100  # REST per_page: a shorter page is the last one
COMMENTS_MAX_PAGES = 100  # the marker search stops after this many full pages
COMMENT_LIMIT = 65536  # GitHub's comment body limit, characters
FOOTER = (
    "<sub>Сгенерировано скиллом issue-graph (.agents/skills/issue-graph); "
    "повторный запуск с тем же охватом обновит этот комментарий.</sub>"
)

# ─── GraphQL ────────────────────────────────────────────────────────────────
_RELATION = (
    "totalCount pageInfo { hasNextPage endCursor } "
    "nodes { number title state repository { nameWithOwner } }"
)
ISSUE_FRAGMENT = (
    "fragment IssueFields on Issue {\n"
    "  id number title state repository { nameWithOwner }\n"
    "  parent { number repository { nameWithOwner } }\n"
    f"  blockedBy(first: 50) {{ {_RELATION} }}\n"
    f"  blocking(first: 50) {{ {_RELATION} }}\n"
    "  projectItems(first: 20) { nodes {\n"
    "    project { number owner { ... on Organization { login } ... on User { login } } }\n"
    '    fieldValueByName(name: "Status") {\n'
    "      ... on ProjectV2ItemFieldSingleSelectValue { name }\n"
    "    }\n"
    "  } }\n"
    "}\n"
)
# The `query:` search is fuzzy and not paged: the exact title is filtered after the fetch,
# and 20 candidates is enough for a sprint title.
MILESTONES_QUERY = """query($owner: String!, $name: String!, $title: String!) {
  repository(owner: $owner, name: $name) {
    milestones(first: 20, query: $title, states: [OPEN, CLOSED]) { nodes { number title } }
  }
}
"""
MILESTONE_QUERY = (
    """query($owner: String!, $name: String!, $number: Int!, $after: String) {
  repository(owner: $owner, name: $name) {
    milestone(number: $number) {
      issues(first: 50, after: $after) {
        pageInfo { hasNextPage endCursor }
        nodes { ...IssueFields }
      }
    }
  }
}
"""
    + ISSUE_FRAGMENT
)
EPIC_QUERY = (
    """query($owner: String!, $name: String!, $number: Int!, $after: String) {
  repository(owner: $owner, name: $name) {
    issue(number: $number) {
      title
      subIssues(first: 50, after: $after) {
        pageInfo { hasNextPage endCursor }
        nodes { ...IssueFields }
      }
    }
  }
}
"""
    + ISSUE_FRAGMENT
)


def issues_query(numbers: list[int]) -> str:
    aliases = "".join(f"    i{n}: issue(number: {n}) {{ ...IssueFields }}\n" for n in numbers)
    return (
        "query($owner: String!, $name: String!) {\n"
        "  repository(owner: $owner, name: $name) {\n"
        f"{aliases}"
        "  }\n"
        "}\n" + ISSUE_FRAGMENT
    )


def relation_query(field: str) -> str:
    return (
        "query($id: ID!, $after: String) {\n"
        "  node(id: $id) {\n"
        "    ... on Issue {\n"
        f"      {field}(first: 50, after: $after) {{\n"
        "        pageInfo { hasNextPage endCursor }\n"
        "        nodes { number title state repository { nameWithOwner } }\n"
        "      }\n"
        "    }\n"
        "  }\n"
        "}\n"
    )


# ─── errors ─────────────────────────────────────────────────────────────────
def stderr_line(payload: dict[str, str]) -> None:
    """The one JSON line on stderr: an error, or the publish result."""
    line = json.dumps(payload, ensure_ascii=False) + "\n"
    sys.stderr.flush()
    sys.stderr.buffer.write(line.encode("utf-8"))
    sys.stderr.buffer.flush()


def fail(message: str, code: int) -> NoReturn:
    stderr_line({"error": message})
    sys.exit(code)


def obj(value: Any) -> dict[str, Any]:
    """A JSON object from a response; anything else is a shape error (exit 3)."""
    if not isinstance(value, dict):
        raise TypeError(f"expected an object, got {type(value).__name__}")
    return value


# ─── names ──────────────────────────────────────────────────────────────────
def display(repo: str) -> str:
    return DISPLAY.get(repo, repo)


def slug(repo: str) -> str:
    """Alias with every non-alphanumeric as `_` — fixture names and Mermaid ids."""
    return re.sub(r"[^A-Za-z0-9]", "_", display(repo))


def short(key: Key) -> str:
    return f"{display(key[0])}#{key[1]}"


def url(key: Key) -> str:
    return f"https://github.com/{key[0]}/issues/{key[1]}"


def ref(key: Key) -> str:
    return f"[{short(key)}]({url(key)})"


def node_id(key: Key) -> str:
    return f"{slug(key[0])}_{key[1]}"


def label(key: Key, title: str) -> str:
    """`<alias>#35;<n> <title>`: leading [tag] groups dropped, cut to 40, escaped."""
    text = LEADING_TAGS.sub("", title).strip()
    if len(text) > LABEL_WIDTH:
        text = text[: LABEL_WIDTH - 1] + "…"
    return f"{display(key[0])}#35;{key[1]} {text.translate(MERMAID_ESCAPES)}"


def md_text(text: str) -> str:
    """Raw title text for Markdown outside the Mermaid block: no autolink, no mention.
    Code spans keep their text — they do not decode entities, so the zero-width
    space would show. A span is a run of N backticks up to the next run of exactly
    N; a run without such a partner is plain text."""

    def guard(plain: str) -> str:
        return AUTOLINK.sub(lambda m: m.group(0) + ZERO_WIDTH, plain)

    out: list[str] = []
    done = 0  # text before this offset is already in `out`
    search = 0
    while opener := BACKTICKS.search(text, search):
        closer = next(
            (
                run
                for run in BACKTICKS.finditer(text, opener.end())
                if len(run.group(0)) == len(opener.group(0))
            ),
            None,
        )
        if closer is None:
            search = opener.end()
            continue
        out += [guard(text[done : opener.start()]), text[opener.start() : closer.end()]]
        done = search = closer.end()
    out.append(guard(text[done:]))
    return "".join(out)


def valid_repo(repo: str) -> bool:
    return bool(REPO_NAME.fullmatch(repo)) and not any(
        set(part) == {"."} for part in repo.split("/")
    )


def parse_ref(text: str) -> Key:
    m = REF.fullmatch(text)
    if not m or not valid_repo(ALIASES.get(m.group(1), m.group(1))):
        fail(f"bad REF {text!r}: expected api#N, ui#N or OWNER/NAME#N", 2)
    return (ALIASES.get(m.group(1), m.group(1)), int(m.group(2)))


def parse_target(text: str) -> Key:
    """`--publish-to`: a bare N is api#N (the sprint issues live there), else a REF.
    The shape is checked here so that every bad value gets one message naming both forms."""
    if NUMBER.fullmatch(text):
        return (ALIASES["api"], int(text))
    m = REF.fullmatch(text)
    if not m or not valid_repo(ALIASES.get(m.group(1), m.group(1))):
        fail(
            f"bad --publish-to {text!r}: expected N (an api issue) or REF "
            "(api#N, ui#N or OWNER/NAME#N)",
            2,
        )
    return parse_ref(text)


# ─── transport ──────────────────────────────────────────────────────────────
def fixture_dir() -> str | None:
    """The offline seam, or None for live gh. Set but empty is a mistake: it must
    never fall through to a live call."""
    value = os.environ.get(FIXTURE_ENV)
    if value == "":
        fail(f"{FIXTURE_ENV} is set but empty", 3)
    return value


def request(
    name: str, query: str, typed: dict[str, str | int], strings: dict[str, str | None]
) -> dict[str, Any]:
    """One GraphQL request: gh, or the fixture `<name>.json` when the seam is set.
    `typed` go as `-F` (owner, name, number), `strings` as `-f`; a None is omitted."""
    seam = fixture_dir()
    gh_failure: str | None = None
    if seam is not None:
        path = Path(seam) / f"{name}.json"
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            fail(f"cannot read fixture {path}: {exc.strerror or exc}", 3)
    else:
        argv = ["gh", "api", "graphql", "-f", f"query={query}"]
        for key, value in typed.items():
            argv += ["-F", f"{key}={value}"]
        for key, text in strings.items():
            if text is not None:
                argv += ["-f", f"{key}={text}"]
        try:
            proc = subprocess.run(argv, capture_output=True, encoding="utf-8", timeout=GH_TIMEOUT)
        except subprocess.TimeoutExpired:
            fail(f"gh api graphql ({name}) timed out after {GH_TIMEOUT} s", 3)
        except OSError as exc:
            fail(f"cannot run gh: {exc}", 3)
        if proc.returncode != 0:
            gh_failure = f"gh api graphql ({name}) exited {proc.returncode}: {proc.stderr.strip()}"
        raw = proc.stdout
    try:
        response = json.loads(raw)
    except json.JSONDecodeError as exc:
        fail(gh_failure or f"{name}: response is not JSON: {exc}", 3)
    errors = response.get("errors") if isinstance(response, dict) else None
    # A ref that does not exist is input, not a transport failure. Live gh exits 1
    # for it with the full response on stdout, so this is checked before the exit code.
    if (
        isinstance(errors, list)
        and errors
        and all(isinstance(e, dict) and e.get("type") == "NOT_FOUND" for e in errors)
    ):
        fail(not_found(errors, typed, strings), 2)
    if gh_failure:
        fail(gh_failure, 3)
    if not isinstance(response, dict):
        fail(f"{name}: response is not a JSON object", 3)
    if errors:
        messages = [e.get("message", e) if isinstance(e, dict) else e for e in errors]
        fail(f"{name}: GraphQL errors: {'; '.join(str(m) for m in messages)}", 3)
    data = response.get("data")
    if not isinstance(data, dict):
        fail(f"{name}: response has no data", 3)
    return data


def not_found(
    errors: list[dict[str, Any]], typed: dict[str, str | int], strings: dict[str, str | None]
) -> str:
    """Name what each NOT_FOUND error points at, from the last `path` element and
    the request's own variables: `issue api#5 not found`, `repository o/n not found`."""
    repo = f"{typed.get('owner')}/{typed.get('name')}"
    number = typed.get("number")
    found: list[str] = []
    for error in errors:
        path = error.get("path")
        last = str(path[-1]) if isinstance(path, list) and path else ""
        alias = re.fullmatch(r"i([1-9][0-9]*)", last)
        if alias:
            entry = f"issue {short((repo, int(alias.group(1))))} not found"
        elif last == "issue" and number is not None:
            entry = f"issue {short((repo, int(number)))} not found"
        elif last == "repository":
            entry = f"repository {repo} not found"
        elif last == "milestone" and number is not None:
            entry = f"milestone {number} not found in {display(repo)}"
        elif last == "node":
            entry = f"issue {strings.get('id')} not found"
        else:
            entry = str(error.get("message", "not found"))
        if entry not in found:
            found.append(entry)
    return "; ".join(found)


def rest(
    method: str,
    path: str,
    *,
    fixture: str,
    missing: str,
    body: str | None = None,
    forbidden: str | None = None,
) -> Any:
    """One REST call: `gh api -X METHOD path`, or the fixture `<fixture>.json` when
    the seam is set. A write (`body` given) sends {"body": body} on stdin; in fixture
    mode it goes to the write log instead. A 404 is input: `missing`, exit 2. A live
    403 with `forbidden` given fails as `<forbidden>: <gh stderr>`, exit 3."""
    seam = fixture_dir()
    gh_failure: str | None = None
    gh_stderr = ""
    if seam is not None:
        if body is not None:
            log_write(method, path, body)
        source = Path(seam) / f"{fixture}.json"
        try:
            raw = source.read_text(encoding="utf-8")
        except OSError as exc:
            fail(f"cannot read fixture {source}: {exc.strerror or exc}", 3)
    else:
        argv = ["gh", "api", "-X", method, path]
        payload: str | None = None
        if body is not None:
            argv += ["--input", "-"]
            payload = json.dumps({"body": body}, ensure_ascii=False)
        try:
            proc = subprocess.run(
                argv, input=payload, capture_output=True, encoding="utf-8", timeout=GH_TIMEOUT
            )
        except subprocess.TimeoutExpired:
            written = "" if body is None else WRITE_TIMEOUT
            fail(f"gh api -X {method} {path} timed out after {GH_TIMEOUT} s{written}", 3)
        except OSError as exc:
            fail(f"cannot run gh: {exc}", 3)
        if proc.returncode != 0:
            gh_stderr = proc.stderr.strip()
            gh_failure = f"gh api -X {method} {path} exited {proc.returncode}: {gh_stderr}"
        raw = proc.stdout
    try:
        response = json.loads(raw)
    except json.JSONDecodeError as exc:
        fail(gh_failure or f"{method} {path}: response is not JSON: {exc}", 3)
    # Live gh exits 1 for a 404 with the error object on stdout, so this is checked
    # before the exit code.
    status = response.get("status") if isinstance(response, dict) else None
    if status == "404":
        fail(missing, 2)
    if gh_failure:
        if status == "403" and forbidden is not None:
            fail(f"{forbidden}: {gh_stderr}", 3)
        fail(gh_failure, 3)
    return response


def log_write(method: str, path: str, body: str) -> None:
    """Fixture mode: a write is appended to the write log, never sent anywhere."""
    log = os.environ.get(WRITE_LOG_ENV)
    if not log:
        fail(f"fixture mode cannot publish: set {WRITE_LOG_ENV}", 3)
    line = json.dumps({"method": method, "path": path, "body": body}, ensure_ascii=False) + "\n"
    try:
        with Path(log).open("a", encoding="utf-8") as out:
            out.write(line)
    except OSError as exc:
        fail(f"cannot write {log}: {exc.strerror or exc}", 3)


def repository(data: dict[str, Any], repo: str) -> dict[str, Any]:
    found = data.get("repository")
    if found is None:
        fail(f"repository {repo} not found", 2)
    return obj(found)


def paged(
    prefix: str,
    query: str,
    typed: dict[str, str | int],
    connection: Callable[[dict[str, Any]], dict[str, Any]],
) -> list[dict[str, Any]]:
    """Follow `pageInfo` from page p1 until `hasNextPage` is false."""
    nodes: list[dict[str, Any]] = []
    after: str | None = None
    page = 1
    while True:
        conn = connection(request(f"{prefix}-p{page}", query, typed, {"after": after}))
        nodes.extend(obj(n) for n in conn["nodes"])
        info = obj(conn["pageInfo"])
        if not info["hasNextPage"]:
            return nodes
        after = str(info["endCursor"])
        page += 1


def complete_relations(issue: dict[str, Any]) -> None:
    """Replace the first page of blockedBy / blocking with every page."""
    for field in ("blockedBy", "blocking"):
        conn = obj(issue[field])
        nodes = [obj(n) for n in conn["nodes"]]
        info = obj(conn["pageInfo"])
        page = 2
        while info["hasNextPage"]:
            data = request(
                f"node-{issue['id']}-{field}-p{page}",
                relation_query(field),
                {},
                {"id": str(issue["id"]), "after": str(info["endCursor"])},
            )
            more = obj(obj(data["node"])[field])
            nodes.extend(obj(n) for n in more["nodes"])
            info = obj(more["pageInfo"])
            page += 1
        conn["nodes"] = nodes


# ─── scope ──────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Scope:
    title: str
    warnings: list[str]
    issues: list[dict[str, Any]]


def fetch_milestone(title: str, repos: list[str]) -> Scope:
    warnings: list[str] = []
    issues: list[dict[str, Any]] = []
    found = False
    for repo in repos:
        owner, name = repo.split("/")
        typed: dict[str, str | int] = {"owner": owner, "name": name}
        data = request(f"milestones-{slug(repo)}", MILESTONES_QUERY, typed, {"title": title})
        # The `query` search is fuzzy: only an exact title counts.
        nodes = [obj(m) for m in obj(repository(data, repo)["milestones"])["nodes"]]
        numbers = [int(m["number"]) for m in nodes if m.get("title") == title]
        if not numbers:
            warnings.append(f"Майлстоун «{md_text(title)}» не найден в {display(repo)}.")
            continue
        found = True

        def milestone_issues(data: dict[str, Any], repo: str = repo) -> dict[str, Any]:
            return obj(obj(repository(data, repo)["milestone"])["issues"])

        issues += paged(
            f"milestone-{slug(repo)}-{numbers[0]}",
            MILESTONE_QUERY,
            {**typed, "number": numbers[0]},
            milestone_issues,
        )
    if not found:
        fail(f"milestone {title!r} not found in {', '.join(display(r) for r in repos)}", 2)
    return Scope(f"майлстоун «{md_text(title)}»", warnings, issues)


def fetch_epic(epic: Key) -> Scope:
    repo, number = epic
    owner, name = repo.split("/")
    titles: list[str] = []

    def sub_issues(data: dict[str, Any]) -> dict[str, Any]:
        issue = repository(data, repo)["issue"]
        if issue is None:
            fail(f"epic {short(epic)} not found", 2)
        titles.append(str(obj(issue)["title"]))
        return obj(issue["subIssues"])

    issues = paged(
        f"epic-{slug(repo)}-{number}",
        EPIC_QUERY,
        {"owner": owner, "name": name, "number": number},
        sub_issues,
    )
    return Scope(f"эпик {ref(epic)} {md_text(titles[0])}", [], issues)


def fetch_issues(keys: list[Key]) -> Scope:
    by_repo: dict[str, list[int]] = {}
    for repo, number in keys:
        by_repo.setdefault(repo, []).append(number)
    issues: list[dict[str, Any]] = []
    for repo, numbers in by_repo.items():
        owner, name = repo.split("/")
        data = request(
            f"issues-{slug(repo)}", issues_query(numbers), {"owner": owner, "name": name}, {}
        )
        found = repository(data, repo)
        for number in numbers:
            issue = found.get(f"i{number}")
            if issue is None:
                fail(f"issue {short((repo, number))} not found", 2)
            issues.append(obj(issue))
    return Scope("задачи " + ", ".join(ref(k) for k in keys), [], issues)


# ─── graph ──────────────────────────────────────────────────────────────────
@dataclass
class Node:
    key: Key
    title: str
    is_open: bool
    in_scope: bool
    status: str | None = None
    parent: Key | None = None


@dataclass
class Graph:
    nodes: dict[Key, Node]
    edges: set[Edge]
    containers: set[Key]

    def scope(self) -> list[Key]:
        return sorted(k for k, n in self.nodes.items() if n.in_scope)

    def external(self) -> list[Key]:
        return sorted(k for k, n in self.nodes.items() if not n.in_scope)

    def linked(self) -> set[Key]:
        return {k for edge in self.edges for k in edge}


def key_of(raw: dict[str, Any]) -> Key:
    return (str(obj(raw["repository"])["nameWithOwner"]), int(raw["number"]))


def board_status(issue: dict[str, Any], owner: str, project: int) -> str | None:
    """Status of the item on board `project` owned by `owner`; any other board is ignored."""
    for raw in obj(issue["projectItems"])["nodes"]:
        item = obj(raw)
        board = obj(item["project"])
        login = obj(board.get("owner") or {}).get("login")
        if board.get("number") == project and login == owner:
            name = obj(item.get("fieldValueByName") or {}).get("name")
            return None if name is None else str(name)
    return None


def build_graph(issues: list[dict[str, Any]], owner: str, project: int) -> Graph:
    nodes: dict[Key, Node] = {}
    for issue in issues:
        key = key_of(issue)
        if key in nodes:
            continue
        parent = issue.get("parent")
        nodes[key] = Node(
            key=key,
            title=str(issue["title"]),
            is_open=issue["state"] == "OPEN",
            in_scope=True,
            status=board_status(issue, owner, project),
            parent=key_of(obj(parent)) if parent else None,
        )

    def end(raw: dict[str, Any]) -> Key:
        key = key_of(raw)
        if key not in nodes:
            nodes[key] = Node(key, str(raw["title"]), raw["state"] == "OPEN", in_scope=False)
        return key

    edges: set[Edge] = set()
    for issue in issues:
        key = key_of(issue)
        edges.update((end(other), key) for other in obj(issue["blockedBy"])["nodes"])
        edges.update((key, end(other)) for other in obj(issue["blocking"])["nodes"])

    scope = {k for k, n in nodes.items() if n.in_scope}
    containers = {
        n.parent
        for n in nodes.values()
        if n.in_scope and n.parent is not None and n.parent in scope and n.parent != n.key
    }
    return Graph(nodes, edges, containers)


def successors(edges: set[Edge]) -> dict[Key, list[Key]]:
    succ: dict[Key, list[Key]] = {}
    for a, b in edges:
        succ.setdefault(a, []).append(b)
        succ.setdefault(b, [])
    return {k: sorted(v) for k, v in succ.items()}


def cyclic_components(succ: dict[Key, list[Key]]) -> list[set[Key]]:
    """Tarjan SCC; keeps the components of size > 1 and self-loops."""
    index: dict[Key, int] = {}
    low: dict[Key, int] = {}
    stack: list[Key] = []
    on_stack: set[Key] = set()
    found: list[set[Key]] = []

    def visit(v: Key) -> None:
        index[v] = low[v] = len(index)
        stack.append(v)
        on_stack.add(v)
        for w in succ[v]:
            if w not in index:
                visit(w)
                low[v] = min(low[v], low[w])
            elif w in on_stack:
                low[v] = min(low[v], index[w])
        if low[v] == index[v]:
            component: set[Key] = set()
            while True:
                w = stack.pop()
                on_stack.discard(w)
                component.add(w)
                if w == v:
                    break
            if len(component) > 1 or v in succ[v]:
                found.append(component)

    for v in sorted(succ):
        if v not in index:
            visit(v)
    return found


def walk_cycle(component: set[Key], succ: dict[Key, list[Key]]) -> list[Key]:
    """From the smallest key, successors in key order inside the component,
    backing out of dead ends, until an edge returns to the start."""
    start = min(component)
    path = [start]

    def extend(v: Key) -> bool:
        for w in succ[v]:
            if w == start:
                return True
            if w in component and w not in path:
                path.append(w)
                if extend(w):
                    return True
                path.pop()
        return False

    extend(start)
    return [*path, start]


def find_cycles(edges: set[Edge]) -> list[list[Key]]:
    succ = successors(edges)
    return sorted((walk_cycle(c, succ) for c in cyclic_components(succ)), key=lambda c: c[0])


def open_dag(g: Graph) -> tuple[list[Key], dict[Key, list[Key]]]:
    """Open nodes minus edge-less containers; edges with both ends open."""
    linked = g.linked()
    members = sorted(
        k for k, n in g.nodes.items() if n.is_open and not (k in g.containers and k not in linked)
    )
    member_set = set(members)
    succ: dict[Key, list[Key]] = {k: [] for k in members}
    for a, b in sorted(g.edges):
        if a in member_set and b in member_set:
            succ[a].append(b)
    return members, succ


def kahn_layers(members: list[Key], succ: dict[Key, list[Key]]) -> list[list[Key]]:
    indegree = dict.fromkeys(members, 0)
    for v in members:
        for w in succ[v]:
            indegree[w] += 1
    layers: list[list[Key]] = []
    current = sorted(v for v in members if indegree[v] == 0)
    while current:
        layers.append(current)
        following: list[Key] = []
        for v in current:
            for w in succ[v]:
                indegree[w] -= 1
                if indegree[w] == 0:
                    following.append(w)
        current = sorted(following)
    return layers


def _rank(path: list[Key]) -> tuple[int, list[Key]]:
    """Longer first; among equal lengths the lexicographically smallest keys."""
    return (-len(path), path)


def critical_path(layers: list[list[Key]], succ: dict[Key, list[Key]]) -> list[Key]:
    best: dict[Key, list[Key]] = {}
    for v in reversed([v for layer in layers for v in layer]):
        tails = [best[w] for w in succ[v]]
        best[v] = [v, *min(tails, key=_rank)] if tails else [v]
    return min(best.values(), key=_rank) if best else []


def waiting(g: Graph) -> dict[Key, list[Key]]:
    """In-scope `In Progress` issues with at least one OPEN blocker."""
    out: dict[Key, list[Key]] = {}
    for key in g.scope():
        if g.nodes[key].status != IN_PROGRESS:
            continue
        blockers = sorted(a for a, b in g.edges if b == key and g.nodes[a].is_open)
        if blockers:
            out[key] = blockers
    return out


# ─── report ─────────────────────────────────────────────────────────────────
def node_class(g: Graph, key: Key, alarm: set[Key]) -> str | None:
    node = g.nodes[key]
    if key in g.containers:
        return None
    if key in alarm:
        return "alarm"
    if not node.is_open:
        return "done"
    if not node.in_scope:
        return "external"
    if node.status in REVIEW_STATUSES:
        return "review"
    if node.status == IN_PROGRESS:
        return "progress"
    return None


def render_mermaid(g: Graph, alarm: set[Key]) -> list[str]:
    children: dict[Key, list[Key]] = {}
    top: list[Key] = []
    for key in sorted(g.nodes):
        parent = g.nodes[key].parent
        if g.nodes[key].in_scope and parent in g.containers and parent != key:
            children.setdefault(parent, []).append(key)
        else:
            top.append(key)

    lines = ["flowchart LR"]

    def emit(key: Key, depth: int) -> None:
        indent = "  " * depth
        text = label(key, g.nodes[key].title)
        if key in g.containers:
            lines.append(f'{indent}subgraph {node_id(key)}["{text}"]')
            for child in children.get(key, []):
                emit(child, depth + 1)
            lines.append(f"{indent}end")
        else:
            lines.append(f'{indent}{node_id(key)}["{text}"]')

    for key in top:
        emit(key, 1)
    for a, b in sorted(g.edges):
        arrow = "-->" if g.nodes[a].is_open else "-.->"
        lines.append(f"  {node_id(a)} {arrow} {node_id(b)}")
    lines += [f"  {line}" for line in CLASS_DEFS]
    by_class: dict[str, list[str]] = {}
    for key in sorted(g.nodes):
        name = node_class(g, key, alarm)
        if name:
            by_class.setdefault(name, []).append(node_id(key))
    for name in CLASS_ORDER:
        if name in by_class:
            lines.append(f"  class {','.join(by_class[name])} {name}")
    return lines


def tangle(g: Graph, cycle: list[Key], component: set[Key]) -> list[str]:
    """The nested line under a cycle line: the component's members off the walked
    cycle, in Key order, tagged outside and/or closed; none when there are none."""
    extra = sorted(component - set(cycle))
    if not extra:
        return []

    def tagged(key: Key) -> str:
        node = g.nodes[key]
        tags = [
            tag
            for tag, on in (("вне выборки", not node.in_scope), ("закрыта", not node.is_open))
            if on
        ]
        return ref(key) + (f" ({', '.join(tags)})" if tags else "")

    return ["  - в том же клубке связей ещё: " + ", ".join(tagged(k) for k in extra)]


def render(scope: Scope, g: Graph) -> tuple[str, bool]:
    cycles = find_cycles(g.edges)
    components = cyclic_components(successors(g.edges))
    waits = waiting(g)
    # "On a cycle" = inside a cyclic component, not only on the one cycle reported.
    alarm = set(waits).union(*components)
    in_scope = g.scope()
    external = g.external()
    linked = g.linked()

    def tagged(key: Key) -> str:
        return ref(key) + ("" if g.nodes[key].in_scope else EXTERNAL)

    out = [f"### Граф задач: {scope.title}", ""]
    if scope.warnings:
        out += [f"> {w}" for w in scope.warnings] + [""]
    opened = sum(1 for k in in_scope if g.nodes[k].is_open)
    out += [
        f"Задач: {len(in_scope)} (открыто {opened}, закрыто {len(in_scope) - opened}). "
        f"Связей блокировки: {len(g.edges)}. Вне выборки: {len(external)}.",
        "",
        "```mermaid",
        *render_mermaid(g, alarm),
        "```",
    ]

    def section(title: str, body: list[str], empty: str = "Нет.") -> None:
        out.extend(["", f"#### {title}", "", *(body or [empty])])

    cycle_lines: list[str] = []
    for cycle in cycles:
        component = next(c for c in components if cycle[0] in c)
        cycle_lines += ["- " + " → ".join(ref(k) for k in cycle), *tangle(g, cycle, component)]
    section("Циклы", cycle_lines)
    section(
        "In Progress с открытым блокером",
        [f"- {ref(k)} ждёт {', '.join(tagged(b) for b in bs)}" for k, bs in waits.items()],
    )
    if cycles:
        section("Слои: что можно брать параллельно", ["Не считаются: в графе цикл."])
        section("Критический путь", ["Не считается: в графе цикл."])
    else:
        members, dag = open_dag(g)
        layers = kahn_layers(members, dag)
        section(
            "Слои: что можно брать параллельно",
            [
                f"{i}. {', '.join(tagged(k) for k in layer)}"
                + (" — можно брать сейчас" if i == 1 else "")
                for i, layer in enumerate(layers, 1)
            ],
            "Нет открытых задач.",
        )
        path = critical_path(layers, dag)
        section(
            "Критический путь",
            [f"{' → '.join(ref(k) for k in path)} (задач: {len(path)})"] if len(path) >= 2 else [],
            "Нет: между открытыми задачами нет связей.",
        )
    section(
        "Без связей",
        [
            f"- {ref(k)} {md_text(g.nodes[k].title)}"
            for k in in_scope
            if g.nodes[k].is_open and k not in g.containers and k not in linked
        ],
    )
    outside: list[str] = []
    for key in external:
        blocks = sorted(b for a, b in g.edges if a == key)
        waits_on = sorted(a for a, b in g.edges if b == key)
        parts = []
        if blocks:
            parts.append("блокирует " + ", ".join(ref(k) for k in blocks))
        if waits_on:
            parts.append("ждёт " + ", ".join(ref(k) for k in waits_on))
        closed = "" if g.nodes[key].is_open else " (закрыта)"
        outside.append(f"- {ref(key)} {md_text(g.nodes[key].title)}{closed} — {'; '.join(parts)}")
    section("Вне выборки", outside)
    return "\n".join(out) + "\n", bool(cycles)


# ─── publish ────────────────────────────────────────────────────────────────
def marker(opts: Options) -> str:
    """`<!-- issue-graph: <key> -->`. A milestone key leaves out the repo set; an
    `owner/name` is lower-cased (GitHub ignores its case) before issues are
    deduplicated and sorted by Key. `%` is escaped first, then CR and LF (the
    marker stays one line, so `first_line` can match it), then `>` (a title can
    never close the HTML comment)."""
    if opts.milestone is not None:
        key = f"milestone={opts.milestone}"
    elif opts.epic is not None:
        key = f"epic={opts.epic[0].lower()}#{opts.epic[1]}"
    else:
        keys = sorted({(repo.lower(), n) for repo, n in opts.issues})
        key = "issues=" + ",".join(f"{repo}#{n}" for repo, n in keys)
    for raw, escaped in (("%", "%25"), ("\r", "%0D"), ("\n", "%0A"), (">", "%3E")):
        key = key.replace(raw, escaped)
    return f"<!-- issue-graph: {key} -->"


def first_line(text: str) -> str:
    return text.split("\n", 1)[0].removesuffix("\r")


def find_marked(target: Key, mark: str) -> int | None:
    """Id of the oldest comment on `target` whose first line is `mark`: pages of
    100, oldest first, up to the first match or a short page; a full last page
    under the cap without a match stops the search (exit 3)."""
    repo, number = target
    for page in range(1, COMMENTS_MAX_PAGES + 1):
        path = f"repos/{repo}/issues/{number}/comments?per_page={COMMENTS_PAGE}&page={page}"
        items = rest(
            "GET",
            path,
            fixture=f"comments-{slug(repo)}-{number}-p{page}",
            missing=f"issue {short(target)} not found",
        )
        if not isinstance(items, list):
            fail(f"GET {path}: response is not a JSON array", 3)
        for item in items:
            comment = obj(item)
            body = comment.get("body")
            if isinstance(body, str) and first_line(body) == mark:
                return int(comment["id"])
        if len(items) < COMMENTS_PAGE:
            return None
    fail(
        f"issue {short(target)}: more than {COMMENTS_MAX_PAGES * COMMENTS_PAGE} comments, "
        "the marker search stopped",
        3,
    )


def publish(target: Key, mark: str, report: str) -> None:
    """Update the marked comment on `target` or create one; the result goes to stderr."""
    body = f"{mark}\n\n{report}\n{FOOTER}\n"
    if len(body) > COMMENT_LIMIT:
        fail(
            f"report is {len(body)} characters, over GitHub's {COMMENT_LIMIT}-character "
            "comment limit; narrow the scope",
            2,
        )
    repo, number = target
    found = find_marked(target, mark)
    if found is None:
        action = "created"
        response = rest(
            "POST",
            f"repos/{repo}/issues/{number}/comments",
            fixture=f"comment-created-{slug(repo)}-{number}",
            missing=f"issue {short(target)} not found",
            body=body,
        )
    else:
        action = "updated"
        # A 403 here is final: a new comment would leave the marked one stale.
        response = rest(
            "PATCH",
            f"repos/{repo}/issues/comments/{found}",
            fixture=f"comment-updated-{found}",
            missing=f"comment {found} not found",
            body=body,
            forbidden=f"comment {found} carries the marker but this account cannot edit it",
        )
    stderr_line({"published": action, "url": str(obj(response)["html_url"])})


# ─── CLI ────────────────────────────────────────────────────────────────────
class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        fail(message, 2)


@dataclass(frozen=True)
class Options:
    milestone: str | None
    epic: Key | None
    issues: list[Key]
    repos: list[str]
    project_owner: str
    project: int
    publish_to: Key | None


def parse_args(argv: list[str]) -> Options:
    parser = _Parser(
        prog="issue_graph.py",
        description="Sprint dependency graph report (Mermaid, layers, critical path, cycles).",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--milestone", metavar="TITLE", help="milestone title, exact match")
    mode.add_argument("--epic", metavar="REF", help="epic: its direct sub-issues")
    mode.add_argument("--issues", metavar="REF", nargs="+", help="explicit issues")
    parser.add_argument(
        "--repo",
        metavar="OWNER/NAME",
        action="extend",
        nargs="+",
        help="repos searched for --milestone (default: api and ui)",
    )
    parser.add_argument("--project-owner", metavar="ORG", default=DEFAULT_PROJECT_OWNER)
    parser.add_argument("--project", metavar="NUMBER", type=int, default=DEFAULT_PROJECT)
    parser.add_argument(
        "--publish-to",
        metavar="N|REF",
        help="also post the report to that issue (N = api#N), updated in place on re-run",
    )
    ns = parser.parse_args(argv)

    repos = list(dict.fromkeys(ns.repo or []))
    if repos and ns.milestone is None:
        fail("--repo applies to --milestone only", 2)
    for repo in repos:
        if not valid_repo(repo):
            fail(f"--repo must be OWNER/NAME, got {repo!r}", 2)
    return Options(
        milestone=None if ns.milestone is None else str(ns.milestone),
        epic=None if ns.epic is None else parse_ref(str(ns.epic)),
        issues=list(dict.fromkeys(parse_ref(str(r)) for r in ns.issues or [])),
        repos=repos or list(DEFAULT_REPOS),
        project_owner=str(ns.project_owner),
        project=int(ns.project),
        publish_to=None if ns.publish_to is None else parse_target(str(ns.publish_to)),
    )


def fetch(opts: Options) -> Scope:
    if opts.milestone is not None:
        scope = fetch_milestone(opts.milestone, opts.repos)
    elif opts.epic is not None:
        scope = fetch_epic(opts.epic)
    else:
        scope = fetch_issues(opts.issues)
    for issue in scope.issues:
        complete_relations(issue)
    return scope


def main(argv: list[str]) -> int:
    opts = parse_args(argv[1:])
    try:
        scope = fetch(opts)
        graph = build_graph(scope.issues, opts.project_owner, opts.project)
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        fail(f"unexpected GraphQL response shape: {exc!r}", 3)
    report, cyclic = render(scope, graph)
    sys.stdout.flush()
    sys.stdout.buffer.write(report.encode("utf-8"))
    sys.stdout.buffer.flush()
    if opts.publish_to is None:
        return 1 if cyclic else 0
    if cyclic:
        fail("not published: the graph has a cycle", 1)
    try:
        publish(opts.publish_to, marker(opts), report)
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        fail(f"unexpected REST response shape: {exc!r}", 3)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
