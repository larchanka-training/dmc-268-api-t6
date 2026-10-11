"""A worker that reports three CPU seconds for every request."""

from __future__ import annotations

import json
import sys

for line in sys.stdin:
    key = json.loads(line)["key"]
    answer = {"key": key, "error": "ast_failed", "cpu": 3.0}
    sys.stdout.write(json.dumps(answer) + "\n")
    sys.stdout.flush()
