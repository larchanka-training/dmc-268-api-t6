#!/usr/bin/env python3
"""issue_facts.py — the acceptance facts of one GitHub issue as stable JSON.

Collects what the acceptance procedure (api `docs/CONTRIBUTING.md`, «Приёмка
задачи», commands 1-3) needs and states it as facts: the full closing history
with the kind of closer of every close, the Acceptance Criteria / DoD
checkboxes, and every pull request that references the issue. It never judges
whether a close was legitimate: the actor is reported, but the classification
is by closer (merge, commit, manual, ...), never by actor.

Usage
-----
    issue_facts.py REF
    REF := api#N | ui#N | OWNER/NAME#N | N        (a bare N is api#N)

Contract
--------
stdout carries one JSON document (`sort_keys`, indent 2, no ASCII escaping, one
trailing newline), schema 1:

    issue     {ref, title, url, state, state_reason}
    closing   {events, last_closer, ever_closed_by_merge}
              events: every ClosedEvent / ReopenedEvent, chronological; a close
              has state_reason, closer (merge | commit | manual | project | the
              lowercased GraphQL typename) and closer_ref (owner/repo#N of the
              closing PR, the full oid of the closing commit, else null).
              last_closer: closer of the last close while the issue is CLOSED,
              else null. ever_closed_by_merge: any close by a merge or a commit.
    checkboxes {ac, dod, outside}
              ac / dod: {total, checked, items[{checked, text}]} for the
              `## Acceptance Criteria` / `## DoD Checklist` sections (a section
              ends at the next heading of the same or higher level); outside:
              checkboxes anywhere else. Fenced code (``` and ~~~), HTML comments
              and blockquotes are skipped. Headings are ATX (`## Name`) only;
              setext headings (underlined with === or ---) are not recognised.
    prs       every pull request from the timeline's cross-references (any repo,
              deduplicated, sorted by (repo, number)): {ref, title, url, state,
              merged_at, head_sha, ci, unresolved_threads, refs, will_close}.
              refs: the title or body has a `Refs` line naming THIS issue. The
              accepted grammar is the team's: a line starting with `Refs` (any
              case, an optional list bullet, an optional colon) followed by
              `#N` or `OWNER/NAME#N` references separated by commas, `;`, `&`,
              `and` or blanks, e.g. `Refs #12, #13`; text after the list is
              ignored. `#N` counts only for a PR in the issue's own repo, a full
              `OWNER/NAME#N` from any repo (the repo name is case-insensitive).
              Anything else is NOT recognised and leaves refs false, though the
              PR is still listed: `**Refs** #N`, `Refs: [#N](url)`, an issue URL,
              `Refs` and the reference on separate lines, `Refs` in running
              text. Lines in fenced code or HTML comments do not count.
              will_close is GitHub's `willCloseTarget` of the cross-reference:
              it is false on a pull request that is already merged, even one
              that closed the issue; that merge-close shows in
              closing.events[].closer_ref instead.

stderr carries one JSON line {"error": "..."} on failure and stdout stays empty.
Exit 0 ok; 2 usage or bad REF; 3 gh / API error (non-zero gh exit, timeout, any
GraphQL errors, issue not found, unexpected shape, missing fixture, fixture dir
set but empty). Read-only: the only requests are `gh api graphql` queries.

Offline seam: with ACCEPT_ISSUE_FIXTURE_DIR set, gh is never called and every
request reads <dir>/<name>.json (the full {"data": ...} response) instead; set
but empty exits 3, never falls through to gh. Names: issue-p<k> (timeline page
k, the first is p1) and threads-<owner>_<name>-<number>-p<k> (review-thread
page k of a pull request, the first follow-up page is p2).
"""
# SYNC: mirrored in dmc-268-{ui,api}-t6/.agents/skills/accept-issue/scripts/issue_facts.py

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
FIXTURE_ENV = "ACCEPT_ISSUE_FIXTURE_DIR"
GH_TIMEOUT = 120  # seconds per gh call
MAX_PAGES = 200  # a page that always has a next page must not loop forever

REPO_NAME = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
REF = re.compile(r"(api|ui|[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#([1-9][0-9]*)")
NUMBER = re.compile(r"[1-9][0-9]*")

HEADING = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.*?)[ \t]*#*[ \t]*$")
# CommonMark fences: at most 3 spaces of indent; a backtick opener's info string
# has no backtick; a closer is the same character, at least as long, and bare.
FENCE_OPEN = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
FENCE_CLOSE = re.compile(r"^ {0,3}(`{3,}|~{3,})[ \t]*$")
CHECKBOX = re.compile(r"^\s*[-*+] \[([ xX])\](.*)$")
SECTIONS = {"acceptance criteria": "ac", "dod checklist": "dod"}

# A `Refs` line: the keyword, then a list of references (`#12`, `owner/repo#12`)
# separated by commas / `and` / `&` / blanks; text after the list is ignored.
_TOKEN = r"(?:[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)?#[1-9][0-9]*"
REFS_LINE = re.compile(
    rf"^\s*(?:[-*+]\s+)?Refs?\b:?\s*({_TOKEN}(?:\s*(?:,|;|&|\band\b)?\s*{_TOKEN})*)",
    re.IGNORECASE,
)
REFS_TOKEN = re.compile(r"([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)?#([1-9][0-9]*)")

_PR_FIELDS = """
  number title url body state mergedAt headRefOid
  repository { nameWithOwner }
  commits(last: 1) { nodes { commit { statusCheckRollup { state } } } }
  reviewThreads(first: 100) { pageInfo { hasNextPage endCursor } nodes { isResolved } }
"""
ISSUE_QUERY = f"""
query($owner: String!, $name: String!, $number: Int!, $c: String) {{
  repository(owner: $owner, name: $name) {{
    issue(number: $number) {{
      title url state stateReason body
      timelineItems(first: 100, after: $c,
                    itemTypes: [CLOSED_EVENT, REOPENED_EVENT, CROSS_REFERENCED_EVENT]) {{
        pageInfo {{ hasNextPage endCursor }}
        nodes {{
          __typename
          ... on ClosedEvent {{
            createdAt actor {{ login }} stateReason
            closer {{
              __typename
              ... on PullRequest {{ number repository {{ nameWithOwner }} }}
              ... on Commit {{ oid }}
            }}
          }}
          ... on ReopenedEvent {{ createdAt actor {{ login }} }}
          ... on CrossReferencedEvent {{
            willCloseTarget
            source {{ __typename ... on PullRequest {{ {_PR_FIELDS} }} }}
          }}
        }}
      }}
    }}
  }}
}}
"""
THREADS_QUERY = """
query($owner: String!, $name: String!, $number: Int!, $c: String) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      reviewThreads(first: 100, after: $c) {
        pageInfo { hasNextPage endCursor }
        nodes { isResolved }
      }
    }
  }
}
"""


def no_note() -> str:
    return ""


# A caller that has already written something sets this to append what it wrote to every
# exit-3 message (acceptance.py does); the default adds nothing.
exit3_note: Callable[[], str] = no_note


def fail(message: str, code: int) -> NoReturn:
    if code == 3:
        message += exit3_note()
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
def fetch_issue(
    repo: str, number: int, prefix: str = ""
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """The issue's own fields (first page) and every timeline node, all pages."""
    issue: dict[str, Any] | None = None
    timeline: list[dict[str, Any]] = []
    cursor: str | None = None
    for page in range(1, MAX_PAGES + 1):
        data = request(f"{prefix}issue-p{page}", ISSUE_QUERY, repo, number, cursor)
        repository = data.get("repository")
        node = obj(repository, "repository") if repository is not None else {}
        raw_issue = node.get("issue")
        if raw_issue is None:
            fail(f"issue {ref_str(repo, number)} not found", 3)
        current = obj(raw_issue, "issue")
        if issue is None:
            issue = current
        nodes, more, cursor = nodes_of(current.get("timelineItems"), "issue.timelineItems")
        timeline.extend(nodes)
        if not more:
            assert issue is not None
            return issue, timeline
    fail(f"issue timeline has more than {MAX_PAGES} pages", 3)


def extra_unresolved(repo: str, number: int, cursor: str, prefix: str = "") -> int:
    """Unresolved review threads on the pages after the first, 100 per page."""
    count = 0
    after: str | None = cursor
    for page in range(2, MAX_PAGES + 2):
        name = f"{prefix}threads-{repo.replace('/', '_')}-{number}-p{page}"
        data = request(name, THREADS_QUERY, repo, number, after)
        repository = obj(data.get("repository"), "repository")
        pull = obj(repository.get("pullRequest"), "pullRequest")
        nodes, more, after = nodes_of(pull.get("reviewThreads"), "pullRequest.reviewThreads")
        count += sum(1 for n in nodes if n.get("isResolved") is False)
        if not more:
            return count
    fail(f"review threads of {ref_str(repo, number)} have more than {MAX_PAGES} pages", 3)


# ─── closing history ────────────────────────────────────────────────────────
def closer_of(node: dict[str, Any], where: str) -> tuple[str, str | None]:
    """(closer, closer_ref) of a ClosedEvent."""
    raw = node.get("closer")
    if raw is None:
        return ("manual", None)
    closer = obj(raw, f"{where}.closer")
    typename = text_of(closer, "__typename", f"{where}.closer")
    if typename == "PullRequest":
        pr_repo = obj(closer.get("repository"), f"{where}.closer.repository")
        pr_number = closer.get("number")
        if not isinstance(pr_number, int):
            fail(f"unexpected shape: {where}.closer.number is not an integer", 3)
        return ("merge", ref_str(text_of(pr_repo, "nameWithOwner", where), pr_number))
    if typename == "Commit":
        return ("commit", text_of(closer, "oid", f"{where}.closer"))
    if typename == "ProjectV2":
        return ("project", None)
    return (typename.lower(), None)


def build_closing(timeline: list[dict[str, Any]], state: str) -> dict[str, Any]:
    events: list[dict[str, Any]] = []
    for i, node in enumerate(timeline):
        kind = node.get("__typename")
        if kind not in ("ClosedEvent", "ReopenedEvent"):
            continue
        where = f"timeline[{i}]"
        actor_raw = node.get("actor")
        actor = None
        if actor_raw is not None:
            actor = text_of(obj(actor_raw, f"{where}.actor"), "login", f"{where}.actor")
        event: dict[str, Any] = {
            "kind": "closed" if kind == "ClosedEvent" else "reopened",
            "at": text_of(node, "createdAt", where),
            "actor": actor,
        }
        if kind == "ClosedEvent":
            event["state_reason"] = opt_text(node, "stateReason", where)
            event["closer"], event["closer_ref"] = closer_of(node, where)
        events.append(event)
    events.sort(key=lambda e: e["at"])  # stable: equal timestamps keep API order
    closes = [e for e in events if e["kind"] == "closed"]
    return {
        "events": events,
        "last_closer": closes[-1]["closer"] if closes and state == "CLOSED" else None,
        "ever_closed_by_merge": any(e["closer"] in ("merge", "commit") for e in closes),
    }


# ─── markdown scanning ──────────────────────────────────────────────────────
def visible_lines(text: str, *, skip_quotes: bool) -> Iterator[str]:
    """The lines of `text` that render as prose: outside fenced code and HTML
    comments (single- and multi-line; the text around a comment stays) and, when
    `skip_quotes`, outside blockquotes. Shared by the checkbox scan and the
    `Refs` search so that both see the same text."""
    for _, line in visible_indexed(text, skip_quotes=skip_quotes):
        yield line


def visible_indexed(text: str, *, skip_quotes: bool) -> Iterator[tuple[int, str]]:
    """`visible_lines` with the index of each line's raw line in `text.splitlines()`,
    for a caller that rewrites the raw line."""
    fence: str | None = None  # the opening run of the open fenced block
    in_comment = False
    for index, raw in enumerate(text.splitlines()):
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
        yield index, line


def scan_checkboxes(body: str) -> Iterator[tuple[int, str | None, bool, str]]:
    """Every checkbox of the issue body as (raw line index in `body.splitlines()`,
    section, checked, text); section is "ac" / "dod" inside the two sections, else
    None."""
    current: str | None = None  # "ac" | "dod" | None
    level = 0  # heading level of the open section
    for index, line in visible_indexed(body, skip_quotes=True):
        heading = HEADING.match(line)
        if heading:
            depth = len(heading.group(1))
            name = SECTIONS.get(heading.group(2).strip().lower())
            if name is not None:
                current, level = name, depth
            elif current is not None and depth <= level:
                current = None
            continue
        box = CHECKBOX.match(line)
        if box:
            yield index, current, box.group(1) != " ", box.group(2).strip()


def build_checkboxes(body: str) -> dict[str, Any]:
    """AC / DoD checkboxes of the issue body, plus a count of the ones elsewhere."""
    sections: dict[str, list[dict[str, Any]]] = {"ac": [], "dod": []}
    outside = 0
    for _, section, checked, text in scan_checkboxes(body):
        if section is None:
            outside += 1
        else:
            sections[section].append({"checked": checked, "text": text})
    return {
        "ac": section_facts(sections["ac"]),
        "dod": section_facts(sections["dod"]),
        "outside": outside,
    }


def section_facts(items: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "total": len(items),
        "checked": sum(1 for i in items if i["checked"]),
        "items": items,
    }


# ─── pull requests ──────────────────────────────────────────────────────────
def names_issue(pr: dict[str, Any], pr_repo: str, issue: tuple[str, int]) -> bool:
    """True when the PR title or body has a `Refs` line naming `issue`: a bare
    `#N` means the PR's own repo, so it counts only when that is the issue's repo."""
    repo, number = issue
    for part in (pr.get("title"), pr.get("body")):
        for line in visible_lines(part or "", skip_quotes=False):
            m = REFS_LINE.match(line)
            if not m:
                continue
            for token in REFS_TOKEN.finditer(m.group(1)):
                token_repo = (token.group(1) or pr_repo).lower()
                if token_repo == repo.lower() and int(token.group(2)) == number:
                    return True
    return False


def build_prs(
    timeline: list[dict[str, Any]], issue: tuple[str, int], prefix: str = ""
) -> list[dict[str, Any]]:
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
        will_close = node.get("willCloseTarget") is True
        key = (pr_repo, pr_number)
        if key in found:
            found[key]["will_close"] = found[key]["will_close"] or will_close
            continue
        threads, more, cursor = nodes_of(
            source.get("reviewThreads"), f"{where}.source.reviewThreads"
        )
        unresolved = sum(1 for t in threads if t.get("isResolved") is False)
        if more:
            assert cursor is not None
            unresolved += extra_unresolved(pr_repo, pr_number, cursor, prefix)
        found[key] = {
            "ref": ref_str(pr_repo, pr_number),
            "title": text_of(source, "title", f"{where}.source"),
            "url": text_of(source, "url", f"{where}.source"),
            "state": text_of(source, "state", f"{where}.source"),
            "merged_at": opt_text(source, "mergedAt", f"{where}.source"),
            "head_sha": text_of(source, "headRefOid", f"{where}.source"),
            "ci": rollup_state(source, where),
            "unresolved_threads": unresolved,
            "refs": names_issue(source, pr_repo, issue),
            "will_close": will_close,
        }
    return [found[key] for key in sorted(found)]


def rollup_state(pr: dict[str, Any], where: str) -> str | None:
    """`statusCheckRollup.state` of the PR's last commit, None when it has none."""
    nodes = commit_nodes(pr, where)
    if not nodes:
        return None
    commit = obj(nodes[-1].get("commit"), f"{where}.source.commits[].commit")
    rollup = commit.get("statusCheckRollup")
    if rollup is None:
        return None
    return text_of(obj(rollup, f"{where}.statusCheckRollup"), "state", f"{where}.statusCheckRollup")


def commit_nodes(pr: dict[str, Any], where: str) -> list[dict[str, Any]]:
    """`commits(last: 1)` is queried without pageInfo: just its nodes."""
    nodes = obj(pr.get("commits"), f"{where}.source.commits").get("nodes")
    if not isinstance(nodes, list):
        fail(f"unexpected shape: {where}.source.commits.nodes is not a list", 3)
    return [obj(n, f"{where}.source.commits.nodes[]") for n in nodes]


# ─── main ───────────────────────────────────────────────────────────────────
def collect(repo: str, number: int, prefix: str = "") -> dict[str, Any]:
    """The facts. `prefix` is prepended to every offline fixture name (the read-back
    of a write uses `after-`); it changes nothing against live gh."""
    issue, timeline = fetch_issue(repo, number, prefix)
    state = text_of(issue, "state", "issue")
    body = issue.get("body")
    if body is not None and not isinstance(body, str):
        fail("unexpected shape: issue.body is not a string or null", 3)
    return {
        "schema": SCHEMA,
        "issue": {
            "ref": ref_str(repo, number),
            "title": text_of(issue, "title", "issue"),
            "url": text_of(issue, "url", "issue"),
            "state": state,
            "state_reason": opt_text(issue, "stateReason", "issue"),
        },
        "closing": build_closing(timeline, state),
        "checkboxes": build_checkboxes(body or ""),
        "prs": build_prs(timeline, (repo, number), prefix),
    }


class Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        fail(message, 2)


def main(argv: list[str] | None = None) -> int:
    parser = Parser(prog="issue_facts.py", description="Acceptance facts of one issue as JSON.")
    parser.add_argument(
        "ref", metavar="REF", help="api#N | ui#N | OWNER/NAME#N | N (bare N = api#N)"
    )
    args = parser.parse_args(argv)
    repo, number = parse_ref(args.ref)
    facts = collect(repo, number)
    sys.stdout.write(json.dumps(facts, sort_keys=True, indent=2, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
