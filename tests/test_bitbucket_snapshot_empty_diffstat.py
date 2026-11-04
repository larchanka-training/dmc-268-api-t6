"""A distinct Bitbucket head cannot be certified by an empty diffstat."""

from __future__ import annotations

import asyncio

import httpx

from app.modules.code_index.application.vcs import Ancestry
from tests.bitbucket_snapshot_fakes import _SHA, _Credentials, _source


def test_distinct_head_with_empty_diffstat_requires_full_rebuild() -> None:
    base = "b" * 40

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(f"/merge-base/{base}..{_SHA}"):
            return httpx.Response(200, json={"hash": base})
        assert request.url.path.endswith(f"/diffstat/{_SHA}..{base}")
        return httpx.Response(200, json={"size": 0, "values": []})

    async def exercise() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            comparison = await _source(_Credentials(), client).compare(base, _SHA)
            assert comparison is not None
            assert comparison.ancestry is Ancestry.AHEAD
            assert comparison.files == ()
            assert not comparison.complete

    asyncio.run(exercise())
