#!/usr/bin/env python3
"""Check a recorded issue on board 12 and ensure its requested Status.

Exit 0 with {"board": "added"} only after Status reads back; exit 6 if the
item is absent or Status cannot be confirmed. ISSUE_FIXTURE_DIR replaces each
GitHub call with a JSON file of the same name for offline tests.
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


def edit(url: str, status: str) -> None:
    call(
        "project_edit",
        [
            "project",
            "item-edit",
            BOARD,
            "--owner",
            OWNER,
            "--url",
            url,
            "--field",
            "Status",
            "--value",
            status,
        ],
    )


def main(argv: list[str]) -> int:
    if len(argv) != 3 or not argv[1].startswith("https://github.com/") or not argv[2]:
        print('{"error": "usage: issue-board.py ISSUE_URL STATUS"}', file=sys.stderr)
        return 2
    url, want = argv[1:]
    try:
        row = item(url)
        if row is None:
            print('{"board": "failed"}')
            return 6
        if row.get("status") != want:
            edit(url, want)
            row = item(url, ".confirm")
        if row is not None and row.get("status") == want:
            if want != "Todo":
                time.sleep(float(os.environ.get("ISSUE_STATUS_RETRY_DELAY", "3")))
                row = item(url, ".retry")
        else:
            time.sleep(float(os.environ.get("ISSUE_STATUS_RETRY_DELAY", "3")))
        if row is None or row.get("status") != want:
            edit(url, want)
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
