#!/usr/bin/env python3
"""acceptance.py — the author's self-check table and the accepter's close / return.

Implements the acceptance rules of api `docs/CONTRIBUTING.md` («Приёмка задачи»,
§2 steps 8-9) on top of `issue_facts.py` (the facts) and `board_status.py` (the card).
Everything a model could get wrong silently — a row for every AC and DoD, the AC Check
gate, ticking the body, closing, the board move — is done here; the skill text only
sequences the steps. The exact output of every case is fixed by `examples/`
(`examples/README.md` is the contract).

Usage
-----
    acceptance.py table  REF ROWS_JSON [--apply]
    acceptance.py accept REF [--table-comment ID] [--followup REF]... [--apply]
    acceptance.py return REF [--table-comment ID] [--fail ID=reason]... [--apply]
    REF := api#N | ui#N | OWNER/NAME#N | N        (a bare N is api#N)

`accept` and `return` are the accepter's. `accept` reads the executor's table (the
latest comment with the hidden marker, or the comment named by `--table-comment ID`),
checks it against the issue body and, when every row is done or moved, ticks the AC and
DoD boxes, posts the closing comment that names every item and closes the issue as
completed (read back: CLOSED, closed manually, nothing unchecked). `return` posts the
«что осталось» comment for the table's «не выполнено» rows plus `--fail ID=reason`
(evidence that did not hold) and moves the card from AC Check to In Progress.

`table` is the author's self-check: it validates the rows (one per AC and DoD of the
issue body, evidence from `ROWS_JSON`, item text from the body), renders the table
comment, and moves the card: AC Check when nothing is «не выполнено»; On Review ->
In Progress when something is (a card already in AC Check is left, the acceptor
returns it). Without `--apply` nothing is written: the JSON verdict shows the exact
comment. With `--apply` everything is revalidated and written; a stop writes nothing.
After the close the read-back is retried (3 times, 1 s apart); the exit-3 error then
names the writes already made. Before the body PATCH the body is read again and the run
stops (exit 3, nothing written) if it differs from the one the verdict was computed on.

Contract
--------
stdout carries one JSON document (`sort_keys`, indent 2, no ASCII escaping, one
trailing newline), schema 1. Exit 0 ok, 1 stop (`verdict` is `stop`), 2 usage or a bad
ROWS_JSON, 3 gh / API / data error (also `--apply` against the offline seam without
ACCEPT_ISSUE_WRITE_LOG). On 2 and 3 stderr carries one JSON line {"error": "..."} and
stdout stays empty. The keys are listed in `examples/README.md`.

Offline seam: with ACCEPT_ISSUE_FIXTURE_DIR set, gh is never called and every request
reads <dir>/<name>.json (the full {"data": ...} response); the names are those of
`issue_facts.py` and `board_status.py` plus `acceptance-p<k>` (viewer, body, assignees,
comments). A write is then appended as a JSON line to $ACCEPT_ISSUE_WRITE_LOG instead.
"""
# SYNC: mirrored in dmc-268-{ui,api}-t6/.agents/skills/accept-issue/scripts/acceptance.py

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn

sys.path.insert(0, str(Path(__file__).resolve().parent))
import board_status  # noqa: E402
import issue_facts  # noqa: E402

SCHEMA = 1
WRITE_LOG_ENV = "ACCEPT_ISSUE_WRITE_LOG"
GH_TIMEOUT = 120  # seconds per gh call
MAX_PAGES = 200

TABLE_MARK = "<!-- accept-issue:table v1 -->"
ACCEPTED_MARK = "<!-- accept-issue:accepted v1 -->"
RETURNED_MARK = "<!-- accept-issue:returned v1 -->"
MARKS = (TABLE_MARK, ACCEPTED_MARK, RETURNED_MARK)
NOT_DONE = "не выполнено"
MOVED_REF = r"(?:[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)?#[1-9][0-9]*"
MOVED_TO = re.compile(MOVED_REF)
MOVED_NOTE = re.compile(rf"перенесён в ({MOVED_REF})")  # the body item and the table cell
STATUSES = ("done", "not_done", "moved")

RULE = (
    "Невыполненный AC или DoD доделывается в том же issue: кодовый — новым PR с `Refs #N`, "
    "некодовый — действием и доказательством (правило «Доработка — в том же issue», "
    "`docs/CONTRIBUTING.md`, «Приёмка задачи»)."
)
NEXT = {
    ("table", "ac_check"): "The table is posted for the acceptor: the acceptor runs accept next.",
    ("table", "not_done"): (
        "Rework stays in this issue: a code AC -> a new PR with Refs, "
        "a non-code AC -> an action plus an evidence comment; no follow-up issue, no "
        "«Хвосты». Then run table again."
    ),
    ("table", "left"): (
        "The card stays in AC Check and the acceptor returns it to In Progress. Rework stays "
        "in this issue: a code AC -> a new PR with Refs, a non-code AC -> "
        "an action plus an evidence comment; no follow-up issue, no «Хвосты». Then run table again."
    ),
    ("table", "stop"): "Fix the stop reasons, then run table again.",
    ("accept", "accept"): (
        "With --apply the issue is closed as completed; Done on the board is set by the "
        "workflow «Item closed», not by this script."
    ),
    ("accept", "return"): (
        "The table is not acceptable as it stands: run return (add --fail ID=reason for "
        "evidence that did not hold)."
    ),
    ("accept", "stop"): "Do not close the issue; fix the stop reasons first.",
    ("return", "return"): (
        "The card goes back to In Progress; the executor reworks in the same issue and "
        "posts a new table."
    ),
    ("return", "stop"): "Nothing is returned; fix the stop reasons first.",
}
FOLLOWUP_ON_UNMET = "follow-up на невыполненный AC или DoD не заводится — доработка в том же issue"
CHECKBOX_RAW = re.compile(r"^(\s*[-*+] \[) (\].*)$", re.DOTALL)
SEPARATOR_CELL = re.compile(r":?-+:?")
ITEM_ID = re.compile(r"(AC|DoD)\s*([1-9][0-9]*)", re.IGNORECASE)
READBACK_ATTEMPTS = 3
READBACK_PAUSE = 1.0  # seconds between read-back attempts; 0 under the offline seam

ACCEPTANCE_QUERY = """
query($owner: String!, $name: String!, $number: Int!, $c: String) {
  viewer { login }
  repository(owner: $owner, name: $name) {
    issue(number: $number) {
      body
      assignees(first: 100) { nodes { login } }
      comments(first: 100, after: $c) {
        pageInfo { hasNextPage endCursor }
        nodes { databaseId author { login } createdAt body }
      }
    }
  }
}
"""

PARENT_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    issue(number: $number) { parent { number repository { nameWithOwner } } }
  }
}
"""

Json = dict[str, Any]


WRITTEN: list[str] = []  # kinds of the writes made so far in this run, in order


def written_note() -> str:
    return f" (written: {', '.join(WRITTEN)})" if WRITTEN else ""


# Every exit 3 after the first write names what was written, in this script and in the
# facts / board modules it calls: still exactly one JSON error line.
issue_facts.exit3_note = written_note
board_status.exit3_note = written_note


def fail(message: str, code: int) -> NoReturn:
    issue_facts.fail(message, code)


# ─── fetch ──────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Comment:
    id: int
    author: str
    created_at: str
    body: str


@dataclass(frozen=True)
class Item:
    id: str  # AC1.. / DoD1..
    kind: str  # "ac" | "dod"
    text: str
    checked: bool
    line: int  # index of the raw line in body.splitlines()


@dataclass(frozen=True)
class Snapshot:
    repo: str
    number: int
    facts: Json
    body: str
    items: list[Item]
    assignees: list[str]
    viewer: str
    comments: list[Comment]

    @property
    def ref(self) -> str:
        return issue_facts.ref_str(self.repo, self.number)


def fetch_acceptance(
    repo: str, number: int, prefix: str = ""
) -> tuple[str, str, list[str], list[Comment]]:
    """(viewer, body, assignees, comments) of the issue, every comment page."""
    viewer = ""
    body = ""
    assignees: list[str] = []
    comments: list[Comment] = []
    cursor: str | None = None
    for page in range(1, MAX_PAGES + 1):
        data = issue_facts.request(
            f"{prefix}acceptance-p{page}", ACCEPTANCE_QUERY, repo, number, cursor
        )
        repository = issue_facts.obj(data.get("repository"), "repository")
        raw = repository.get("issue")
        if raw is None:
            fail(f"issue {issue_facts.ref_str(repo, number)} not found", 3)
        issue = issue_facts.obj(raw, "issue")
        if page == 1:
            viewer = issue_facts.text_of(
                issue_facts.obj(data.get("viewer"), "viewer"), "login", "viewer"
            )
            raw_body = issue.get("body")
            if raw_body is not None and not isinstance(raw_body, str):
                fail("unexpected shape: issue.body is not a string or null", 3)
            body = raw_body or ""
            raw_assignees = issue_facts.obj(issue.get("assignees"), "issue.assignees").get("nodes")
            if not isinstance(raw_assignees, list):
                fail("unexpected shape: issue.assignees.nodes is not a list", 3)
            assignees = [
                issue_facts.text_of(issue_facts.obj(n, "assignees[]"), "login", "assignees[]")
                for n in raw_assignees
            ]
        nodes, more, cursor = issue_facts.nodes_of(issue.get("comments"), "issue.comments")
        for node in nodes:
            comment_id = node.get("databaseId")
            if not isinstance(comment_id, int):
                fail("unexpected shape: comment.databaseId is not an integer", 3)
            author = node.get("author")
            login = (
                issue_facts.text_of(issue_facts.obj(author, "comment.author"), "login", "author")
                if author is not None
                else "ghost"
            )
            comments.append(
                Comment(
                    comment_id,
                    login,
                    issue_facts.text_of(node, "createdAt", "comment"),
                    issue_facts.text_of(node, "body", "comment"),
                )
            )
        if not more:
            return viewer, body, assignees, comments
    fail(f"issue comments have more than {MAX_PAGES} pages", 3)


def body_items(body: str) -> list[Item]:
    """The AC and DoD checkboxes of the body, with the ids the table uses."""
    counts = {"ac": 0, "dod": 0}
    items: list[Item] = []
    for line, section, checked, text in issue_facts.scan_checkboxes(body):
        if section is None:
            continue
        counts[section] += 1
        prefix = "AC" if section == "ac" else "DoD"
        items.append(Item(f"{prefix}{counts[section]}", section, text, checked, line))
    return items


def snapshot(repo: str, number: int) -> Snapshot:
    facts = issue_facts.collect(repo, number)
    viewer, body, assignees, comments = fetch_acceptance(repo, number)
    return Snapshot(repo, number, facts, body, body_items(body), assignees, viewer, comments)


# ─── writes ─────────────────────────────────────────────────────────────────
def seam() -> bool:
    return issue_facts.fixture_dir() is not None


def require_write_log() -> None:
    """Called before any write of an `--apply` run: the offline seam never falls
    through to gh, so it needs a log to write to."""
    if seam() and not os.environ.get(WRITE_LOG_ENV):
        fail(f"--apply against {issue_facts.FIXTURE_ENV} needs {WRITE_LOG_ENV}", 3)


def write(op: Json, argv: list[str]) -> None:
    """One write: a JSON line in the write log under the seam, else `gh api` REST."""
    if seam():
        log = os.environ.get(WRITE_LOG_ENV)
        if not log:
            fail(f"--apply against {issue_facts.FIXTURE_ENV} needs {WRITE_LOG_ENV}", 3)
        with open(log, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(op, sort_keys=True, ensure_ascii=False) + "\n")
        return
    try:
        proc = subprocess.run(
            ["gh", "api", *argv], capture_output=True, encoding="utf-8", timeout=GH_TIMEOUT
        )
    except subprocess.TimeoutExpired:
        fail(f"gh api {op['op']} timed out after {GH_TIMEOUT} s", 3)
    except (OSError, ValueError) as exc:
        fail(f"cannot run gh api {op['op']}: {exc}", 3)
    if proc.returncode != 0:
        fail(f"gh api {op['op']} exited {proc.returncode}: {proc.stderr.strip()}", 3)


def post_comment(repo: str, number: int, body: str) -> None:
    op = {"body": body, "issue": number, "op": "comment", "repo": repo}
    write(op, [f"repos/{repo}/issues/{number}/comments", "-f", f"body={body}"])
    WRITTEN.append("comment")


def set_card(repo: str, number: int, status: str) -> str:
    """Move the card and read the Status back (the log line comes first)."""
    ref = issue_facts.ref_str(repo, number)
    if seam():
        write({"op": "set-status", "ref": ref, "status": status}, [])
    return board_status.set_status(
        repo, number, status, on_written=lambda: WRITTEN.append("set-status")
    )


# ─── rows and the table ─────────────────────────────────────────────────────
def esc(text: str) -> str:
    """A table cell: `|` escaped, line breaks become blanks."""
    return " ".join(text.replace("|", "\\|").split())


def load_rows(path: str) -> dict[str, Any]:
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except (OSError, ValueError) as exc:
        fail(f"cannot read ROWS_JSON {path}: {getattr(exc, 'strerror', None) or exc}", 2)
    try:
        spec = json.loads(raw)
    except json.JSONDecodeError as exc:
        fail(f"ROWS_JSON {path} is not JSON: {exc}", 2)
    if not isinstance(spec, dict):
        fail(f"ROWS_JSON {path} must be an object {{id: {{status, evidence}}}}", 2)
    return spec


def build_rows(items: list[Item], spec: dict[str, Any]) -> tuple[list[Json], list[str]]:
    """(rows, problems): one row per body item from the author's `spec`; a problem is
    a stop, and then no rows are returned."""
    problems: list[str] = []
    known = {item.id for item in items}
    for key in spec:
        if key not in known:
            problems.append(f"{key}: no such AC or DoD in the issue body")
    rows: list[Json] = []
    for item in items:
        entry = spec.get(item.id)
        if entry is None:
            problems.append(f"{item.id}: no row for this item")
            continue
        if not isinstance(entry, dict):
            problems.append(f"{item.id}: the row must be an object")
            continue
        status = entry.get("status")
        if status not in STATUSES:
            problems.append(f"{item.id}: status must be done, not_done or moved")
            continue
        evidence = ""
        if status == "done":
            given = entry.get("evidence")
            if not isinstance(given, str) or not given.strip():
                problems.append(f"{item.id}: status done needs a non-empty evidence")
                continue
            evidence = given.strip()
            read_as = evidence_status(evidence)[0]
            if read_as != "done":
                problems.append(
                    f"{item.id}: this evidence reads as {read_as} in the table; use status "
                    f"{read_as} or write another evidence"
                )
                continue
        elif status == "moved":
            target = entry.get("to")
            if not isinstance(target, str) or not MOVED_TO.fullmatch(target):
                problems.append(f'{item.id}: status moved needs "to": "#M"')
                continue
            note = MOVED_NOTE.search(item.text)
            if note is None or note.group(1) != target:
                problems.append(
                    f"{item.id}: status moved needs «перенесён в {target}» in the issue body "
                    "item; the tech lead writes it, the executor cannot move an AC"
                )
                continue
            evidence = f"перенесён в {target}"
        else:
            evidence = NOT_DONE
        rows.append(
            {
                "evidence": evidence,
                "id": item.id,
                "kind": item.kind,
                "status": status,
                "text": item.text,
            }
        )
    return ([], problems) if problems else (rows, [])


def render_table(rows: list[Json], card: str | None) -> str:
    lines = [
        TABLE_MARK,
        "",
        "## Самопроверка: AC → доказательство",
        "",
        "| # | Пункт | Доказательство |",
        "| --- | --- | --- |",
    ]
    for row in rows:
        lines.append(f"| {row['id']} | {esc(row['text'])} | {esc(row['evidence'])} |")
    missing = [row["id"] for row in rows if row["status"] == "not_done"]
    lines.append("")
    if missing and card == "AC Check":
        lines.append(
            f"Итог: не выполнено — {', '.join(missing)}. Карточка остаётся в AC Check: "
            "принимающий вернёт её в In Progress; доработка идёт в этом же issue."
        )
    elif missing:
        lines.append(
            f"Итог: не выполнено — {', '.join(missing)}. AC Check не ставлю, "
            "доработка идёт в этом же issue."
        )
    else:
        lines.append("Итог: «не выполнено» нет, ставлю AC Check.")
    return "\n".join(lines) + "\n"


def latest_marked(comments: list[Comment], marker: str) -> Comment | None:
    marked = [c for c in comments if c.body.lstrip().startswith(marker)]
    return marked[-1] if marked else None


def already_posted(comments: list[Comment], marker: str, body: str) -> Comment | None:
    """The latest script comment (any of the three markers) when it is of this `marker`
    and says the same: only then is posting `body` again a duplicate."""
    scripted = [c for c in comments if c.body.lstrip().startswith(MARKS)]
    if not scripted:
        return None
    last = scripted[-1]
    same = last.body.lstrip().startswith(marker) and last.body.strip() == body.strip()
    return last if same else None


# ─── the board ──────────────────────────────────────────────────────────────
def board_block(
    action: str = "none",
    status_from: str | None = None,
    to: str | None = None,
    readback: str | None = None,
) -> Json:
    return {"action": action, "from": status_from, "readback": readback, "to": to}


def plan_card(not_done: bool, status: str | None) -> tuple[str, str | None, str | None, str | None]:
    """(action, to, stop, warning) for the author's card move (D5 in the plan)."""
    if not_done:
        if status == "On Review":
            return ("set", "In Progress", None, None)
        if status == "In Progress":
            return ("none", "In Progress", None, None)
        if status == "AC Check":
            return ("left", None, None, None)
        return ("none", None, None, f"the card is in {status or 'no Status'}: not moved")
    if status in ("On Review", "In Progress"):
        return ("set", "AC Check", None, None)
    if status == "AC Check":
        return ("none", "AC Check", None, None)
    stop = (
        f"the card is in {status or 'no Status'}: AC Check is set from On Review, "
        "In Progress or AC Check"
    )
    return ("none", None, stop, None)


# ─── table ──────────────────────────────────────────────────────────────────
def table_output(snap: Snapshot, apply: bool) -> Json:
    return {
        "ac_check": False,
        "apply": apply,
        "board": board_block(),
        "comment": None,
        "issue": snap.ref,
        "mode": "author",
        "next": NEXT[("table", "stop")],
        "rework_rule": None,
        "rows": [],
        "schema": SCHEMA,
        "stops": [],
        "verdict": "stop",
        "viewer": snap.viewer,
        "warnings": [],
    }


def run_table(ref: str, rows_path: str, apply: bool) -> Json:
    WRITTEN.clear()
    repo, number = issue_facts.parse_ref(ref)
    spec = load_rows(rows_path)
    if apply:
        require_write_log()
    snap = snapshot(repo, number)
    out = table_output(snap, apply)
    if snap.facts["issue"]["state"] == "CLOSED":
        out["stops"] = [f"issue {snap.ref} is CLOSED: the self-check applies to an open issue"]
        return out
    if not snap.items:
        out["stops"] = [f"issue {snap.ref} has no AC or DoD checkboxes: there is nothing to check"]
        return out
    rows, problems = build_rows(snap.items, spec)
    if problems:
        out["stops"] = problems
        return out
    out["rows"] = rows
    warnings = [
        f"PR {pr['ref']} is OPEN and has a Refs line naming this issue: "
        "the self-check follows the last PR of the task"
        for pr in snap.facts["prs"]
        if pr["refs"] and pr["state"] == "OPEN"
    ]
    if snap.viewer not in snap.assignees:
        listing = ", ".join(snap.assignees) or "none"
        warnings.append(f"viewer {snap.viewer} is not an assignee (assignees: {listing})")
    not_done = any(row["status"] == "not_done" for row in rows)
    status = board_status.read_status(repo, number)
    action, to, stop, card_warning = plan_card(not_done, status)
    if card_warning:
        warnings.append(card_warning)
    out["warnings"] = warnings
    out["board"] = board_block(action, status, to)
    if stop:
        out["board"] = board_block("none", status)
        out["stops"] = [stop]
        return out
    comment = render_table(rows, status)
    out["comment"] = comment
    out["verdict"] = "not_done" if not_done else "ac_check"
    out["ac_check"] = not not_done
    out["rework_rule"] = RULE if not_done else None
    out["next"] = NEXT[("table", "left" if action == "left" else out["verdict"])]
    if apply:
        latest = already_posted(snap.comments, TABLE_MARK, comment)
        if latest is not None:
            out["warnings"].append(
                f"the same table is already the latest table comment (#{latest.id}): "
                "not posted again"
            )
        else:
            post_comment(repo, number, comment)
        if action == "set" and to is not None:
            out["board"]["readback"] = set_card(repo, number, to)
    return out


# ─── the executor's table ───────────────────────────────────────────────────
class TableError(Exception):
    """The table comment cannot be found or read; the message is a stop."""


def split_row(line: str) -> list[str] | None:
    """The cells of a markdown table row (`\\|` unescaped), None for a non-row."""
    text = line.strip()
    if not text.startswith("|"):
        return None
    text = text[1:]
    if text.endswith("|") and not text.endswith("\\|"):
        text = text[:-1]
    cells: list[str] = []
    current: list[str] = []
    i = 0
    while i < len(text):
        if text[i] == "\\" and i + 1 < len(text) and text[i + 1] == "|":
            current.append("|")
            i += 2
            continue
        if text[i] == "|":
            cells.append("".join(current).strip())
            current = []
        else:
            current.append(text[i])
        i += 1
    cells.append("".join(current).strip())
    return cells


def table_rows(body: str) -> list[list[str]]:
    """The data rows of every markdown table in a comment (header and separator rows
    dropped)."""
    rows: list[list[str]] = []
    for line in body.splitlines():
        cells = split_row(line)
        if cells is None:
            continue
        if any(cells) and all(SEPARATOR_CELL.fullmatch(c) for c in cells if c):
            continue
        if cells[0].strip("* ") == "#":
            continue
        rows.append(cells)
    return rows


def squash(text: str) -> str:
    return " ".join(text.replace("`", "").replace("*", "").split()).casefold()


def evidence_status(cell: str) -> tuple[str, str]:
    """(status, evidence) of an evidence cell: done | not_done | moved | missing."""
    evidence = cell.strip()
    plain = evidence.strip("*_ ").casefold()
    if not plain:
        return ("missing", "")
    if plain.startswith(NOT_DONE):
        return ("not_done", evidence)
    if MOVED_NOTE.match(plain):
        return ("moved", evidence)
    return ("done", evidence)


def match_strict(
    cells_list: list[list[str]], items: list[Item], table_id: int
) -> dict[str, tuple[str, str]]:
    """{id: (item text in the table, evidence cell)} of a table this script wrote."""
    found: dict[str, tuple[str, str]] = {}
    known = {item.id for item in items}
    for cells in cells_list:
        if len(cells) != 3 or cells[0] not in known or cells[0] in found:
            raise TableError(
                f"table comment {table_id}: the row «{' | '.join(cells)}» is not "
                "«| id | text | evidence |» of an AC or DoD of this issue"
            )
        found[cells[0]] = (cells[1], cells[2])
    return found


def match_tolerant(cells_list: list[list[str]], items: list[Item]) -> dict[str, tuple[str, str]]:
    """{id: (item text, evidence cell)} of a hand-written table: a row belongs to the item
    named by `AC<n>` / `DoD<n>` in its first cell, else to the item whose text one of its
    cells holds; the evidence is the last cell."""
    found: dict[str, tuple[str, str]] = {}
    by_id = {item.id.casefold(): item for item in items}
    for cells in cells_list:
        if len(cells) < 2:
            continue
        item: Item | None = None
        named = ITEM_ID.fullmatch(cells[0].strip("*_ "))
        if named:
            item = by_id.get(f"{named.group(1)}{named.group(2)}".casefold())
        if item is None:
            for candidate in items:
                wanted = squash(candidate.text)
                held = [squash(c) for c in cells[:-1]]
                if any(c == wanted or (len(c) >= 12 and wanted.startswith(c)) for c in held):
                    item = candidate
                    break
        if item is not None and item.id not in found:
            found[item.id] = (item.text, cells[-1])
    return found


@dataclass(frozen=True)
class TableRead:
    comment: Comment
    via: str  # "marker" | "flag"
    rows: list[Json]
    returned: list[Json]  # the return items the table itself gives


def read_table(snap: Snapshot, table_comment: int | None) -> TableRead:
    """Find the executor's table and read it against the body items; TableError when
    there is none or it cannot be read."""
    if table_comment is None:
        marked = latest_marked(snap.comments, TABLE_MARK)
        if marked is None:
            raise TableError(
                f"no executor table found: no comment with the marker {TABLE_MARK} and no "
                "--table-comment ID; give the ID of a hand-written table with --table-comment"
            )
        comment, via = marked, "marker"
    else:
        named = [c for c in snap.comments if c.id == table_comment]
        if not named:
            raise TableError(f"comment {table_comment} not found in the issue")
        comment, via = named[0], "flag"
    cells_list = table_rows(comment.body)
    if not cells_list:
        raise TableError(f"comment {comment.id} has no table")
    if via == "marker":
        found = match_strict(cells_list, snap.items, comment.id)
    else:
        found = match_tolerant(cells_list, snap.items)
    rows: list[Json] = []
    returned: list[Json] = []
    for item in snap.items:
        entry = found.get(item.id)
        status, evidence = "missing", ""
        reason: str | None = "в таблице нет строки"
        if entry is not None:
            status, evidence = evidence_status(entry[1])
            reason = None
            seen = " ".join(entry[0].split())
            if via == "marker" and not " ".join(item.text.split()).startswith(seen):
                status, reason = "stale", "пункт в теле issue изменён после таблицы"
            elif status == "missing":
                reason = "в таблице нет доказательства"
            elif status == "not_done":
                reason = NOT_DONE
            elif status == "moved":
                target = MOVED_NOTE.search(evidence.casefold())
                note = MOVED_NOTE.search(item.text)
                wanted = target.group(1) if target else ""
                if note is None or note.group(1).casefold() != wanted:
                    reason = f"перенесён в {wanted}, но такой пометки техлида в теле issue нет"
        rows.append(
            {
                "evidence": evidence,
                "id": item.id,
                "kind": item.kind,
                "status": status,
                "text": item.text,
            }
        )
        if reason is not None:
            returned.append({"id": item.id, "reason": reason, "text": item.text})
    return TableRead(comment, via, rows, returned)


# ─── the accepter's comments and writes ─────────────────────────────────────
def comment_url(snap: Snapshot, comment_id: int) -> str:
    return f"https://github.com/{snap.repo}/issues/{snap.number}#issuecomment-{comment_id}"


def short_ref(snap: Snapshot, repo: str, number: int) -> str:
    return f"#{number}" if repo == snap.repo else issue_facts.ref_str(repo, number)


def render_accepted(snap: Snapshot, rows: list[Json], table_id: int, followups: list[str]) -> str:
    def line(row: Json) -> str:
        # a moved row says it once: its body item already carries «перенесён в #M»
        if row["status"] == "moved":
            return f"- {row['id']} — {row['text']}"
        return f"- {row['id']} — {row['text']} — выполнено"

    lines = [
        ACCEPTED_MARK,
        "",
        "## Приёмка: issue закрыт",
        "",
        f"Таблица исполнителя: {comment_url(snap, table_id)}. "
        "Чекбоксы AC и DoD в теле issue отмечены.",
        "",
        "**Acceptance Criteria**",
        "",
        *[line(r) for r in rows if r["kind"] == "ac"],
        "",
        "**DoD Checklist**",
        "",
        *[line(r) for r in rows if r["kind"] == "dod"],
    ]
    if followups:
        lines += ["", f"Follow-up вне AC (новый скоуп): {', '.join(followups)}"]
    return "\n".join(lines) + "\n"


def render_returned(snap: Snapshot, items: list[Json], table_id: int) -> str:
    lines = [
        RETURNED_MARK,
        "",
        "## Приёмка: возврат в In Progress",
        "",
        f"Issue не закрыт. Таблица исполнителя: {comment_url(snap, table_id)}.",
        "",
        "**Что осталось**",
        "",
        *[f"- {i['id']} — {i['text']} — {i['reason']}" for i in items],
        "",
        f"Доработка — в том же issue: кодовый AC — новым PR с `Refs #{snap.number}`, некодовый — "
        "действием и доказательством комментарием. Затем исполнитель дополняет таблицу "
        "новым комментарием и снова ставит AC Check.",
    ]
    return "\n".join(lines) + "\n"


class TickError(Exception):
    """A box cannot be ticked safely; the message is a stop."""


def tick_body(body: str, items: list[Item], ids: list[str]) -> str:
    """`body` with the boxes of `ids` ticked and nothing else changed (line endings kept).
    A raw line that is not a plain unchecked box raises TickError."""
    lines = body.splitlines(keepends=True)
    by_id = {item.id: item for item in items}
    for item_id in ids:
        index = by_id[item_id].line
        match = CHECKBOX_RAW.match(lines[index])
        if match is None:
            raise TickError(
                f"cannot tick {item_id}: line {index + 1} of the body is not a plain checkbox"
            )
        lines[index] = f"{match.group(1)}x{match.group(2)}"
    return "".join(lines)


def patch_issue(repo: str, number: int, fields: dict[str, str], op: Json) -> None:
    argv = [f"repos/{repo}/issues/{number}", "-X", "PATCH"]
    for key, value in fields.items():
        argv += ["-f", f"{key}={value}"]
    write({**op, "issue": number, "repo": repo}, argv)
    WRITTEN.append(str(op["op"]))


def check_parent(snap: Snapshot, ref: str) -> tuple[Json | None, str | None]:
    """({ref, parent}, None), or (None, the stop) when the follow-up's native parent is
    not this issue."""
    repo, number = issue_facts.parse_ref(ref)
    follow = issue_facts.ref_str(repo, number)
    name = f"parent-{repo.replace('/', '_')}-{number}"
    data = issue_facts.request(name, PARENT_QUERY, repo, number, None)
    repository = issue_facts.obj(data.get("repository"), "repository")
    raw = repository.get("issue")
    if raw is None:
        fail(f"follow-up {follow} not found", 3)
    parent = issue_facts.obj(raw, "issue").get("parent")
    if parent is None:
        return (None, f"follow-up {follow} has no parent, not this issue {snap.ref}")
    node = issue_facts.obj(parent, "issue.parent")
    parent_number = node.get("number")
    if not isinstance(parent_number, int):
        fail("unexpected shape: issue.parent.number is not an integer", 3)
    parent_repo = issue_facts.text_of(
        issue_facts.obj(node.get("repository"), "issue.parent.repository"),
        "nameWithOwner",
        "issue.parent.repository",
    )
    parent_ref = issue_facts.ref_str(parent_repo, parent_number)
    if parent_ref.casefold() != snap.ref.casefold():
        return (None, f"follow-up {follow} has the parent {parent_ref}, not this issue {snap.ref}")
    return ({"parent": parent_ref, "ref": follow}, None)


# ─── accept and return ──────────────────────────────────────────────────────
def accepter_output(snap: Snapshot, command: str, apply: bool) -> Json:
    return {
        "after_table": [],
        "apply": apply,
        "board": board_block(),
        "comment": None,
        "followups": [],
        "issue": snap.ref,
        "mode": "accepter",
        "next": NEXT[(command, "stop")],
        "readback": None,
        "return_items": [],
        "rework_rule": None,
        "rows": [],
        "schema": SCHEMA,
        "stops": [],
        "table": None,
        "tick": [],
        "verdict": "stop",
        "viewer": snap.viewer,
        "warnings": [],
    }


def closed_stop(snap: Snapshot, command: str) -> str:
    closing = snap.facts["closing"]
    closer = closing["last_closer"]
    if closer == "manual":
        return f"issue {snap.ref} is already CLOSED by a manual close: nothing to {command}"
    closes = [e for e in closing["events"] if e["kind"] == "closed"]
    shown = closer or "an unknown closer"
    if closes and closes[-1].get("closer_ref"):
        shown += f" ({closes[-1]['closer_ref']})"
    return (
        f"issue {snap.ref} is CLOSED and its last close was by {shown}, not a manual close: "
        "the tech lead reopens it (docs/CONTRIBUTING.md, «Приёмка задачи»); "
        "this script never reopens an issue"
    )


def excerpt(text: str) -> str:
    first = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    return first if len(first) <= 80 else first[:79] + "…"


def accepter_warnings(
    snap: Snapshot, table: TableRead, status: str | None, returned: list[Json]
) -> list[str]:
    warnings: list[str] = []
    if status != "AC Check":
        warnings.append(f"the card is in {status or 'no Status'}, not AC Check")
    reasons = []
    if snap.viewer == table.comment.author:
        reasons.append("wrote the table")
    if snap.viewer in snap.assignees:
        reasons.append("is an assignee of the issue")
    if reasons:
        warnings.append(
            f"viewer {snap.viewer} {' and '.join(reasons)}; the acceptor is the tech lead or a "
            "participant they named (this script does not decide who that is)"
        )
    for pr in snap.facts["prs"]:
        if not (pr["refs"] and pr["state"] == "MERGED"):
            continue
        if pr["unresolved_threads"] > 0:
            warnings.append(
                f"PR {pr['ref']} has {pr['unresolved_threads']} unresolved review thread(s)"
            )
        if pr["ci"] != "SUCCESS":
            warnings.append(f"PR {pr['ref']} CI state is {pr['ci'] or 'none'}, not SUCCESS")
    if table.comment.author not in snap.assignees:
        listing = ", ".join(snap.assignees) or "none"
        warnings.append(
            f"the table author {table.comment.author} is not an assignee (assignees: {listing})"
        )
    unmet = {i["id"] for i in returned}
    for row in table.rows:
        note = MOVED_NOTE.search(row["text"])
        if row["status"] == "moved" and row["id"] not in unmet and note is not None:
            warnings.append(
                f"{row['id']} засчитан как перенесённый в {note.group(1)} по пометке в теле "
                "issue: проверьте, что пометку дописал техлид — тело issue может править "
                "и исполнитель"
            )
    return warnings


def parse_fail(values: list[str]) -> dict[str, str]:
    failed: dict[str, str] = {}
    for value in values:
        key, sep, reason = value.partition("=")
        if not sep or not key.strip() or not reason.strip():
            fail(f"bad --fail {value!r}: expected ID=reason", 2)
        failed[key.strip()] = reason.strip()
    return failed


def run_accepter(
    command: str,
    ref: str,
    table_comment: int | None,
    followups: list[str],
    failed: dict[str, str],
    apply: bool,
) -> Json:
    WRITTEN.clear()
    repo, number = issue_facts.parse_ref(ref)
    if apply:
        require_write_log()
    snap = snapshot(repo, number)
    out = accepter_output(snap, command, apply)
    stops: list[str] = out["stops"]
    if snap.facts["issue"]["state"] == "CLOSED":
        stops.append(closed_stop(snap, command))
        return out
    if not snap.items:
        stops.append(f"issue {snap.ref} has no AC or DoD checkboxes: there is nothing to {command}")
        return out
    for pr in snap.facts["prs"]:
        if pr["refs"] and pr["state"] == "OPEN":
            stops.append(
                f"PR {pr['ref']} is OPEN and has a Refs line naming this issue: "
                "the task is not finished, acceptance waits for its merge"
            )
            return out
    try:
        table = read_table(snap, table_comment)
    except TableError as exc:
        stops.append(str(exc))
        return out
    out["table"] = {
        "author": table.comment.author,
        "comment_id": table.comment.id,
        "url": comment_url(snap, table.comment.id),
        "via": table.via,
    }
    out["rows"] = table.rows
    early_warnings: list[str] = []
    later = snap.comments[snap.comments.index(table.comment) + 1 :]
    returned_after = [c for c in later if c.body.lstrip().startswith(RETURNED_MARK)]
    if returned_after:
        message = (
            f"таблица старше возврата #{returned_after[-1].id}: "
            "исполнитель не выложил новую таблицу "
            "(таблицу, выложенную вручную после возврата, укажите через --table-comment ID)"
        )
        if command == "accept":
            stops.append(message)
            return out
        early_warnings.append(message)
    unknown = [k for k in failed if k not in {item.id for item in snap.items}]
    if unknown:
        stops.extend(f"--fail {k}: no such AC or DoD in the issue body" for k in unknown)
        return out
    returned = {i["id"]: i for i in table.returned}
    for item in snap.items:
        if item.id in failed:
            returned[item.id] = {
                "id": item.id,
                "reason": f"доказательство не подтвердилось: {failed[item.id]}",
                "text": item.text,
            }
    out["return_items"] = [returned[i.id] for i in snap.items if i.id in returned]
    out["after_table"] = [
        {"author": c.author, "excerpt": excerpt(c.body), "id": c.id}
        for c in later
        if not c.body.lstrip().startswith(MARKS)
    ]
    verdict = "return" if out["return_items"] else "accept"
    if out["return_items"]:
        out["rework_rule"] = RULE
    if command == "accept" and followups and verdict == "return":
        stops.append(FOLLOWUP_ON_UNMET)
        return out
    if command == "return" and verdict == "accept":
        stops.append("nothing to return: the table has no «не выполнено» and no --fail was given")
        return out
    checked: list[Json] = []
    for follow in followups:
        found, stop = check_parent(snap, follow)
        if found is None:
            stops.append(stop or "follow-up check failed")
            return out
        checked.append(found)
    tick = [item.id for item in snap.items if not item.checked]
    ticked = snap.body
    if command == "accept" and verdict == "accept":
        try:
            ticked = tick_body(snap.body, snap.items, tick)
        except TickError as exc:
            stops.append(str(exc))
            return out
    status = board_status.read_status(repo, number)
    out["warnings"] = early_warnings + accepter_warnings(snap, table, status, out["return_items"])
    out["verdict"] = verdict
    out["next"] = NEXT[(command, verdict)]
    if command == "return":
        target = "In Progress"
        out["board"] = board_block("none" if status == target else "set", status, target)
        out["comment"] = render_returned(snap, out["return_items"], table.comment.id)
        if apply:
            apply_return(snap, out)
        return out
    out["board"] = board_block("none", status)
    if verdict == "return":
        if apply:  # `accept --apply` never returns: nothing is written
            out["verdict"] = "stop"
            out["stops"] = ["the verdict is return, not accept: nothing is written; run return"]
            out["next"] = NEXT[("accept", "stop")]
        return out
    out["followups"] = checked
    out["tick"] = tick
    shown = [short_ref(snap, *issue_facts.parse_ref(f["ref"])) for f in checked]
    out["comment"] = render_accepted(snap, table.rows, table.comment.id, shown)
    if apply:
        out["readback"] = apply_accept(snap, out, ticked)
    return out


def readback_once(snap: Snapshot, ticked: str) -> tuple[Json, list[str]]:
    """One read-back of the close: (the numbers, the problems)."""
    prefix = "after-" if seam() else ""
    facts = issue_facts.collect(snap.repo, snap.number, prefix)
    boxes = facts["checkboxes"]
    total = boxes["ac"]["total"] + boxes["dod"]["total"]
    checked = boxes["ac"]["checked"] + boxes["dod"]["checked"]
    readback = {
        "checked": checked,
        "last_closer": facts["closing"]["last_closer"],
        "state": facts["issue"]["state"],
        "total": total,
        "unchecked": total - checked,
    }
    problems = []
    if readback["state"] != "CLOSED" or readback["last_closer"] != "manual":
        problems.append(
            f"the issue reads back as {readback['state']} (closer {readback['last_closer']}), "
            "not CLOSED by a manual close"
        )
    if readback["unchecked"] != 0 or total == 0:
        problems.append(f"{readback['unchecked']} of {total} AC / DoD boxes are unchecked")
    if boxes["outside"] != snap.facts["checkboxes"]["outside"]:
        problems.append("the number of checkboxes outside AC / DoD changed")
    if fetch_acceptance(snap.repo, snap.number, prefix)[1] != ticked:
        problems.append("the body reads back different from the ticked body")
    return readback, problems


def retry_readback(
    check: Callable[[], tuple[Json, list[str]]],
    pause: float,
    sleep: Callable[[float], None],
) -> tuple[Json, list[str]]:
    """`check` up to READBACK_ATTEMPTS times, `pause` seconds apart (GitHub may not show
    the close at once); the last answer when none is clean."""
    answer = check()
    for _ in range(READBACK_ATTEMPTS - 1):
        if not answer[1]:
            break
        if pause > 0:
            sleep(pause)
        answer = check()
    return answer


def apply_accept(snap: Snapshot, out: Json, ticked: str) -> Json:
    """tick -> closing comment -> close -> read-back; returns the read-back. Everything
    that can refuse has been checked before the first write."""
    _, body_now, _, comments_now = fetch_acceptance(snap.repo, snap.number)
    if body_now != snap.body:
        fail(
            "the issue body changed since the verdict was computed; nothing was written, "
            "run accept again",
            3,
        )
    if ticked != snap.body:
        patch_issue(snap.repo, snap.number, {"body": ticked}, {"body": ticked, "op": "patch-body"})
    latest = already_posted(comments_now, ACCEPTED_MARK, out["comment"])
    if latest is not None:
        out["warnings"].append(
            f"the same closing comment is already the latest one (#{latest.id}): not posted again"
        )
    else:
        post_comment(snap.repo, snap.number, out["comment"])
    patch_issue(
        snap.repo,
        snap.number,
        {"state": "closed", "state_reason": "completed"},
        {"op": "close", "state_reason": "completed"},
    )
    readback, problems = retry_readback(
        lambda: readback_once(snap, ticked), 0.0 if seam() else READBACK_PAUSE, time.sleep
    )
    if problems:
        fail("read-back failed after the writes: " + "; ".join(problems), 3)
    return readback


def apply_return(snap: Snapshot, out: Json) -> None:
    """returned comment -> card to In Progress (read back)."""
    comments_now = fetch_acceptance(snap.repo, snap.number)[3]
    latest = already_posted(comments_now, RETURNED_MARK, out["comment"])
    if latest is not None:
        out["warnings"].append(
            f"the same returned comment is already the latest one (#{latest.id}): not posted again"
        )
    else:
        post_comment(snap.repo, snap.number, out["comment"])
    if out["board"]["action"] == "set":
        out["board"]["readback"] = set_card(snap.repo, snap.number, out["board"]["to"])


# ─── main ───────────────────────────────────────────────────────────────────
class Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        fail(message, 2)


def build_parser() -> Parser:
    parser = Parser(
        prog="acceptance.py", description="Self-check table and acceptance of an issue."
    )
    sub = parser.add_subparsers(dest="command", required=True, parser_class=Parser)
    ref_help = "api#N | ui#N | OWNER/NAME#N | N"
    table = sub.add_parser("table", help="the author's self-check table")
    table.add_argument("ref", metavar="REF", help=ref_help)
    table.add_argument("rows", metavar="ROWS_JSON", help="evidence per AC / DoD id")
    table.add_argument("--apply", action="store_true", help="write (default: dry run)")
    accept = sub.add_parser("accept", help="tick, close and name every AC")
    return_ = sub.add_parser("return", help="send the card back with what is left")
    for parser_ in (accept, return_):
        parser_.add_argument("ref", metavar="REF", help=ref_help)
        parser_.add_argument(
            "--table-comment",
            metavar="ID",
            type=int,
            help="read the table of this comment (default: the latest marker comment)",
        )
        parser_.add_argument("--apply", action="store_true", help="write (default: dry run)")
    accept.add_argument(
        "--followup",
        metavar="REF",
        action="append",
        default=[],
        help="a follow-up filed for a finding outside the AC (its parent must be this issue)",
    )
    return_.add_argument(
        "--fail",
        metavar="ID=reason",
        action="append",
        default=[],
        help="an AC / DoD whose evidence did not hold",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "table":
        out = run_table(args.ref, args.rows, args.apply)
    elif args.command == "accept":
        out = run_accepter("accept", args.ref, args.table_comment, args.followup, {}, args.apply)
    else:
        failed = parse_fail(args.fail)
        out = run_accepter("return", args.ref, args.table_comment, [], failed, args.apply)
    sys.stdout.write(json.dumps(out, sort_keys=True, indent=2, ensure_ascii=False) + "\n")
    return 1 if out["verdict"] == "stop" else 0


if __name__ == "__main__":
    sys.exit(main())
