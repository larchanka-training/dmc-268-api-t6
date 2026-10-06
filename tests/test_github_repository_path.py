"""Validated GitHub request path segments: names and SHAs are untrusted input (api#73)."""

from __future__ import annotations

import time
from collections.abc import Callable

import httpx
import pytest

from app.common.infrastructure.github_repository_path import (
    InvalidGitHubPathSegment,
    commit_sha_segment,
    ref_segment,
    repository_path_from_full_name,
)

_SHA_40 = "0123456789abcdef0123456789abcdef01234567"
_SHA_64 = "0123456789abcdef" * 4
_API = "https://api.github.com"


def _raw_path(path: str) -> bytes:
    """The path as httpx puts it on the wire: it collapses ``.`` and ``..`` segments."""
    return httpx.Request("GET", f"{_API}{path}").url.raw_path


@pytest.mark.parametrize(
    "full_name",
    [
        "example-owner/example-repo",
        "octo/.github",
        "octo_acme/repo",
        "owner/repo.js",
        "o/r_",
        "o/-x",
        "o/r.",
        "o/...x",
        "9/9",
    ],
)
def test_repository_path_keeps_a_valid_name_byte_for_byte(full_name: str) -> None:
    path = repository_path_from_full_name(full_name)

    assert path == f"/repos/{full_name}"
    assert _raw_path(path) == f"/repos/{full_name}".encode()


@pytest.mark.parametrize(
    "full_name",
    [
        "",
        "o",
        "/",
        "o/",
        "/r",
        "o/.",
        "o/..",
        "o/...",
        "../r",
        "./r",
        "o/../../pulls/1",
        "o/r/x",
        "a/b/c",
        "o//r",
        "o.x/r",
        "-o/r",
        "o /r",
        "o/r p",
        "o/r with space",
        "o/r\n",
        "o/r\t",
        "o/r\x00",
        "o/r?x",
        "o/r#x",
        "o/r%2Fx",
        "o/%2e%2e",
        "o/%2E",
        "o/r\\x",
        "o/r١",
        "o/ré",
        "é/r",
    ],
)
def test_repository_path_rejects_a_name_outside_owner_slash_repo(full_name: str) -> None:
    with pytest.raises(InvalidGitHubPathSegment) as raised:
        repository_path_from_full_name(full_name)

    assert isinstance(raised.value, ValueError)


def test_repository_path_accepts_a_name_of_512_characters_and_rejects_one_of_513() -> None:
    longest = "o/" + "r" * 510
    too_long = "o/" + "r" * 511

    assert repository_path_from_full_name(longest) == f"/repos/{longest}"
    with pytest.raises(InvalidGitHubPathSegment) as raised:
        repository_path_from_full_name(too_long)
    assert too_long not in str(raised.value)


def test_repository_path_rejects_an_overlong_name_before_the_regex_can_backtrack() -> None:
    """``o/a.a.a....!`` costs seconds in the regex once it has tens of thousands of pieces."""
    crafted = "o/" + "a." * 20_000 + "!"
    started = time.perf_counter()

    with pytest.raises(InvalidGitHubPathSegment):
        repository_path_from_full_name(crafted)

    assert time.perf_counter() - started < 0.5


@pytest.mark.parametrize(
    ("name", "encoded"),
    [
        ("main", "main"),
        ("release/1.0", "release%2F1.0"),
        ("feat/a b", "feat%2Fa%20b"),
        ("u\u00fcber", "u%C3%BCber"),
        ("a?b", "a%3Fb"),
        ("a#b", "a%23b"),
        ("a..b", "a..b"),
        (".x", ".x"),
        ("...", "..."),
        ("%2e", "%252e"),
    ],
)
def test_ref_segment_percent_encodes_the_whole_name(name: str, encoded: str) -> None:
    segment = ref_segment(name)

    assert segment == encoded
    assert (
        _raw_path(f"/repos/o/r/git/trees/{segment}") == f"/repos/o/r/git/trees/{encoded}".encode()
    )


def test_ref_segment_keeps_a_slashed_branch_in_one_path_segment() -> None:
    assert _raw_path(f"/repos/o/r/git/trees/{ref_segment('a/b/c')}") == (
        b"/repos/o/r/git/trees/a%2Fb%2Fc"
    )


@pytest.mark.parametrize("name", ["", ".", ".."])
def test_ref_segment_rejects_what_would_walk_the_request_path(name: str) -> None:
    with pytest.raises(InvalidGitHubPathSegment) as raised:
        ref_segment(name)

    assert isinstance(raised.value, ValueError)


@pytest.mark.parametrize("sha", [_SHA_40, _SHA_64, _SHA_40.upper(), "aB" * 20, "0" * 40, "f" * 64])
def test_commit_sha_segment_returns_a_full_hex_sha_unchanged(sha: str) -> None:
    assert commit_sha_segment(sha) == sha


@pytest.mark.parametrize(
    "sha",
    [
        "",
        "main",
        "..",
        "../../x",
        "a" * 39,
        "a" * 41,
        "a" * 63,
        "a" * 65,
        "a" * 128,
        "g" * 40,
        f"{_SHA_40}\n",
        f" {_SHA_40}",
        f"{_SHA_40[:-1]}/",
        f"{_SHA_40[:-3]}%2e.",
        "١" * 40,
    ],
)
def test_commit_sha_segment_rejects_anything_but_a_full_hex_sha(sha: str) -> None:
    with pytest.raises(InvalidGitHubPathSegment) as raised:
        commit_sha_segment(sha)

    assert isinstance(raised.value, ValueError)


@pytest.mark.parametrize(
    ("build", "value"),
    [
        (repository_path_from_full_name, "leak-marker/../../pulls/1"),
        (repository_path_from_full_name, "leak-marker/%2e%2e"),
        (ref_segment, ".."),
        (commit_sha_segment, "leak-marker/../.."),
    ],
)
def test_the_error_never_carries_the_rejected_value(
    build: Callable[[str], str], value: str
) -> None:
    with pytest.raises(InvalidGitHubPathSegment) as raised:
        build(value)

    assert "leak-marker" not in str(raised.value)
    assert "leak-marker" not in repr(raised.value)
    assert raised.value.args == (str(raised.value),)
    assert ".." not in str(raised.value)
