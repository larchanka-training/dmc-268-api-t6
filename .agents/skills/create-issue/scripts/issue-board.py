#!/usr/bin/env python3
"""Check a recorded issue on board 12; with STATUS, ensure its Status.

Usage: issue-board.py ISSUE_URL [STATUS]. Without STATUS, only check presence and
leave a manually chosen Status alone. Exit 0 with {"board": "added"} after the
check, 2 for usage, 5 for a GitHub/read error, or 6 if absent or unconfirmed.
ISSUE_FIXTURE_DIR replaces GitHub calls with project_items[.confirm|.retry|.final],
project_view, and project_fields JSON files; project_edit is logged but not run.
ISSUE_STATUS_RETRY_DELAY sets the read-back pause in seconds (default 3).
"""
# SYNC: mirrored in dmc-268-{ui,api}-t6/.agents/skills/create-issue/scripts/issue-board.py

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

BOARD = "12"
OWNER = "larchanka-training"


def call(name: str, args: list[str]) -> Any:
    fixture = os.environ.get("ISSUE_FIXTURE_DIR")
    if fixture:
        path = Path(fixture)
        with (path / "calls.log").open("a", encoding="utf-8") as log:
            log.write(name + "\n")
        if name == "project_edit":
            return None
        return json.loads((path / f"{name}.json").read_text(encoding="utf-8"))
    result = subprocess.run(["gh", *args], capture_output=True, text=True, check=False)
    if result.returncode:
        raise OSError(result.stderr.strip() or f"gh exited {result.returncode}")
    if name == "project_edit":
        return None
    return json.loads(result.stdout) if result.stdout.strip() else None


def item(url: str, suffix: str = "") -> dict[str, Any] | None:
    data = call(
        "project_items" + suffix,
        ["project", "item-list", BOARD, "--owner", OWNER, "--format", "json", "--limit", "10000"],
    )
    rows = data["items"]
    if data.get("totalCount", len(rows)) > len(rows):
        raise ValueError("board 12 item list is incomplete")
    return next((row for row in rows if (row.get("content") or {}).get("url") == url), None)


def status_ids(status: str) -> tuple[str, str, str]:
    project = call("project_view", ["project", "view", BOARD, "--owner", OWNER, "--format", "json"])
    fields = call(
        "project_fields",
        ["project", "field-list", BOARD, "--owner", OWNER, "--format", "json", "--limit", "100"],
    )
    field = next((f for f in fields["fields"] if f.get("name") == "Status"), None)
    if field is None:
        raise ValueError("board 12 has no Status field")
    options = field.get("options", [])
    option = next((o for o in options if o.get("name") == status), None)
    if option is None:
        raise ValueError(f'status "{status}" is not an option of Status on board 12')
    return project["id"], field["id"], option["id"]


def edit(item_id: str, ids: tuple[str, str, str]) -> None:
    project_id, field_id, option_id = ids
    call(
        "project_edit",
        [
            "project",
            "item-edit",
            "--id",
            item_id,
            "--project-id",
            project_id,
            "--field-id",
            field_id,
            "--single-select-option-id",
            option_id,
        ],
    )


def main(argv: list[str]) -> int:
    if (
        len(argv) not in (2, 3)
        or not argv[1].startswith("https://github.com/")
        or (len(argv) == 3 and not argv[2])
    ):
        print('{"error": "usage: issue-board.py ISSUE_URL [STATUS]"}', file=sys.stderr)
        return 2
    url = argv[1]
    want = argv[2] if len(argv) == 3 else None
    try:
        row = item(url)
        if row is None:
            print('{"board": "failed"}')
            return 6
        if want is None:
            print('{"board": "added"}')
            return 0
        item_id = row["id"]
        ids = None
        if row.get("status") != want:
            ids = status_ids(want)
            edit(item_id, ids)
            row = item(url, ".confirm")
        if row is not None and row.get("status") == want:
            if want != "Todo":
                time.sleep(float(os.environ.get("ISSUE_STATUS_RETRY_DELAY", "3")))
                row = item(url, ".retry")
        else:
            time.sleep(float(os.environ.get("ISSUE_STATUS_RETRY_DELAY", "3")))
        if row is None or row.get("status") != want:
            if ids is None:
                ids = status_ids(want)
            edit(item_id, ids)
            row = item(url, ".final")
        if row is not None and row.get("status") == want:
            print('{"board": "added"}')
            return 0
        print('{"board": "failed"}')
        return 6
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        print(json.dumps({"error": f"cannot check board 12: {exc}"}), file=sys.stderr)
        return 5


if __name__ == "__main__":
    sys.exit(main(sys.argv))
