#!/usr/bin/env python3
"""board_status.py — set the Status of an issue on the course board, then read it back.

Usage
-----
    board_status.py REF STATUS
    REF    := api#N | ui#N | OWNER/NAME#N | N        (a bare N is api#N)
    STATUS := an option of the board's Status field, e.g. "In Progress"

The board is project 12 of larchanka-training. Its id, the Status field id and the
option id are looked up by name on every run, never hard-coded. The issue must
already be on the board: this script never adds it. The only write is one
`updateProjectV2ItemFieldValue` on the issue's own item; setting the Status the
issue already has is harmless, so a run can be repeated.

Contract
--------
stdout carries one JSON document (`sort_keys`, indent 2, no ASCII escaping, one
trailing newline), the Status as read back after the write:

    {"board": 12, "ref": "owner/repo#N", "status": "In Progress"}

Exit 0 ok; 2 usage, bad REF, or a STATUS the board does not have (stderr lists the
options); 3 gh / API error (non-zero gh exit, timeout, GraphQL errors, board or
issue not found, issue not on the board, unexpected shape, a read-back that
differs from STATUS, missing fixture, fixture dir set but empty). On 2 and 3
stderr carries one JSON line {"error": "..."} and stdout stays empty.

Offline seam: with IMPLEMENT_ISSUE_FIXTURE_DIR set, gh is never called and every
request reads <dir>/<name>.json (the full {"data": ...} response) instead; set
but empty exits 3, never falls through to gh. Names: board (project, Status
field and options), item (the issue's board items), set-status (the mutation
response), readback (the issue's board items after the write).
"""
# SYNC: mirrored in dmc-268-{ui,api}-t6/.agents/skills/implement-issue/scripts/board_status.py

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, NoReturn

ALIASES = {
    "api": "larchanka-training/dmc-268-api-t6",
    "ui": "larchanka-training/dmc-268-ui-t6",
}
BOARD_OWNER = "larchanka-training"
BOARD_NUMBER = 12
FIXTURE_ENV = "IMPLEMENT_ISSUE_FIXTURE_DIR"
GH_TIMEOUT = 120  # seconds per gh call

REF = re.compile(r"(api|ui|[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#([1-9][0-9]*)")
NUMBER = re.compile(r"[1-9][0-9]*")

BOARD_QUERY = """
query($org: String!, $board: Int!) {
  organization(login: $org) {
    projectV2(number: $board) {
      id
      field(name: "Status") {
        __typename
        ... on ProjectV2SingleSelectField { id name options { id name } }
      }
    }
  }
}
"""
ITEM_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    issue(number: $number) {
      projectItems(first: 100) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id
          project { id }
          fieldValueByName(name: "Status") {
            __typename
            ... on ProjectV2ItemFieldSingleSelectValue { name }
          }
        }
      }
    }
  }
}
"""
SET_QUERY = """
mutation($project: ID!, $item: ID!, $field: ID!, $option: String!) {
  updateProjectV2ItemFieldValue(input: {
    projectId: $project, itemId: $item, fieldId: $field, value: {singleSelectOptionId: $option}
  }) { projectV2Item { id } }
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


def request(name: str, query: str, variables: dict[str, str | int]) -> dict[str, Any]:
    """One GraphQL request: gh, or the fixture `<name>.json` when the seam is set.
    Strings go with -f (never reinterpreted), integers with -F."""
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
        argv = ["gh", "api", "graphql", "-f", f"query={query}"]
        for key, value in variables.items():
            argv += ["-F" if isinstance(value, int) else "-f", f"{key}={value}"]
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


# ─── board ──────────────────────────────────────────────────────────────────
def load_board() -> tuple[str, str, dict[str, str]]:
    """(project id, Status field id, {option name: option id}) of the board."""
    data = request("board", BOARD_QUERY, {"org": BOARD_OWNER, "board": BOARD_NUMBER})
    organization = data.get("organization")
    project = obj(obj(organization, "organization").get("projectV2"), "organization.projectV2")
    field = obj(project.get("field"), "projectV2.field")
    if field.get("__typename") != "ProjectV2SingleSelectField":
        fail(f"board {BOARD_NUMBER} has no single-select field named Status", 3)
    options = field.get("options")
    if not isinstance(options, list):
        fail("unexpected shape: Status.options is not a list", 3)
    by_name = {
        text_of(obj(o, "Status.options[]"), "name", "Status.options[]"): text_of(
            obj(o, "Status.options[]"), "id", "Status.options[]"
        )
        for o in options
    }
    return text_of(project, "id", "projectV2"), text_of(field, "id", "projectV2.field"), by_name


def pick_option(wanted: str, options: dict[str, str]) -> str:
    """The option name matching `wanted` (exact, else case-insensitive), else exit 2."""
    if wanted in options:
        return wanted
    folded = [name for name in options if name.casefold() == wanted.casefold()]
    if len(folded) == 1:
        return folded[0]
    listing = ", ".join(repr(name) for name in options)
    fail(f"unknown Status {wanted!r}: the board's options are {listing}", 2)


def board_item(name: str, repo: str, number: int, project_id: str) -> tuple[str, str | None]:
    """(item id, Status name or None) of the issue on the board; exit 3 if absent."""
    owner, repo_name = repo.split("/", 1)
    variables: dict[str, str | int] = {"owner": owner, "name": repo_name, "number": number}
    data = request(name, ITEM_QUERY, variables)
    repository = obj(data.get("repository"), "repository")
    raw_issue = repository.get("issue")
    if raw_issue is None:
        fail(f"issue {repo}#{number} not found", 3)
    items = obj(obj(raw_issue, "issue").get("projectItems"), "issue.projectItems")
    nodes = items.get("nodes")
    if not isinstance(nodes, list):
        fail("unexpected shape: issue.projectItems.nodes is not a list", 3)
    if obj(items.get("pageInfo"), "issue.projectItems.pageInfo").get("hasNextPage") is True:
        fail("issue.projectItems has more than one page: the board item cannot be told", 3)
    for raw in nodes:
        node = obj(raw, "issue.projectItems[]")
        if obj(node.get("project"), "issue.projectItems[].project").get("id") != project_id:
            continue
        value = node.get("fieldValueByName")
        status = None
        if value is not None:
            status = text_of(obj(value, "fieldValueByName"), "name", "fieldValueByName")
        return text_of(node, "id", "issue.projectItems[]"), status
    fail(f"issue {repo}#{number} is not on board {BOARD_NUMBER}; this script never adds it", 3)


def set_status(repo: str, number: int, wanted: str) -> str:
    project_id, field_id, options = load_board()
    status = pick_option(wanted, options)
    item_id, _ = board_item("item", repo, number, project_id)
    variables: dict[str, str | int] = {
        "project": project_id,
        "item": item_id,
        "field": field_id,
        "option": options[status],
    }
    written = request("set-status", SET_QUERY, variables)
    obj(written.get("updateProjectV2ItemFieldValue"), "updateProjectV2ItemFieldValue")
    _, read_back = board_item("readback", repo, number, project_id)
    if read_back != status:
        fail(f"Status of {repo}#{number} reads back as {read_back!r}, not {status!r}", 3)
    return status


# ─── main ───────────────────────────────────────────────────────────────────
class Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        fail(message, 2)


def main(argv: list[str] | None = None) -> int:
    parser = Parser(prog="board_status.py", description="Set an issue's board Status.")
    parser.add_argument(
        "ref", metavar="REF", help="api#N | ui#N | OWNER/NAME#N | N (bare N = api#N)"
    )
    parser.add_argument("status", metavar="STATUS", help='a Status option, e.g. "In Progress"')
    args = parser.parse_args(argv)
    repo, number = parse_ref(args.ref)
    status = set_status(repo, number, args.status)
    result = {"board": BOARD_NUMBER, "ref": f"{repo}#{number}", "status": status}
    sys.stdout.write(json.dumps(result, sort_keys=True, indent=2, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
