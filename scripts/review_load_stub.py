#!/usr/bin/env python3
"""Fake GitHub and fake LLM for the parallel-Runs load scenario (#163).

One stdlib HTTP server answers, for PRs 1..N of `load/parallel-runs` (installation 17,
repository 101):

- the GitHub REST calls of the review worker and the webhook worker's PR projection: the
  installation token, `pulls/{n}` with commits and files, blobs, compare, trees, check-runs
  (GET/POST/PATCH), reviews (GET/POST) and review comments. Every PR changes `app/example.py` on
  its own head (`head_sha(n)`) over one shared base. Posted reviews and check-runs are kept, so a
  retried publication finds them as GitHub would;
- `POST /v1/chat/completions` as an OpenAI-compatible model answering the `RepoConventionsDraft`
  and `ReviewOutput` schemas, after `--llm-delay-ms`;
- `GET /_stats`: review POSTs per PR, LLM calls per schema and every unhandled request.

Anything else gets 404 and is listed under `unhandled`. The stub checks no JWT or token.

Usage (deploy/load/compose.parallel-runs.yml runs it inside the load stack):
    python scripts/review_load_stub.py [--host 0.0.0.0] [--port 9999] [--prs 5]
                                       [--llm-delay-ms 3000]

Stdlib only; local load runs only, never a deployed environment.
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import sys
import threading
import time
from collections import Counter
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

INSTALLATION_ID = 17
REPOSITORY_ID = 101
REPOSITORY = "load/parallel-runs"
PR_ID_OFFSET = 9000
MAX_PRS = 20
BASE_SHA = "b" * 40
FILE_PATH = "app/example.py"
FILE_SHA = "f" * 40
FILE_TEXT = b"line10\nline11\nline12\n"
PATCH = "@@ -10,2 +10,3 @@\n line10\n+line11\n line12"
README_SHA = "c" * 40
_REPO = f"/repos/{REPOSITORY}"
_UNHANDLED_LIMIT = 100

type Reply = tuple[int, object]


def head_sha(number: int) -> str:
    """The head commit of PR ``number``: distinct per PR, never the base or a blob sha."""
    if not 1 <= number <= MAX_PRS:
        raise ValueError(f"PR number must be from 1 to {MAX_PRS}")
    return f"{number:02x}" * 20


def pull_request(number: int) -> dict[str, object]:
    """PR ``number`` as `GET /repos/{repo}/pulls/{n}` answers it."""
    return {
        "id": PR_ID_OFFSET + number,
        "number": number,
        "title": f"Load change {number}",
        "body": None,
        "html_url": f"https://github.com/{REPOSITORY}/pull/{number}",
        "user": {"login": "load-author"},
        "head": {
            "ref": f"load-{number}",
            "sha": head_sha(number),
            "repo": {"full_name": REPOSITORY},
        },
        "base": {"ref": "main", "sha": BASE_SHA, "repo": {"full_name": REPOSITORY}},
        "state": "open",
        "merged": False,
        "draft": False,
        "updated_at": "2026-10-23T00:00:00Z",
        "labels": [{"name": "ai-review"}],
        "changed_files": 1,
        "additions": 1,
        "deletions": 0,
    }


def conventions_answer() -> dict[str, object]:
    return {
        "files": [{"path": FILE_PATH, "relevance": "Changed module."}],
        "key_patterns": ["Pattern one.", "Pattern two.", "Pattern three."],
        "recommendations": [
            f"Recommendation {n} (from: standard/correctness)"
            for n in ("one", "two", "three", "four", "five")
        ],
    }


def review_answer() -> dict[str, object]:
    return {
        "findings": [
            {
                "path": FILE_PATH,
                "line": 11,
                "start_line": None,
                "severity": "high",
                "category": "correctness",
                "title": "Inverted branch",
                "body": "The new line inverts the branch.",
                "suggestion": None,
                "confidence": 0.9,
                "rule_name": None,
            }
        ],
        "summary": {
            "problem": "The branch is inverted.",
            "done_well": "The change is small.",
            "effort": "small",
        },
    }


ANSWERS: dict[str, Callable[[], dict[str, object]]] = {
    "RepoConventionsDraft": conventions_answer,
    "ReviewOutput": review_answer,
}


class StubState:
    """Routing and the state behind it; thread-safe, so the handler stays a thin adapter."""

    def __init__(
        self, *, prs: int, llm_delay_ms: int, sleep: Callable[[float], None] = time.sleep
    ) -> None:
        if not 1 <= prs <= MAX_PRS:
            raise ValueError(f"prs must be from 1 to {MAX_PRS}")
        self.prs = prs
        self.llm_delay_ms = llm_delay_ms
        self._sleep = sleep
        self._lock = threading.Lock()
        self._next_id = 1000
        self.review_posts: Counter[int] = Counter()
        self.reviews: dict[int, list[dict[str, object]]] = {}
        self.review_comments: dict[int, list[int]] = {}
        self.check_runs: dict[str, list[dict[str, object]]] = {}
        self.llm_calls: Counter[str] = Counter()
        self.unhandled: list[str] = []

    def _new_id(self) -> int:
        self._next_id += 1
        return self._next_id

    def stats(self) -> dict[str, object]:
        with self._lock:
            return {
                "review_posts": {str(n): self.review_posts[n] for n in sorted(self.review_posts)},
                "llm_calls": dict(sorted(self.llm_calls.items())),
                "unhandled": list(self.unhandled),
            }

    def handle(self, method: str, raw_path: str, body: bytes) -> Reply:
        path = raw_path.split("?", 1)[0]
        if method == "GET" and path == "/_stats":
            return 200, self.stats()
        if method == "POST" and path == "/v1/chat/completions":
            return self._chat(body)
        if method == "POST" and path == f"/app/installations/{INSTALLATION_ID}/access_tokens":
            return 201, {"token": "load-token", "expires_at": "2099-01-01T00:00:00Z"}
        reply = self._github(method, path, body)
        if reply is not None:
            return reply
        with self._lock:
            if len(self.unhandled) < _UNHANDLED_LIMIT:
                self.unhandled.append(f"{method} {path}")
        return 404, {"message": "Not Found"}

    def _number(self, raw: str) -> int | None:
        number = int(raw)
        return number if 1 <= number <= self.prs else None

    def _github(self, method: str, path: str, body: bytes) -> Reply | None:
        if not path.startswith(_REPO + "/"):
            return None
        rest = path.removeprefix(_REPO)
        if method == "GET" and rest == f"/git/blobs/{FILE_SHA}":
            content = base64.b64encode(FILE_TEXT).decode()
            return 200, {
                "sha": FILE_SHA,
                "encoding": "base64",
                "content": content,
                "size": len(FILE_TEXT),
            }
        if method == "GET" and rest == f"/git/trees/{BASE_SHA}":
            tree = [{"path": "README.md", "type": "blob", "sha": README_SHA, "size": 3}]
            return 200, {"tree": tree, "truncated": False}
        if method == "POST" and rest == "/check-runs":
            return self._create_check_run(body)
        if method == "PATCH" and (match := re.fullmatch(r"/check-runs/(\d+)", rest)):
            return 200, {"id": int(match.group(1))}
        if match := re.fullmatch(r"/git/trees/([0-9a-f]{40})", rest):
            return self._head_tree(method, match.group(1))
        if match := re.fullmatch(r"/commits/([0-9a-f]{40})/check-runs", rest):
            return self._check_runs(method, match.group(1))
        if match := re.fullmatch(r"/compare/([0-9a-f]{40})\.\.\.([0-9a-f]{40})", rest):
            base, head = match.groups()
            if method == "GET" and base == BASE_SHA and self._head_number(head) is not None:
                return 200, {"merge_base_commit": {"sha": BASE_SHA}}
            return None
        if match := re.fullmatch(r"/pulls/(\d+)(/.*)?", rest):
            number = self._number(match.group(1))
            if number is None:
                return None
            return self._pull(method, number, match.group(2) or "", body)
        return None

    def _head_number(self, sha: str) -> int | None:
        for number in range(1, self.prs + 1):
            if head_sha(number) == sha:
                return number
        return None

    def _head_tree(self, method: str, sha: str) -> Reply | None:
        if method != "GET" or self._head_number(sha) is None:
            return None
        tree = [{"path": FILE_PATH, "type": "blob", "sha": FILE_SHA, "size": len(FILE_TEXT)}]
        return 200, {"tree": tree, "truncated": False}

    def _check_runs(self, method: str, sha: str) -> Reply | None:
        if method != "GET" or self._head_number(sha) is None:
            return None
        with self._lock:
            runs = list(self.check_runs.get(sha, []))
        return 200, {"total_count": len(runs), "check_runs": runs}

    def _create_check_run(self, body: bytes) -> Reply:
        payload = json.loads(body)
        with self._lock:
            check_run: dict[str, object] = {
                "id": self._new_id(),
                "name": payload.get("name"),
                "head_sha": payload.get("head_sha"),
                "external_id": payload.get("external_id"),
            }
            self.check_runs.setdefault(str(payload.get("head_sha")), []).append(check_run)
        return 201, {"id": check_run["id"]}

    def _pull(self, method: str, number: int, rest: str, body: bytes) -> Reply | None:
        if method == "GET" and rest == "":
            return 200, pull_request(number)
        if method == "GET" and rest == "/commits":
            return 200, [{"commit": {"message": f"feat: load change {number}"}}]
        if method == "GET" and rest == "/files":
            return 200, [
                {
                    "filename": FILE_PATH,
                    "status": "modified",
                    "sha": FILE_SHA,
                    "additions": 1,
                    "deletions": 0,
                    "changes": 1,
                    "patch": PATCH,
                }
            ]
        if rest == "/reviews" and method == "GET":
            with self._lock:
                return 200, list(self.reviews.get(number, []))
        if rest == "/reviews" and method == "POST":
            payload = json.loads(body)
            with self._lock:
                review_id = self._new_id()
                self.review_posts[number] += 1
                self.reviews.setdefault(number, []).append(
                    {
                        "id": review_id,
                        "body": payload.get("body"),
                        "commit_id": payload["commit_id"],
                    }
                )
                self.review_comments[review_id] = [
                    self._new_id() for _ in payload.get("comments", [])
                ]
            return 200, {"id": review_id}
        if method == "GET" and (match := re.fullmatch(r"/reviews/(\d+)/comments", rest)):
            with self._lock:
                comments = self.review_comments.get(int(match.group(1)))
            if comments is None:
                return None
            return 200, [{"id": comment_id} for comment_id in comments]
        return None

    def _chat(self, body: bytes) -> Reply:
        request = json.loads(body)
        schema = str(request.get("response_format", {}).get("json_schema", {}).get("name", ""))
        with self._lock:
            self.llm_calls[schema] += 1
        answer = ANSWERS.get(schema)
        if answer is None:
            return 400, {"error": {"message": f"the load stub has no answer for schema {schema!r}"}}
        if self.llm_delay_ms:
            self._sleep(self.llm_delay_ms / 1000)
        return 200, {
            "model": str(request.get("model", "load-stub-model")),
            "choices": [{"message": {"content": json.dumps(answer())}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1200, "completion_tokens": 300},
        }


def make_server(host: str, port: int, state: StubState) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def _answer(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            status, payload = state.handle(self.command, self.path, body)
            raw = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            if status == 404:
                sys.stderr.write(f"review-load-stub: unhandled {self.command} {self.path}\n")

        do_GET = _answer
        do_POST = _answer
        do_PATCH = _answer

        def log_message(self, format: str, *args: object) -> None:
            return None

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    return server


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=9999)
    parser.add_argument("--prs", type=int, default=5, help=f"PRs 1..N to serve (at most {MAX_PRS})")
    parser.add_argument("--llm-delay-ms", type=int, default=3000, help="delay of each LLM answer")
    args = parser.parse_args()
    if not 1 <= args.prs <= MAX_PRS:
        parser.error(f"--prs must be from 1 to {MAX_PRS}")
    if args.llm_delay_ms < 0:
        parser.error("--llm-delay-ms must be >= 0")
    state = StubState(prs=args.prs, llm_delay_ms=args.llm_delay_ms)
    server = make_server(args.host, args.port, state)
    print(
        f"review load stub on http://{args.host}:{args.port} for PRs 1..{args.prs}, "
        f"LLM delay {args.llm_delay_ms} ms",
        file=sys.stderr,
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
