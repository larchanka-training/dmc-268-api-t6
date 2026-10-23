"""Opt-in proof of the index policy adapter over a localhost Postman simulation."""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import tarfile
import time
from urllib.parse import urlsplit

import httpx

from app.modules.code_index.application.vcs import SourceArchive
from app.modules.code_index.domain.limits import IndexLimits
from app.modules.code_index.infrastructure.github_snapshot_source import (
    GitHubRepositorySnapshotSource,
)
from app.modules.code_index.infrastructure.tar_archive import TarArchiveReader
from app.modules.reviews.application.policy_errors import PolicyError
from app.modules.reviews.application.review_ignore import IgnoreMatcher, parse_review_ignore

COMMIT = "c" * 40
DIRECTORY = "d" * 40
BLOB = "e" * 40
RULES = ".review/rules.md"


class RequestRecorder:
    def __init__(self) -> None:
        self.paths: list[str] = []

    async def __call__(self, request: httpx.Request) -> None:
        self.paths.append(request.url.path)


class FakeTokens:
    async def get_installation_access_token(self, installation_external_id: int) -> str:
        assert installation_external_id == 7
        return "index-policy-fake-token"


def archive_without_policy() -> SourceArchive:
    """Synthetic export-ignore archive shape, including an ignored source target."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for path, content in {
            ".gitattributes": b".review export-ignore\n",
            "private/secret.py": b"SECRET = 1\n",
            "app/main.py": b"PUBLIC = 1\n",
        }.items():
            member = tarfile.TarInfo(f"fixture-{COMMIT}/{path}")
            member.size = len(content)
            archive.addfile(member, io.BytesIO(content))
    return SourceArchive(commit_sha=COMMIT, content=buffer.getvalue())


async def verify(base_url: str) -> None:
    scan = await TarArchiveReader(IndexLimits()).scan(
        archive_without_policy(), documents=frozenset({RULES}), deadline=time.monotonic() + 10
    )
    assert RULES not in scan.entries and RULES not in scan.documents
    assert "private/secret.py" in scan.entries
    cases = (
        ("hidden", 3, None),
        ("missing-root", 1, None),
        ("missing-rules", 2, None),
        ("blob-404", 3, "policy_revision_unavailable"),
        ("blob-500", 3, "policy_fetch_transient"),
        ("truncated", 1, "policy_revision_unavailable"),
        ("root-404", 1, "policy_revision_unavailable"),
    )
    total = 0
    for name, count, error_code in cases:
        record = RequestRecorder()

        async with httpx.AsyncClient(
            base_url=base_url,
            headers={"x-mock-slug": "github"},
            event_hooks={"request": [record]},
            trust_env=False,
        ) as client:
            source = GitHubRepositorySnapshotSource(
                client=client,
                token_provider=FakeTokens(),
                installation_external_id=7,
                repository_full_name=f"fixture/{name}",
            )
            outcome: str
            try:
                document = await source.rules_document(COMMIT)
            except PolicyError as error:
                assert error_code == error.code.value, (name, error.code)
                assert error.policy_base_sha == COMMIT
                assert error.__cause__ is None
                outcome = error.code.value
            else:
                assert error_code is None, (name, "expected a policy error")
                policy = parse_review_ignore(document.text)
                if name == "hidden":
                    assert document.is_present and document.blob_oid == BLOB
                    assert policy.patterns == ("private/**",)
                    matcher = IgnoreMatcher(policy)
                    assert matcher.match("private/secret.py") == "private/**"
                    assert matcher.match("app/main.py") is None
                else:
                    assert document.is_missing and policy.patterns == ()
                outcome = document.presence.value
            expected = [f"/repos/fixture/{name}/git/trees/{COMMIT}"]
            if count >= 2:
                expected.append(f"/repos/fixture/{name}/git/trees/{DIRECTORY}")
            if count == 3:
                expected.append(f"/repos/fixture/{name}/git/blobs/{BLOB}")
            assert record.paths == expected, (name, record.paths)
            assert source.request_count == count, (name, source.request_count)
            total += count
            print(json.dumps({"case": name, "outcome": outcome, "requests": count}))
    assert total == 14
    print("PASS: 7 cases, 14 real HTTP requests; no AGENTS blob or branch/contents reads")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:4907")
    args = parser.parse_args()
    url = urlsplit(args.base_url)
    if (
        url.scheme != "http"
        or url.hostname != "127.0.0.1"
        or url.username
        or url.password
        or not url.port
        or url.path not in {"", "/"}
        or url.query
        or url.fragment
    ):
        parser.error("base URL must be http://127.0.0.1:<explicit-port>")
    asyncio.run(verify(args.base_url))


if __name__ == "__main__":
    main()
