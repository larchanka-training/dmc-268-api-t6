#!/usr/bin/env python3
"""pr_context.py — what a pull request review checks against: the pull request, the issues
its `Refs` lines name and their acceptance criteria.

Usage
-----
    pr_context.py REF
    REF := api#N | ui#N | OWNER/NAME#N | N        (a pull request; a bare N is api#N)

Read-only: one `gh api repos/OWNER/NAME/pulls/N --jq …` call, then one
`gh api repos/OWNER/NAME/issues/N --jq …` per issue a `Refs` line names. Nothing is written,
nothing is checked out.

Contract
--------
stdout carries one JSON document (`sort_keys`, indent 2, no ASCII escaping, one trailing
newline):

    {
      "schema": 1,
      "verdict": "go" | "stop",
      "reasons": [str],              # why the run stops; empty on go
      "warnings": [str],
      "fence": "~~~~~~",             # the brief's data fence, longer than any `~` run
      "pr": {"repo", "number", "url", "title", "author", "draft",
             "state": "open" | "closed" | "merged",
             "base_ref", "base_sha", "head_ref", "head_sha", "body"},
      "issues": [{"ref": "owner/name#N", "url", "title", "state": "open" | "closed",
                  "parts": [K],      # the parts the pull request names for this issue
                  "acceptance_criteria": [{"id": "AC<k>", "key": "<ref> AC<k>",
                                           "part": K | null, "text"}],
                  "body"}]
    }

The bodies are verbatim ("" when GitHub has none): the Spec axis reads the issue's Scope from
them, and the AC axis the pull request's own claims. The titles, bodies and criteria are
untrusted: the brief pastes them only between two `fence` lines, a run of `~` one longer than the
longest run in any title or body (at least six), so no line of them can close the block.

The criteria are the checkboxes of the issue's `## Acceptance Criteria`, numbered AC1.. in
the issue's order (the ids of accept-issue's table). When the pull request names parts of an
issue — in its title, else in the first mention in the first line of its `## What`, as
implement-issue reads it — only the criteria of those parts (in the forms of its
`references/parts.md`, the prefix cut off) and the criteria without a prefix are kept, with
their ids. `key` is the row of the
AC table: the issue as `api#N` / `ui#N` (`owner/name#N` for any other repo) and the id, so the
criteria of two issues never share one.

`stop` (exit 1): no `Refs` line names an issue; a ref that is a pull request is skipped with a
warning. Warnings: the pull request is merged or closed; an issue has no acceptance criteria;
the pull request names no part of an issue whose criteria carry parts (all are kept); it names
a part that no criterion carries.

Exit 0 go; 1 stop; 2 usage or bad REF; 3 gh / API / fixture error. On 2 and 3 stdout is empty
and stderr holds one line `{"error": "..."}`.

Offline seam
------------
With PR_REVIEW_FIXTURE_DIR set, each call reads `<dir>/<name>.json` instead of running gh:
`pr.json` for the pull request, `issue-<owner>_<name>-<number>.json` for an issue (the repo
lowercased); each holds what the `gh api … --jq` call prints. Set but empty is an error
(exit 3), never a live call.

Shared helpers
--------------
The REF grammar, the `Refs` lines and the parts a pull request names come from
`implement-issue/scripts/preflight.py`; the criteria checkboxes from
`accept-issue/scripts/issue_facts.py`. Both are loaded from the sibling skill directories, so
the three skills read a pull request and an issue the same way.
"""
# SYNC: mirrored in dmc-268-{ui,api}-t6/.agents/skills/pr-review/scripts/pr_context.py

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any, NoReturn

SCHEMA = 1
FIXTURE_ENV = "PR_REVIEW_FIXTURE_DIR"
GH_TIMEOUT = 120  # seconds per gh call
SKILLS = Path(__file__).resolve().parents[2]

PR_FIELDS = (
    "{number, title, body, state, draft, merged, html_url, user: .user.login,"
    " base: {ref: .base.ref, sha: .base.sha}, head: {ref: .head.ref, sha: .head.sha}}"
)
ISSUE_FIELDS = "{number, title, body, state, html_url, pull_request: (.pull_request != null)}"

MIN_FENCE = 6
TILDES = re.compile(r"~+")

MERGED = "the pull request is merged: the review reads its final head"
CLOSED = "the pull request is closed without a merge"


def fail(message: str, code: int) -> NoReturn:
    sys.stderr.write(json.dumps({"error": message}, ensure_ascii=False) + "\n")
    sys.exit(code)


def load(relative: str, name: str) -> ModuleType:
    """A sibling skill's script as a module (registered first, as dataclasses need)."""
    path = SKILLS / relative
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        fail(f"cannot load {path}", 3)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except OSError as exc:
        fail(f"cannot load {path}: {exc.strerror or exc}", 3)
    return module


preflight = load("implement-issue/scripts/preflight.py", "pr_review_preflight")
issue_facts = load("accept-issue/scripts/issue_facts.py", "pr_review_issue_facts")
SHORT = {repo: alias for alias, repo in preflight.ALIASES.items()}


# ─── transport ──────────────────────────────────────────────────────────────
def fetch(name: str, path: str, fields: str) -> dict[str, Any]:
    """One REST read: `gh api PATH --jq FIELDS`, or the fixture `<name>.json`."""
    seam = os.environ.get(FIXTURE_ENV)
    if seam == "":
        fail(f"{FIXTURE_ENV} is set but empty", 3)
    if seam is not None:
        file = Path(seam) / f"{name}.json"
        try:
            raw = file.read_text(encoding="utf-8")
        except OSError as exc:
            fail(f"cannot read fixture {file}: {exc.strerror or exc}", 3)
        except ValueError as exc:
            fail(f"cannot read fixture {file}: {exc}", 3)
    else:
        argv = ["gh", "api", path, "--jq", fields]
        try:
            proc = subprocess.run(argv, capture_output=True, encoding="utf-8", timeout=GH_TIMEOUT)
        except subprocess.TimeoutExpired:
            fail(f"gh api {path} timed out after {GH_TIMEOUT} s", 3)
        except OSError as exc:
            fail(f"cannot run gh: {exc}", 3)
        except ValueError as exc:  # gh output that is not UTF-8
            fail(f"gh api {path}: cannot read the output: {exc}", 3)
        if proc.returncode != 0:
            fail(f"gh api {path} exited {proc.returncode}: {proc.stderr.strip()}", 3)
        raw = proc.stdout
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        fail(f"{path}: response is not JSON: {exc}", 3)
    if not isinstance(value, dict):
        fail(f"{path}: response is not a JSON object", 3)
    return value


# ─── shape helpers ──────────────────────────────────────────────────────────
def field(node: dict[str, Any], key: str, kind: type, where: str) -> Any:
    value = node.get(key)
    if not isinstance(value, kind):
        fail(f"{where}: `{key}` is not a {kind.__name__}", 3)
    return value


def body_of(node: dict[str, Any], where: str) -> str:
    """A body GitHub returns as null when it is empty."""
    value = node.get("body")
    if value is None:
        return ""
    if not isinstance(value, str):
        fail(f"{where}: `body` is not a str", 3)
    return value


def pr_facts(raw: dict[str, Any], repo: str, number: int) -> dict[str, Any]:
    where = f"{repo}#{number}"
    base = field(raw, "base", dict, where)
    head = field(raw, "head", dict, where)
    merged = field(raw, "merged", bool, where)
    return {
        "author": field(raw, "user", str, where),
        "base_ref": field(base, "ref", str, f"{where} base"),
        "body": body_of(raw, where),
        "base_sha": field(base, "sha", str, f"{where} base"),
        "draft": field(raw, "draft", bool, where),
        "head_ref": field(head, "ref", str, f"{where} head"),
        "head_sha": field(head, "sha", str, f"{where} head"),
        "number": number,
        "repo": repo,
        "state": "merged" if merged else field(raw, "state", str, where),
        "title": field(raw, "title", str, where),
        "url": field(raw, "html_url", str, where),
    }


# ─── criteria ───────────────────────────────────────────────────────────────
def criteria_of(body: str) -> list[dict[str, Any]]:
    """The `## Acceptance Criteria` checkboxes, AC1.., each with the part its prefix names."""
    items: list[dict[str, Any]] = []
    for _, section, _, text in issue_facts.scan_checkboxes(body):
        if section != "ac":
            continue
        m = preflight.PART_CRITERION.fullmatch(text)
        part = preflight.to_int(m.group(1)) if m else None
        items.append(
            {
                "id": f"AC{len(items) + 1}",
                "part": part,
                "text": m.group(2).strip() if m and part is not None else text,
            }
        )
    return items


def select(
    criteria: list[dict[str, Any]], parts: list[int], ref: str, warnings: list[str]
) -> list[dict[str, Any]]:
    """The criteria a pull request that names `parts` is checked against."""
    carried = {c["part"] for c in criteria if c["part"] is not None}
    if not criteria:
        warnings.append(f"{ref} has no acceptance criteria: the AC axis has nothing to check")
        return criteria
    if not parts:
        if carried:
            warnings.append(
                f"the pull request names no part of {ref}: every acceptance criterion is listed"
            )
        return criteria
    for part in parts:
        if part not in carried:
            warnings.append(
                f"the pull request names Part {part} of {ref}, but no acceptance criterion"
                " carries that part"
            )
    return [c for c in criteria if c["part"] is None or c["part"] in parts]


def fence_for(texts: list[str]) -> str:
    """A `~` fence no line of these texts can close: longer than any `~` run in them."""
    longest = max((len(m.group()) for text in texts for m in TILDES.finditer(text)), default=0)
    return "~" * max(MIN_FENCE, longest + 1)


# ─── the run ────────────────────────────────────────────────────────────────
def collect(repo: str, number: int) -> dict[str, Any]:
    raw = fetch("pr", f"repos/{repo}/pulls/{number}", PR_FIELDS)
    pr = pr_facts(raw, repo, number)
    title = pr["title"]
    body = pr["body"]
    warnings: list[str] = []
    if pr["state"] == "merged":
        warnings.append(MERGED)
    elif pr["state"] == "closed":
        warnings.append(CLOSED)

    issues: list[dict[str, Any]] = []
    for ref_repo, ref_number in sorted(preflight.refs_of(title, body, repo)):
        ref = f"{ref_repo}#{ref_number}"
        name = f"issue-{ref_repo.replace('/', '_')}-{ref_number}"
        issue = fetch(name, f"repos/{ref_repo}/issues/{ref_number}", ISSUE_FIELDS)
        if field(issue, "pull_request", bool, ref):
            warnings.append(f"`Refs` names {ref}, a pull request, not an issue: skipped")
            continue
        named, _ = preflight.parts_named(title, body, repo, (ref_repo, ref_number))
        parts = sorted(set(named))
        issue_body = body_of(issue, ref)
        criteria = select(criteria_of(issue_body), parts, ref, warnings)
        label = f"{SHORT.get(ref_repo, ref_repo)}#{ref_number}"
        for criterion in criteria:
            criterion["key"] = f"{label} {criterion['id']}"
        issues.append(
            {
                "acceptance_criteria": criteria,
                "body": issue_body,
                "parts": parts,
                "ref": ref,
                "state": field(issue, "state", str, ref),
                "title": field(issue, "title", str, ref),
                "url": field(issue, "html_url", str, ref),
            }
        )

    reasons: list[str] = []
    if not issues:
        reasons.append(
            f"{repo}#{number} has no `Refs` line naming an issue: the Spec and AC axes have"
            " nothing to check against. Add `Refs #N` (`Refs owner/repo#N` for an issue in the"
            " other repo) to the pull request body, or review the diff with `code-review` and"
            " name the spec"
        )
    return {
        "fence": fence_for([title, body, *(i[k] for i in issues for k in ("title", "body"))]),
        "issues": issues,
        "pr": pr,
        "reasons": reasons,
        "schema": SCHEMA,
        "verdict": "stop" if reasons else "go",
        "warnings": warnings,
    }


class Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        fail(message, 2)


def main(argv: list[str] | None = None) -> int:
    parser = Parser(
        prog="pr_context.py",
        description="The pull request, its Refs issues and their acceptance criteria.",
    )
    parser.add_argument("ref", metavar="REF", help="api#N | ui#N | OWNER/NAME#N | N")
    args = parser.parse_args(argv)
    repo, number = preflight.parse_ref(args.ref)
    doc = collect(repo, number)
    sys.stdout.write(json.dumps(doc, sort_keys=True, indent=2, ensure_ascii=False) + "\n")
    return 0 if doc["verdict"] == "go" else 1


if __name__ == "__main__":
    sys.exit(main())
