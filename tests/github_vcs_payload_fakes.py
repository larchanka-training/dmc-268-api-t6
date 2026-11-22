"""Shared GitHub VCS pull payload for adapter and snapshot tests."""

from __future__ import annotations

_HEAD = "a" * 40


_BASE = "b" * 40


def _pull_payload(*, changed_files: int = 101, head_sha: str = _HEAD) -> dict[str, object]:
    return {
        "id": 901,
        "number": 7,
        "title": "Fix parser",
        "body": "Review me",
        "user": {"login": "octo"},
        "head": {"ref": "feature", "sha": head_sha, "repo": {"full_name": "fork/repo"}},
        "base": {"ref": "main", "sha": _BASE, "repo": {"full_name": "octo/repo"}},
        "labels": [{"name": "backend"}],
        "draft": False,
        "changed_files": changed_files,
        "additions": 102,
        "deletions": 1,
    }
