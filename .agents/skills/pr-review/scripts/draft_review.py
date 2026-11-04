#!/usr/bin/env python3
"""draft_review.py — turn a pull request review into the reviewer's draft on GitHub.

Usage
-----
    draft_review.py REF --findings FILE --diff FILE [--apply]
    REF := api#N | ui#N | OWNER/NAME#N | N        (a pull request; a bare N is api#N)

Without `--apply` nothing is written: the output is the preview of what `--apply` would do. With
it the script checks the same things again, then creates the viewer's pending review and adds the
threads. A pending review is visible to the viewer alone. The script has no way to submit it or to
set a verdict: that stays with the reviewer, on GitHub. It never edits or removes a comment of a
draft, and replaces a draft's text only when that is empty or exactly an earlier summary of this
script: what it writes ends in a hidden `<!-- pr-review <checksum> -->`, the first 16 hex digits of
the SHA-256 of the summary, which any edit breaks.

Input
-----
`--diff` is the output of the review's `git diff`: its added and context lines are the lines a
thread can sit on. `--findings` is one JSON object:

    {
      "schema": 1,
      "head_sha": "<the commit the review read>",
      "findings": [{"path": str, "line": int,
                    "severity": "critical" | "high" | "medium" | "low" | "info",
                    "category": str, "title": str, "body": str,
                    "scope": "in" | "out",
                    "decision": null                       # always null for scope "in"
                              | {"kind": "follow-up", "ref": "api#N"}
                              | {"kind": "accepted" | "rejected", "why": str}}],
      "ac": [{"key": str, "status": "covered" | "not covered" | "can't tell"}]
    }

Where a finding goes
--------------------
A follow-up: one line of the review body. Any other finding: a thread on the line when the line
is in the diff, a thread on the file when only the file is (a deleted, renamed or binary file has
no line to sit on), and when the file is not in the diff either, a thread on the first file of the
diff that names the place. A thread opens with `**P1 · high · correctness.** <title>` (P1 critical
and high, P2 medium, P3 low and info) and starts with `nit:` when it can stay unfixed: P3, and
anything out of scope.

The proposed verdict is text for the reviewer: Request changes for a critical, high or medium
finding in scope or a criterion `not covered`; else Comment when there is a thread or a comment in
the viewer's draft; else Approve.
The author of the pull request gets Comment: GitHub takes no other verdict from them.

Contract
--------
stdout carries one JSON document (`sort_keys`, indent 2, no ASCII escaping, one trailing newline):

    {
      "schema": 1,
      "verdict": "go" | "stop" | "partial",
      "reasons": [str],              # why it stopped, or what is left after a partial write
      "warnings": [str],
      "applied": bool,               # false: a preview or a stop, nothing was written
      "pr": {"repo", "number", "state", "head_sha", "author", "viewer"},
      "review": {"id", "url", "state", "existing": bool} | null,
      "proposed_verdict": "Request changes" | "Comment" | "Approve" | null,
      "summary": str,                # the review body without its checksum; empty on a stop.
                                     # GitHub's Submit window does not show the body: the
                                     # skill hands this over
      "threads": [{"path", "line": int | null, "kind": "line" | "file", "body",
                   "status": "new" | "exists" | "created" | "failed",
                   "error": str}],   # `error` on `failed` only
      "in_body": [{"path", "line", "title"}]
    }

`applied` and `review` report the read-back after the writes, never what was sent. `stop` (exit 1,
no write): the head moved past `head_sha`; the viewer's draft sits on another commit, has over 100
comments or has text of its own (anything but an earlier summary, unchanged; the reason quotes it,
as GitHub's pages do not show it); a finding out of scope has no decision. `partial` (exit 1): the
read-back found a thread or the draft missing, another text, or the head or the draft on another
commit than `head_sha`, or the draft could not be read back (its threads then stay `created`,
unverified); run again, the threads already there are skipped.

Exit 0 go; 1 stop or partial; 2 usage, bad REF, or a bad findings or diff file; 3 gh / API /
fixture error, or GitHub refused the draft itself. On 2 and 3 stdout is empty and stderr holds one
line `{"error": "..."}`.

Offline seam
------------
With PR_REVIEW_FIXTURE_DIR set, gh is never called and each request reads `<dir>/<name>.json`, the
full `{"data": ...}` answer: `state`, `create`, `update-body`, `thread-<k>` (the k-th thread sent),
`readback`. Set
but empty is an error. A write is first appended as a JSON line to $PR_REVIEW_WRITE_LOG, which
`--apply` then requires.
"""
# SYNC: mirrored in dmc-268-{ui,api}-t6/.agents/skills/pr-review/scripts/draft_review.py

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, NoReturn

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pr_context import FIXTURE_ENV, GH_TIMEOUT, Parser, fail, preflight  # noqa: E402

SCHEMA = 1
WRITE_LOG_ENV = "PR_REVIEW_WRITE_LOG"
PRIORITY = {"critical": "P1", "high": "P1", "medium": "P2", "low": "P3", "info": "P3"}
BLOCKING = {"critical", "high", "medium"}
STATUSES = ("covered", "not covered", "can't tell")
KINDS = ("follow-up", "accepted", "rejected")
RECORD = {"accepted": "принято", "rejected": "отклонено"}
FINDING_KEYS = ("body", "category", "decision", "line", "path", "scope", "severity", "title")
HUNK = re.compile(r"@@ -\d+(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
SAME_PATH = re.compile(r"diff --git a/(.+) b/\1")
CHECKSUM_START = "\n\n<!-- pr-review "
ISSUE_REF = re.compile(r"(?:api|ui|[\w.-]+/[\w.-]+)#\d+")

READ = """
query($owner: String!, $name: String!, $number: Int!) {
  viewer { login }
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      id
      state
      headRefOid
      author { login }
      reviews(states: PENDING, first: 1) {
        nodes {
          id
          state
          url
          body
          viewerDidAuthor
          commit { oid }
          comments(first: 100) {
            pageInfo { hasNextPage }
            nodes { path line subjectType body }
          }
        }
      }
    }
  }
}
"""
CREATE = """
mutation($pull: ID!, $commit: GitObjectID!, $body: String!) {
  addPullRequestReview(input: {pullRequestId: $pull, commitOID: $commit, body: $body}) {
    pullRequestReview { id }
  }
}
"""
UPDATE_BODY = """
mutation($review: ID!, $body: String!) {
  updatePullRequestReview(input: {pullRequestReviewId: $review, body: $body}) {
    pullRequestReview { id }
  }
}
"""
THREAD_ON_LINE = """
mutation($review: ID!, $path: String!, $line: Int!, $body: String!) {
  addPullRequestReviewThread(input: {
    pullRequestReviewId: $review, path: $path, line: $line, side: RIGHT, body: $body
  }) { thread { id } }
}
"""
THREAD_ON_FILE = """
mutation($review: ID!, $path: String!, $body: String!) {
  addPullRequestReviewThread(input: {
    pullRequestReviewId: $review, path: $path, subjectType: FILE, body: $body
  }) { thread { id } }
}
"""


class GhError(Exception):
    """A request that did not give an answer."""


# ─── transport ──────────────────────────────────────────────────────────────
def seam() -> str | None:
    value = os.environ.get(FIXTURE_ENV)
    if value == "":
        fail(f"{FIXTURE_ENV} is set but empty", 3)
    return value


def raw_answer(name: str, query: str, variables: dict[str, str | int]) -> str:
    folder = seam()
    if folder is not None:
        file = Path(folder) / f"{name}.json"
        try:
            return file.read_text(encoding="utf-8")
        except (OSError, ValueError) as exc:
            raise GhError(f"gh api graphql: cannot read fixture {file}: {exc}") from exc
    argv = ["gh", "api", "graphql", "-f", f"query={query}"]
    for key, value in variables.items():
        # `-f` for every text: `-F` would read a value starting with `@` as a file.
        argv += ["-F", f"{key}={value}"] if isinstance(value, int) else ["-f", f"{key}={value}"]
    try:
        proc = subprocess.run(argv, capture_output=True, encoding="utf-8", timeout=GH_TIMEOUT)
    except subprocess.TimeoutExpired as exc:
        raise GhError(f"gh api graphql timed out after {GH_TIMEOUT} s") from exc
    except OSError as exc:
        raise GhError(f"cannot run gh: {exc}") from exc
    except ValueError as exc:  # gh output that is not UTF-8
        raise GhError(f"gh api graphql: cannot read the output: {exc}") from exc
    if proc.returncode != 0 and '"errors"' not in proc.stdout:
        raise GhError(f"gh api graphql exited {proc.returncode}: {proc.stderr.strip()}")
    return proc.stdout


def graphql(name: str, query: str, variables: dict[str, str | int]) -> dict[str, Any]:
    """One request, its `data`. GitHub's own errors raise too: gh exits 1 with them on stdout."""
    raw = raw_answer(name, query, variables)
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise GhError(f"gh api graphql: response is not JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise GhError("gh api graphql: response is not a JSON object")
    errors = value.get("errors")
    if errors:
        text = "; ".join(str(e.get("message")) for e in errors if isinstance(e, dict))
        raise GhError(f"gh api graphql: {text}")
    data = value.get("data")
    if not isinstance(data, dict):
        raise GhError("gh api graphql: response has no data")
    return data


def write(name: str, query: str, op: str, variables: dict[str, str | int]) -> dict[str, Any]:
    """One mutation: logged first under the seam, then answered like any request."""
    if seam() is not None:
        log = os.environ[WRITE_LOG_ENV]
        with open(log, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(logged(op, variables), sort_keys=True, ensure_ascii=False))
            handle.write("\n")
    return graphql(name, query, variables)


def logged(op: str, variables: dict[str, str | int]) -> dict[str, str | int]:
    """A write as the input GitHub receives, by the names of its input fields."""
    if op == "updatePullRequestReview":
        return {"op": op, "pullRequestReviewId": variables["review"], "body": variables["body"]}
    if op == "addPullRequestReview":
        return {
            "op": op,
            "pullRequestId": variables["pull"],
            "commitOID": variables["commit"],
            "body": variables["body"],
        }
    target: dict[str, str | int] = (
        {"line": variables["line"], "side": "RIGHT"}
        if "line" in variables
        else {"subjectType": "FILE"}
    )
    return {
        "op": op,
        "pullRequestReviewId": variables["review"],
        "path": variables["path"],
        "body": variables["body"],
        **target,
    }


# ─── pure functions ─────────────────────────────────────────────────────────
def commentable_lines(diff: str) -> dict[str, set[int]]:
    """The lines a thread can sit on, per file: the new side of each hunk. A file with no hunk
    there (deleted, renamed, binary, only its mode) is in the diff with no line."""
    lines: dict[str, set[int]] = {}
    path: str | None = None
    old_left = new_left = number = 0
    for line in diff.split("\n"):
        if old_left > 0 or new_left > 0:
            mark = line[:1]
            if mark == "-":
                old_left -= 1
            elif mark in {"+", " "}:
                if path is not None:
                    lines[path].add(number)
                number += 1
                new_left -= 1
                old_left -= 1 if mark == " " else 0
            continue
        if line.startswith("diff --git "):
            path = None
            if same := SAME_PATH.fullmatch(line):
                lines.setdefault(same.group(1), set())
        elif line.startswith(("rename to ", "copy to ")):
            lines.setdefault(line.split(" to ", 1)[1], set())
        elif line.startswith("+++ "):
            name = line[4:].rstrip("\t")
            path = name[2:] if name.startswith("b/") else None
            if path is not None:
                lines.setdefault(path, set())
        elif hunk := HUNK.match(line):
            old_left = int(hunk.group(1) or 1)
            number = int(hunk.group(2))
            new_left = int(hunk.group(3) or 1)
    return lines


def label(finding: dict[str, Any]) -> str:
    severity = finding["severity"]
    head = f"**{PRIORITY[severity]} · {severity} · {finding['category']}.** {finding['title']}"
    return f"nit: {head}" if PRIORITY[severity] == "P3" or finding["scope"] == "out" else head


def decided(finding: dict[str, Any]) -> str:
    """The closing sentence of a finding out of scope; empty for one in scope."""
    decision = finding["decision"]
    if decision is None:
        return ""
    if decision["kind"] == "follow-up":
        return f"Вне скоупа — follow-up {decision['ref']}."
    return f"Вне скоупа — {RECORD[decision['kind']]}: {decision['why']}"


def thread_body(finding: dict[str, Any], kind: str) -> str:
    parts = [label(finding)]
    if kind == "file":
        parts.append(f"Строка {finding['line']} не входит в дифф.")
    elif kind == "other":
        parts.append(
            f"Замечание к `{finding['path']}:{finding['line']}`: "
            "этого файла нет в диффе, тред на первом файле PR."
        )
    parts.append(finding["body"])
    if tail := decided(finding):
        parts.append(tail)
    return "\n\n".join(parts)


def body_line(finding: dict[str, Any]) -> str:
    """A finding that has no thread, as one list item of the review body."""
    head = label(finding).removeprefix("nit: ")
    text = " ".join(finding["body"].split())
    line = f"- `{finding['path']}:{finding['line']}` {head} — {text}"
    return f"{line} {tail}" if (tail := decided(finding)) else line


def proposed_verdict(
    findings: list[dict[str, Any]],
    ac: list[dict[str, str]],
    has_remarks: bool,
    is_author: bool,
) -> str:
    if is_author:
        return "Comment"
    if any(f["scope"] == "in" and f["severity"] in BLOCKING for f in findings):
        return "Request changes"
    if any(row["status"] == "not covered" for row in ac):
        return "Request changes"
    return "Comment" if has_remarks else "Approve"


def summary_of(
    head_sha: str,
    findings: list[dict[str, Any]],
    ac: list[dict[str, str]],
    verdict: str,
    without_thread: list[str],
) -> str:
    severities = ", ".join(
        f"{name} {sum(f['severity'] == name for f in findings)}" for name in PRIORITY
    )
    statuses = ", ".join(f"{name} {sum(r['status'] == name for r in ac)}" for name in STATUSES)
    parts = [
        f"Итог на `{head_sha[:8]}`: {severities}. AC: {statuses}.",
        f"Предложенный вердикт: {verdict}. Вердикт и Submit — решение ревьюера.",
    ]
    not_covered = [row["key"] for row in ac if row["status"] == "not covered"]
    if not_covered:
        parts.append(f"Не покрыто (not covered): {', '.join(not_covered)}")
    by_hand = [row["key"] for row in ac if row["status"] == "can't tell"]
    if by_hand:
        parts.append(f"Проверить руками (can't tell): {', '.join(by_hand)}")
    if without_thread:
        parts.append("Без треда:\n" + "\n".join(without_thread))
    return "\n\n".join(parts)


def draft_text(summary: str) -> str:
    """What a draft holds: the summary and, hidden when rendered, a checksum of it."""
    checksum = hashlib.sha256(summary.encode("utf-8")).hexdigest()[:16]
    return f"{summary}{CHECKSUM_START}{checksum} -->"


def is_earlier_summary(body: str) -> bool:
    """True for a text this script wrote and nobody changed since: only that may be replaced."""
    return body == draft_text(body.rpartition(CHECKSUM_START)[0])


# ─── the findings file ──────────────────────────────────────────────────────
Complain = Callable[[str], NoReturn]


def check_text(node: dict[str, Any], key: str, where: str, complain: Complain) -> None:
    if not isinstance(node.get(key), str) or not node[key]:
        complain(f"{where}{key} must be a text")


def check_finding(node: Any, where: str, complain: Complain) -> None:
    if not isinstance(node, dict) or sorted(node) != list(FINDING_KEYS):
        complain(f"{where} keys must be {', '.join(FINDING_KEYS)}")
    for key in ("category", "path", "title", "body"):
        check_text(node, key, f"{where}.", complain)
    if "\n" in node["title"]:
        complain(f"{where}.title must be one line")
    if node["severity"] not in tuple(PRIORITY):  # a tuple: a list here must not raise
        complain(f"{where}.severity must be one of {', '.join(PRIORITY)}")
    if node["scope"] not in ("in", "out"):
        complain(f"{where}.scope must be one of in, out")
    if type(node["line"]) is not int or node["line"] < 1:
        complain(f"{where}.line must be a positive integer")
    decision = node["decision"]
    if decision is None:
        return
    if node["scope"] == "in":
        complain(f"{where}.decision must be null for a finding in scope")
    if not isinstance(decision, dict) or decision.get("kind") not in KINDS:
        complain(f"{where}.decision.kind must be one of {', '.join(KINDS)}")
    if decision["kind"] != "follow-up":
        check_text(decision, "why", f"{where}.decision.", complain)
    elif not ISSUE_REF.fullmatch(str(decision.get("ref"))):
        complain(f"{where}.decision.ref must name an issue: api#N, ui#N or owner/name#N")


def load_findings(path: str) -> dict[str, Any]:
    def complain(message: str) -> NoReturn:
        fail(f"{path}: {message}", 2)

    try:
        raw = Path(path).read_text(encoding="utf-8")
    except (OSError, ValueError) as exc:
        fail(f"cannot read {path}: {exc}", 2)
    try:
        plan = json.loads(raw)
    except json.JSONDecodeError as exc:
        fail(f"{path}: not JSON: {exc}", 2)
    if not isinstance(plan, dict):
        complain("must be a JSON object")
    if sorted(plan) != ["ac", "findings", "head_sha", "schema"]:
        complain("keys must be ac, findings, head_sha, schema")
    if plan["schema"] != SCHEMA:
        complain(f"schema must be {SCHEMA}")
    check_text(plan, "head_sha", "", complain)
    for key in ("findings", "ac"):
        if not isinstance(plan[key], list):
            complain(f"{key} must be a list")
    for k, node in enumerate(plan["findings"]):
        check_finding(node, f"findings[{k}]", complain)
    for k, row in enumerate(plan["ac"]):
        if not isinstance(row, dict):
            complain(f"ac[{k}] must be an object")
        check_text(row, "key", f"ac[{k}].", complain)
        if row.get("status") not in STATUSES:
            complain(f"ac[{k}].status must be one of {', '.join(STATUSES)}")
    return plan


def load_diff(path: str) -> dict[str, set[int]]:
    try:
        diff = Path(path).read_text(encoding="utf-8")
    except (OSError, ValueError) as exc:
        fail(f"cannot read {path}: {exc}", 2)
    lines = commentable_lines(diff)
    if not lines:
        fail(f"{path}: the diff names no file", 2)
    return lines


# ─── GitHub's state ─────────────────────────────────────────────────────────
def read_state(name: str, repo: str, number: int) -> dict[str, Any]:
    owner, repo_name = repo.split("/", 1)
    data = graphql(name, READ, {"owner": owner, "name": repo_name, "number": number})
    viewer = data.get("viewer")
    repository = data.get("repository")
    pull = repository.get("pullRequest") if isinstance(repository, dict) else None
    if not isinstance(viewer, dict) or not isinstance(pull, dict):
        raise GhError("repository.pullRequest: expected an object")
    drafts = pull["reviews"]["nodes"]
    return {
        "pull_id": pull["id"],
        "head_sha": pull["headRefOid"],
        "state": str(pull["state"]).lower(),
        "author": (pull.get("author") or {}).get("login", ""),
        "viewer": viewer["login"],
        "draft": next((d for d in drafts if d.get("viewerDidAuthor") is True), None),
    }


def thread_key(path: str, line: int | None, body: str) -> tuple[str, int | None, str]:
    return path, line, body.strip()


def threads_in(draft: dict[str, Any] | None) -> set[tuple[str, int | None, str]]:
    if draft is None:
        return set()
    return {
        thread_key(c["path"], None if c.get("subjectType") == "FILE" else c["line"], c["body"])
        for c in draft["comments"]["nodes"]
    }


def state_warnings(state: dict[str, Any]) -> list[str]:
    warnings = []
    if state["state"] == "merged":
        warnings.append("the pull request is merged: the draft comments on its final head")
    elif state["state"] == "closed":
        warnings.append("the pull request is closed without a merge")
    if state["author"] == state["viewer"]:
        warnings.append(
            "you are the author of this pull request: GitHub accepts only a comment from you"
        )
    return warnings


def stop_reasons(state: dict[str, Any], plan: dict[str, Any]) -> list[str]:
    reasons = []
    head = state["head_sha"]
    if plan["head_sha"] != head:
        reasons.append(
            f"the pull request's head moved to {head}: the findings are about "
            f"{plan['head_sha']}; run the review again"
        )
    draft = state["draft"]
    if draft is not None:
        if draft["commit"]["oid"] != head:
            reasons.append(
                f"your draft review is on {draft['commit']['oid']}, not on the head {head}: "
                "submit or delete it on GitHub, then run again"
            )
        if draft["comments"]["pageInfo"]["hasNextPage"]:
            reasons.append(
                "your draft review has over 100 comments: finish it on GitHub, then run again"
            )
        if draft["body"] and not is_earlier_summary(draft["body"]):
            reasons.append(
                "your draft review has text of its own: keep what you need of it, then submit "
                f"or delete the draft on GitHub and run again. The text: {draft['body']}"
            )
    undecided = [
        f"{f['path']}:{f['line']}"
        for f in plan["findings"]
        if f["scope"] == "out" and f["decision"] is None
    ]
    if undecided:
        reasons.append(
            "findings out of scope need a decision (follow-up, accepted or rejected): "
            + ", ".join(undecided)
        )
    return reasons


# ─── the run ────────────────────────────────────────────────────────────────
def is_follow_up(finding: dict[str, Any]) -> bool:
    decision = finding["decision"]
    return decision is not None and decision["kind"] == "follow-up"


def place(finding: dict[str, Any], lines: dict[str, set[int]]) -> str:
    if is_follow_up(finding):
        return "body"
    in_file = lines.get(finding["path"])
    if in_file is None:
        return "other"
    return "line" if finding["line"] in in_file else "file"


def plan_threads(
    findings: list[dict[str, Any]],
    lines: dict[str, set[int]],
    draft: dict[str, Any] | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    present = threads_in(draft)
    first_file = next(iter(lines))  # in the diff's order; load_diff keeps `lines` from being empty
    threads = []
    without = []
    for finding in findings:
        kind = place(finding, lines)
        if kind == "body":
            without.append(finding)
            continue
        line = finding["line"] if kind == "line" else None
        path = first_file if kind == "other" else finding["path"]
        body = thread_body(finding, kind)
        status = "exists" if thread_key(path, line, body) in present else "new"
        threads.append(
            {
                "path": path,
                "line": line,
                "kind": "line" if kind == "line" else "file",
                "body": body,
                "status": status,
            }
        )
    return threads, without


def add_threads(threads: list[dict[str, Any]], review_id: str) -> None:
    """Send what is missing; one refused thread does not stop the others."""
    sent = 0
    for thread in threads:
        if thread["status"] != "new":
            continue
        sent += 1
        variables: dict[str, str | int] = {
            "review": review_id,
            "path": thread["path"],
            "body": thread["body"],
        }
        query = THREAD_ON_FILE
        if thread["kind"] == "line":
            variables["line"] = thread["line"]
            query = THREAD_ON_LINE
        try:
            data = write(f"thread-{sent}", query, "addPullRequestReviewThread", variables)
        except GhError as exc:
            thread.update(status="failed", error=str(exc))
            continue
        # A line outside the diff is no GraphQL error: the answer is a null thread.
        answer = data.get("addPullRequestReviewThread")
        if not isinstance(answer, dict) or answer.get("thread") is None:
            thread.update(status="failed", error="GitHub did not create the thread")
        else:
            thread["status"] = "created"


def new_document(repo: str, number: int, state: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "verdict": "go",
        "reasons": [],
        "warnings": state_warnings(state),
        "applied": False,
        "pr": {
            "repo": repo,
            "number": number,
            "state": state["state"],
            "head_sha": state["head_sha"],
            "author": state["author"],
            "viewer": state["viewer"],
        },
        "review": None,
        "proposed_verdict": None,
        "summary": "",
        "threads": [],
        "in_body": [],
    }


def create_draft(state: dict[str, Any], text: str) -> str:
    try:
        created = write(
            "create",
            CREATE,
            "addPullRequestReview",
            {"pull": state["pull_id"], "commit": state["head_sha"], "body": text},
        )
    except GhError as exc:
        fail(str(exc), 3)
    review_id: str = created["addPullRequestReview"]["pullRequestReview"]["id"]
    return review_id


def update_text(doc: dict[str, Any], review_id: str, text: str) -> None:
    variables: dict[str, str | int] = {"review": review_id, "body": text}
    try:
        write("update-body", UPDATE_BODY, "updatePullRequestReview", variables)
    except GhError as exc:
        doc["warnings"].append(str(exc))  # the read-back reports the text the draft holds


def collect(
    repo: str, number: int, plan: dict[str, Any], lines: dict[str, set[int]], apply: bool
) -> dict[str, Any]:
    if apply and seam() is not None and not os.environ.get(WRITE_LOG_ENV):
        fail(f"--apply against {FIXTURE_ENV} needs {WRITE_LOG_ENV}", 3)
    try:
        state = read_state("state", repo, number)
    except GhError as exc:
        fail(str(exc), 3)
    doc = new_document(repo, number, state)
    reasons = stop_reasons(state, plan)
    if reasons:
        doc.update(verdict="stop", reasons=reasons)
        return doc

    draft = state["draft"]
    findings, ac = plan["findings"], plan["ac"]
    threads, without = plan_threads(findings, lines, draft)
    remarks = threads or threads_in(draft)
    verdict = proposed_verdict(findings, ac, bool(remarks), state["author"] == state["viewer"])
    summary = summary_of(state["head_sha"], findings, ac, verdict, [body_line(f) for f in without])
    text = draft_text(summary)
    doc.update(
        proposed_verdict=verdict,
        summary=summary,
        threads=threads,
        in_body=[{"path": f["path"], "line": f["line"], "title": f["title"]} for f in without],
    )
    if draft is not None:
        doc["review"] = review_of(draft, True)
        if draft["body"] and draft["body"] != text:
            doc["warnings"].append("the summary replaces the one your draft holds")
    if not apply:
        return doc

    if draft is None:
        review_id = create_draft(state, text)
    else:
        review_id = draft["id"]
        if draft["body"] != text:
            update_text(doc, review_id, text)
    add_threads(threads, review_id)
    read_back(doc, repo, number, draft is not None, plan["head_sha"])
    return doc


def review_of(draft: dict[str, Any], existing: bool) -> dict[str, Any]:
    return {"id": draft["id"], "url": draft["url"], "state": draft["state"], "existing": existing}


def differences(state: dict[str, Any], head_sha: str, summary: str) -> list[str]:
    """What the read-back shows that is not the review the reviewer confirmed."""
    reasons = []
    if state["head_sha"] != head_sha:
        reasons.append(
            f"the pull request's head moved to {state['head_sha']} during the write: "
            f"the draft is about {head_sha}; run the review again"
        )
    draft = state["draft"]
    if draft["commit"]["oid"] != head_sha:
        reasons.append(
            f"the draft is on {draft['commit']['oid']}, not on the commit the review read, "
            f"{head_sha}; run the review again"
        )
    if draft["body"] != draft_text(summary):
        reasons.append("the draft does not hold the summary after the write")
    return reasons


def read_back(doc: dict[str, Any], repo: str, number: int, existing: bool, head_sha: str) -> None:
    """Report only what GitHub holds now."""
    doc["review"] = None
    reasons: list[str] = []
    present: set[tuple[str, int | None, str]] | None = None
    try:
        state = read_state("readback", repo, number)
        draft = state["draft"]
        present = threads_in(draft)
        if draft is None or draft["state"] != "PENDING":
            reasons.append("the draft is not on GitHub after the write")
        else:
            doc.update(applied=True, review=review_of(draft, existing))
            reasons += differences(state, head_sha, doc["summary"])
    except (GhError, KeyError, TypeError, AttributeError) as exc:
        present = None  # nothing is known: the threads stay `created`, unverified
        reasons.append(f"cannot read the draft back: {exc}")
    if present is not None:
        for thread in doc["threads"]:
            missing = thread_key(thread["path"], thread["line"], thread["body"]) not in present
            if thread["status"] == "created" and missing:
                thread.update(status="failed", error="not in the read-back")
    failed = sum(t["status"] == "failed" for t in doc["threads"])
    if failed:
        reasons.append(
            f"{failed} thread(s) not created: fix the cause and run again; "
            "those already there are skipped"
        )
    if reasons:
        doc.update(verdict="partial", reasons=reasons)


def main(argv: list[str] | None = None) -> int:
    parser = Parser(
        prog="draft_review.py",
        description="Preview or create the reviewer's draft review of a pull request.",
    )
    parser.add_argument("ref", metavar="REF", help="api#N | ui#N | OWNER/NAME#N | N")
    parser.add_argument("--findings", required=True, metavar="FILE")
    parser.add_argument("--diff", required=True, metavar="FILE")
    parser.add_argument("--apply", action="store_true", help="write the draft")
    args = parser.parse_args(argv)
    repo, number = preflight.parse_ref(args.ref)
    plan = load_findings(args.findings)
    lines = load_diff(args.diff)
    try:
        doc = collect(repo, number, plan, lines, args.apply)
    except (KeyError, TypeError, AttributeError) as exc:
        fail(f"unexpected answer from GitHub: {exc!r}", 3)
    sys.stdout.write(json.dumps(doc, sort_keys=True, indent=2, ensure_ascii=False) + "\n")
    return 0 if doc["verdict"] == "go" else 1


if __name__ == "__main__":
    sys.exit(main())
