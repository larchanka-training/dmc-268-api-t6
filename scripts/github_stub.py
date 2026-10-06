#!/usr/bin/env python3
"""GitHub API stub for the local webhook recipe in docs/WEBHOOK_WORKER.md (#70).

Answers just enough for `scripts/webhook_smoke.py`'s `pull_request` / `labeled` delivery
(installation 17, repository 101 `smoke/webhook-smoke`, PR 7) to create a Run: an installation
token and the current PR with the `ai-review` label. The recipe seeds `wait_for_ci = never`, so
no CI is read. Every other request gets 404; tests/test_github_stub_script.py keeps the PR in
step with the smoke delivery.

Usage (point the webhook worker at it with GITHUB_API_URL=http://127.0.0.1:9999):
    uv run python scripts/github_stub.py [--port 9999]

Stdlib only; local use, never a deployed environment.
"""

from __future__ import annotations

import argparse
import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

REPOSITORY = "smoke/webhook-smoke"
PULL_REQUEST = {
    "id": 901,
    "number": 7,
    "title": "Webhook smoke",
    "body": None,
    "html_url": f"https://github.com/{REPOSITORY}/pull/7",
    "user": {"login": "webhook-smoke"},
    "head": {"ref": "feature", "sha": "a" * 40},
    "base": {"ref": "main", "sha": "b" * 40},
    "state": "open",
    "merged": False,
    "updated_at": "2026-10-04T00:00:00Z",
    "labels": [{"name": "ai-review"}],
}


class Handler(BaseHTTPRequestHandler):
    def _send(self, status: int, payload: object) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        if self.path.endswith("/access_tokens"):
            self._send(201, {"token": "stub", "expires_at": "2099-01-01T00:00:00Z"})
            return
        self._send(404, {"message": "Not Found"})

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path == f"/repos/{REPOSITORY}/pulls/{PULL_REQUEST['number']}":
            self._send(200, PULL_REQUEST)
        else:
            self._send(404, {"message": "Not Found"})

    def log_message(self, format: str, *args: object) -> None:
        sys.stderr.write(f"github-stub: {format % args}\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=9999)
    port = parser.parse_args().port
    print(f"GitHub stub on http://127.0.0.1:{port}", file=sys.stderr)
    HTTPServer(("127.0.0.1", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
