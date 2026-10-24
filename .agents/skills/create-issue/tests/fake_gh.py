#!/usr/bin/env python3
"""A stand-in `gh` / `curl` for the "real command line" tests.

Copied first on PATH under both names: it appends its argv to $STUB_LOG (one JSON
array per line, the program name first) and answers from the JSON config in
$STUB_CFG. A call it does not know exits 99, so an untested call fails the test
instead of passing silently.

The native-link endpoints keep state the way GitHub does, derived from the log of
this run: a landed POST to `.../sub_issues` sets the child's `parent_issue_url`, a
landed POST to `.../dependencies/blocked_by` adds the blocker to that list. Config:
`issues` ("owner/repo#N" → the REST issue object, or {"_code", "_err"} for a failed
call), `blocked_by` ("owner/repo#N" → the initial list) and `sub_issue_post` /
`blocked_by_post` ({"code", "err", "lands"}).
"""
# SYNC: mirrored in dmc-268-{ui,api}-t6/.agents/skills/create-issue/tests/fake_gh.py

from __future__ import annotations

import json
import os
import re
import sys
from typing import Any, NoReturn

ISSUE = re.compile(r"repos/([^/]+/[^/]+)/issues/([0-9]+)")
SUB_ISSUES = re.compile(r"repos/([^/]+/[^/]+)/issues/([0-9]+)/sub_issues")
BLOCKED_BY = re.compile(r"repos/([^/]+/[^/]+)/issues/([0-9]+)/dependencies/blocked_by")


def answer(text: str = "", code: int = 0, err: str = "") -> NoReturn:
    sys.stdout.write(text)
    sys.stderr.write(err)
    sys.exit(code)


def landed_posts(cfg: dict[str, Any], kind: str, pattern: re.Pattern[str]) -> list[list[str]]:
    """`gh api -X POST <path> -F <field>` calls of this run that the config lets land."""
    post = cfg.get(kind, {})
    if not post.get("lands", True):
        return []
    with open(os.environ["STUB_LOG"], encoding="utf-8") as fh:
        calls = [json.loads(line)[1:] for line in fh]
    return [
        c for c in calls if c[:3] == ["api", "-X", "POST"] and pattern.fullmatch(c[3]) is not None
    ]


def get_issue(cfg: dict[str, Any], key: str, fallback: str | None) -> NoReturn:
    issues = cfg.get("issues", {})
    if key in issues:
        obj = dict(issues[key])
    elif fallback is not None:
        answer(fallback)
    else:
        answer("", 99, f"unexpected issue lookup: {key}\n")
    if "_err" in obj:
        answer("", obj.get("_code", 1), obj["_err"])
    for post in landed_posts(cfg, "sub_issue_post", SUB_ISSUES):
        if post[5] == f"sub_issue_id={obj.get('id')}":
            parent = SUB_ISSUES.fullmatch(post[3])
            assert parent is not None
            obj["parent_issue_url"] = f"https://api.github.com/repos/{parent[1]}/issues/{parent[2]}"
    answer(json.dumps(obj))


def get_blocked_by(cfg: dict[str, Any], path: str, key: str) -> NoReturn:
    rows = list(cfg.get("blocked_by", {}).get(key, []))
    by_id = {obj.get("id"): obj for obj in cfg.get("issues", {}).values()}
    for post in landed_posts(cfg, "blocked_by_post", BLOCKED_BY):
        if post[3] == path:
            rows.append(by_id[int(post[5].split("=", 1)[1])])
    answer(json.dumps(rows))


def main() -> None:
    name = os.path.basename(sys.argv[0])
    argv = sys.argv[1:]
    with open(os.environ["STUB_LOG"], "a", encoding="utf-8") as fh:
        fh.write(json.dumps([name, *argv]) + "\n")
    with open(os.environ["STUB_CFG"], encoding="utf-8") as fh:
        cfg: dict[str, Any] = json.load(fh)
    repo = cfg["repo"]

    if name == "curl":
        answer("200")
    if argv[:2] == ["api", "user"]:
        answer("octocat\n")
    if argv[:2] == ["api", f"repos/{repo}"]:
        answer("true\n")
    if argv[:2] == ["issue", "list"]:
        answer("[]")
    if argv[:2] == ["issue", "create"]:
        body = argv[argv.index("--body-file") + 1]
        with (
            open(body, encoding="utf-8") as src,
            open(os.environ["STUB_BODY"], "w", encoding="utf-8") as dst,
        ):
            dst.write(src.read())
        answer(cfg["create_out"], cfg["create_rc"], cfg["create_err"])
    if argv[0] == "api" and "per_page=30" in argv[1]:
        with open(os.environ["STUB_LOG"], encoding="utf-8") as fh:
            n = sum(1 for line in fh if "per_page=30" in line)
        answer(cfg["recent"][n - 1])
    if argv == ["api", f"repos/{repo}/milestones?state=open&per_page=100"]:
        answer(cfg["milestones"])
    if argv[:2] == ["project", "view"]:
        answer(cfg["project_view"])
    if argv[:2] == ["project", "field-list"]:
        answer(cfg["fields"])
    if argv[:3] == ["api", "-X", "POST"] and len(argv) == 6 and argv[4] == "-F":
        kind = (
            "sub_issue_post"
            if SUB_ISSUES.fullmatch(argv[3])
            else "blocked_by_post"
            if BLOCKED_BY.fullmatch(argv[3])
            else None
        )
        if kind is not None:
            post = cfg.get(kind, {})
            # A sub-issue POST answers with the parent, never the child.
            answer('{"number": 0}', post.get("code", 0), post.get("err", ""))
    if len(argv) == 2 and argv[0] == "api":
        path, _, query = argv[1].partition("?")
        hit = ISSUE.fullmatch(path)
        if hit:
            fallback = cfg.get("view") if path == f"repos/{repo}/issues/13" else None
            get_issue(cfg, f"{hit[1]}#{hit[2]}", fallback)
        hit = BLOCKED_BY.fullmatch(path)
        if hit and query == "per_page=100":
            get_blocked_by(cfg, path, f"{hit[1]}#{hit[2]}")
    if argv[:2] == ["project", "item-add"]:
        answer(cfg.get("item_add_out", ""), cfg["board_rc"], cfg["board_err"])
    if argv[:2] == ["project", "item-edit"]:
        answer("", cfg.get("edit_rc", 0), cfg.get("edit_err", ""))
    if argv[:2] == ["api", "graphql"]:
        # the Status read-backs, in order: after the first edit, after the retry
        with open(os.environ["STUB_LOG"], encoding="utf-8") as fh:
            n = sum(1 for line in fh if json.loads(line)[1:3] == ["api", "graphql"])
        answer(cfg["status_reads"][n - 1])
    sys.stderr.write("unexpected call: " + json.dumps(argv) + "\n")
    sys.exit(99)


if __name__ == "__main__":
    main()
