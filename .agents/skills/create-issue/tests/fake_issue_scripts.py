#!/usr/bin/env python3
"""A stand-in for issue-create.sh and issue-link.sh in the issue-batch.py tests.

Copied into one directory under both names, which ISSUE_BATCH_SCRIPTS_DIR points the
driver at. Every call appends one JSON line to $STUB_LOG: {"script", "argv", "spec",
"state"} — the spec file the driver passed (create only) and the batch state file as
it was when the call started ($STUB_STATE), so a test sees what was recorded before
each item ran.

Answers come from the JSON config in $STUB_CFG, keyed by the item's summary (create)
or by the issue ref (link):

* `create`: {"<summary>": {"number", "code", "dry_code", "duplicate"}} — code 6 is a
  board failure and 7 a link failure (both still print the result, as the real script
  does); 4 with `duplicate` prints that issue as the blocked duplicate; any other
  non-zero code prints only an error. `number` defaults to 100 + the count of real
  creates so far.
* `link`: {"OWNER/REPO#N": {"code"}} — 7 marks every link failed; another non-zero
  code prints only an error.
"""
# SYNC: mirrored in dmc-268-{ui,api}-t6/.agents/skills/create-issue/tests/fake_issue_scripts.py

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, NoReturn


def fail(script: str, code: int, message: str = "") -> NoReturn:
    text = message or f"stub {script} failed with exit {code}"
    print(json.dumps({"error": text}), file=sys.stderr)
    sys.exit(code)


def links_row(ref: str, ok: bool, action: str) -> dict[str, Any]:
    return {"ref": ref, "ok": ok, "action": action if ok else None}


def log_call(script: str, argv: list[str], spec: Any) -> list[dict[str, Any]]:
    """Append this call; return the calls logged before it."""
    log = Path(os.environ["STUB_LOG"])
    before = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    state_path = Path(os.environ["STUB_STATE"])
    state = json.loads(state_path.read_text()) if state_path.exists() else None
    with log.open("a", encoding="utf-8") as fh:
        row = {"script": script, "argv": argv, "spec": spec, "state": state}
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    return before


def create(argv: list[str], cfg: dict[str, Any]) -> int:
    spec = json.loads(Path(argv[0]).read_text(encoding="utf-8"))
    before = log_call("create", argv, spec)
    item = cfg.get("create", {}).get(spec["summary"], {})
    repo, no_board = spec["repo"], "--no-board" in argv
    status = None if no_board else spec.get("status") or "Todo"
    if "--dry-run" in argv:
        code = item.get("dry_code", 0)
        if code:
            fail("issue-create.sh --dry-run", code)
        dry = {"dry_run": True, "repo": repo, "body": f"body of {spec['summary']}\n"}
        dry.update({"milestone": spec.get("milestone"), "status": status, "links": []})
        print(json.dumps(dry, ensure_ascii=False))
        return 0
    code = item.get("code", 0)
    if code == 4 and "duplicate" in item:
        num = item["duplicate"]
        url = f"https://github.com/{repo}/issues/{num}"
        print(json.dumps({"duplicate": True, "url": url, "number": num, "repo": repo}))
        fail(
            "issue-create.sh",
            4,
            f"an issue with exactly this title already exists in {repo}: {url}",
        )
    if code not in (0, 6, 7):
        fail("issue-create.sh", code)
    creates = [c for c in before if c["script"] == "create" and "--dry-run" not in c["argv"]]
    num = item.get("number", 101 + len(creates))
    out: dict[str, Any] = {
        "url": f"https://github.com/{repo}/issues/{num}",
        "number": num,
        "repo": repo,
        "verified": True,
        "board": "failed" if code == 6 else "skipped" if no_board else "added",
        "status": status,
    }
    if "parent" in spec or "blocked_by" in spec:
        parent = spec.get("parent")
        out["links"] = {
            "parent": links_row(parent, code != 7, "linked") if parent else None,
            "blocked_by": [links_row(r, code != 7, "linked") for r in spec.get("blocked_by", [])],
        }
    print(json.dumps(out, ensure_ascii=False))
    return int(code)


def link(argv: list[str], cfg: dict[str, Any]) -> int:
    log_call("link", argv, None)
    issue, rest = argv[0], argv[1:]
    code = cfg.get("link", {}).get(issue, {}).get("code", 0)
    if code not in (0, 7):
        fail("issue-link.sh", code)
    parent = rest[rest.index("--parent") + 1] if "--parent" in rest else None
    blocked = [rest[i + 1] for i, arg in enumerate(rest) if arg == "--blocked-by"]
    links = {
        "parent": links_row(parent, code == 0, "exists") if parent else None,
        "blocked_by": [links_row(ref, code == 0, "exists") for ref in blocked],
    }
    print(json.dumps({"issue": issue, "links": links}))
    return int(code)


def main() -> int:
    cfg = json.loads(Path(os.environ["STUB_CFG"]).read_text(encoding="utf-8"))
    name = Path(sys.argv[0]).name
    if name == "issue-create.sh":
        return create(sys.argv[1:], cfg)
    if name == "issue-link.sh":
        return link(sys.argv[1:], cfg)
    fail(name, 99)


if __name__ == "__main__":
    sys.exit(main())
