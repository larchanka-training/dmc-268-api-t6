"""Shared fakes of the GitLab Run HTTP, VCS and source tests."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import unquote
from uuid import UUID

import httpx

from app.modules.integrations.gitlab.application.manage_connection import GitLabOriginPolicy
from app.modules.integrations.gitlab.application.request_credentials import (
    BoundGitLabTarget,
    GitLabRequestCredential,
)
from app.modules.integrations.gitlab.infrastructure.run_credentials import FetchCode
from app.modules.reviews.application.process_run import RunVcsInput
from app.modules.reviews.application.vcs_diff import PullRequestLocator
from tests.gitlab_http_fakes import Stream

ORIGIN = "https://gitlab.com"
IDENTITY = UUID("10000000-0000-0000-0000-000000000001")
WORKSPACE = UUID("10000000-0000-0000-0000-000000000002")
REPOSITORY = UUID("10000000-0000-0000-0000-000000000003")
INPUT = RunVcsInput(
    IDENTITY,
    REPOSITORY,
    "a" * 40,
    "b" * 40,
    PullRequestLocator(71, "team/project", 12, "gitlab", IDENTITY, 17),
)
TARGET = BoundGitLabTarget(IDENTITY, WORKSPACE, REPOSITORY, ORIGIN, 17, 1)
POLICY = GitLabOriginPolicy(frozenset({ORIGIN}))
TOKEN = "synthetic-run-PAT-canary"


@dataclass
class Credentials:
    expected_input: RunVcsInput = INPUT
    target: BoundGitLabTarget = TARGET
    admitted: int = 0
    checks: int = 0
    failure: Exception | None = None

    async def bind(self, *, error_code: FetchCode = "diff_fetch_failed") -> BoundGitLabTarget:
        return self.target

    async def credential_for_request(
        self, *, error_code: FetchCode = "diff_fetch_failed"
    ) -> GitLabRequestCredential:
        self.admitted += 1
        return GitLabRequestCredential(self.target, TOKEN)

    async def checkpoint(self, *, error_code: FetchCode = "diff_fetch_failed") -> None:
        self.checks += 1
        if self.failure is not None:
            raise self.failure


def refs() -> dict[str, str]:
    return {"head_sha": "a" * 40, "start_sha": "b" * 40, "base_sha": "c" * 40}


def version_refs() -> dict[str, str]:
    return {"head_commit_sha": "a" * 40, "start_commit_sha": "b" * 40, "base_commit_sha": "c" * 40}


@dataclass
class Router:
    requests: list[httpx.Request] = field(default_factory=list)
    canonical: dict[str, Any] = field(
        default_factory=lambda: {
            "id": 901,
            "iid": 12,
            "project_id": 17,
            "target_project_id": 17,
            "source_project_id": 29,
            "state": "opened",
            "diff_refs": refs(),
            "changes_count": "2",
            "title": "Title",
            "description": "Description",
            "author": {"username": "author"},
            "source_branch": "feature",
            "target_branch": "main",
            "labels": ["review"],
            "draft": False,
        }
    )
    files: list[dict[str, Any]] = field(
        default_factory=lambda: [
            {
                "old_path": "src/old.py",
                "new_path": "src/new.py",
                "new_file": False,
                "deleted_file": False,
                "renamed_file": True,
                "diff": "@@ -1 +1 @@\n-old\n+new\n",
            },
            {
                "old_path": "asset.dat",
                "new_path": "asset.dat",
                "new_file": False,
                "deleted_file": False,
                "renamed_file": False,
                "diff": "",
            },
        ]
    )
    streams: list[Stream] = field(default_factory=list)

    def response(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        headers: dict[str, str] = {"Content-Type": "application/json"}
        status = 200
        data: Any = {}
        raw: bytes | None = None
        if path.endswith("/merge_requests/12"):
            data = self.canonical
        elif path.endswith("/versions"):
            data = [{"id": 9, **version_refs()}]
            headers["X-Next-Page"] = ""
        elif path.endswith("/versions/9"):
            data = {
                "id": 9,
                **version_refs(),
                "state": "collected",
                "real_size": str(len(self.files)),
                "diffs": self.files,
            }
        elif "/repository/files/" in path:
            name = unquote(path.split("/repository/files/")[1])
            oid = "d" * 40 if name == "src/new.py" else "e" * 40
            headers = {
                "X-Gitlab-Blob-Id": oid,
                "X-Gitlab-Size": "3",
                "X-Gitlab-Ref": request.url.params["ref"],
            }
            raw = b""
        elif "/repository/blobs/" in path:
            raw = b"\x00xy" if "/" + "e" * 40 + "/" in path else b"new"
        elif path.endswith("/commits"):
            data = [{"id": "f" * 40, "message": "Subject"}]
            headers["X-Next-Page"] = ""
        else:
            status = 501
        stream = Stream((raw if raw is not None else json.dumps(data).encode(),))
        self.streams.append(stream)
        return httpx.Response(status, headers=headers, stream=stream)


def reply(
    data: Any = None,
    *,
    status: int = 200,
    headers: dict[str, str] | None = None,
    raw: bytes | None = None,
) -> httpx.Response:
    return httpx.Response(
        status,
        headers={"Content-Type": "application/json", **(headers or {})},
        stream=Stream((json.dumps(data).encode() if raw is None else raw,)),
    )


RUN_ID = UUID(int=12)


async def checkpoint() -> None:
    pass


@dataclass
class SourceRouter:
    requests: list[httpx.Request] = field(default_factory=list)
    vcs: Router = field(default_factory=Router)
    files: dict[str, tuple[str, bytes]] = field(
        default_factory=lambda: {
            "src/app.py": ("d" * 40, b"new"),
            "web/app.ts": ("e" * 40, b"x"),
            "notes.unknown": ("f" * 40, b"unknown"),
            "empty.py": ("0" * 40, b""),
            "AGENTS.md": ("1" * 40, b"Use uv.\n"),
            ".review/rules.md": ("2" * 40, b"```review-ignore\ngenerated/**\n```\n"),
        }
    )

    def entry(self, path: str) -> dict[str, Any]:
        return {
            "id": self.files[path][0],
            "name": path.rsplit("/", 1)[-1],
            "path": path,
            "type": "blob",
            "mode": "100644",
        }

    def response(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path.endswith("/repository/tree"):
            path = request.url.params.get("path", "")
            recursive = request.url.params.get("recursive") == "true"
            if path == ".review":
                return reply([self.entry(".review/rules.md")])
            if recursive:
                return reply(
                    [
                        self.entry(name)
                        for name in ("src/app.py", "web/app.ts", "notes.unknown", "empty.py")
                    ]
                )
            return reply(
                [
                    self.entry("AGENTS.md"),
                    {
                        "id": "3" * 40,
                        "name": ".review",
                        "path": ".review",
                        "type": "tree",
                        "mode": "040000",
                    },
                ]
            )
        if "/repository/files/" in request.url.path:
            path = unquote(request.url.path.split("/repository/files/")[1])
            if path not in self.files:
                return reply(status=404)
            oid, raw = self.files[path]
            return reply(
                raw=b"",
                headers={
                    "X-Gitlab-Blob-Id": oid,
                    "X-Gitlab-Size": str(len(raw)),
                    "X-Gitlab-Ref": request.url.params["ref"],
                },
            )
        if "/repository/blobs/" in request.url.path:
            oid = request.url.path.split("/repository/blobs/")[1].split("/")[0]
            found = next((raw for sha, raw in self.files.values() if sha == oid), None)
            return reply(raw=found) if found is not None else reply(status=404)
        return self.vcs.response(request)
