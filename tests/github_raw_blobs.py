"""GitHub blob answers of test fakes: a policy read streams the blob raw (#168).

The run's other blob reads (diff, RAG) still ask for the base64 JSON, so a fake that serves
both answers by the ``Accept`` header, as GitHub does.
"""

from __future__ import annotations

import base64
import hashlib

import httpx

RAW_BLOB_MEDIA_TYPE = "application/vnd.github.raw+json"


def git_blob_oid(raw: bytes) -> str:
    """The Git SHA-1 object id of a blob: the raw policy read checks it against the bytes."""
    return hashlib.sha1(b"blob %d\0" % len(raw) + raw, usedforsecurity=False).hexdigest()


def blob_answer(request: httpx.Request, raw: bytes) -> httpx.Response:
    """The raw bytes when the raw media type is asked for, else the base64 JSON answer."""
    if request.headers.get("Accept") == RAW_BLOB_MEDIA_TYPE:
        return httpx.Response(200, content=raw)
    sha = request.url.path.rsplit("/", 1)[1]
    content = base64.b64encode(raw).decode()
    return httpx.Response(
        200, json={"sha": sha, "encoding": "base64", "content": content, "size": len(raw)}
    )
