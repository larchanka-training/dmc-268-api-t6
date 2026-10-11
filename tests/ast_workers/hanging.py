"""A worker that answers the first request and then never answers again."""

from __future__ import annotations

import json
import sys
import time

for number, line in enumerate(sys.stdin):
    if number:
        time.sleep(600)
    key = json.loads(line)["key"]
    facts: dict[str, object] = {
        "language": "python",
        "profile_version": "test",
        "symbols": [],
        "imports": [],
        "exports": [],
        "package_name": None,
        "build_constrained": False,
        "has_errors": False,
    }
    sys.stdout.write(json.dumps({"key": key, "facts": facts, "cpu": 0.1}) + "\n")
    sys.stdout.flush()
