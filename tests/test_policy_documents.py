"""Policy documents: bytes, normalization, presence and the paths of the base tree."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from hashlib import sha256

import pytest

from app.modules.reviews.application.policy_documents import (
    LoadedPolicyDocuments,
    LoadPolicyDocuments,
    PolicyEntryKind,
    PolicyTreeEntry,
    normalize_document,
)
from app.modules.reviews.application.policy_errors import (
    PolicyError,
    PolicyErrorCode,
    policy_error_detail,
)
from app.modules.reviews.application.policy_snapshot import DocumentPresence

_BASE = "b" * 40
_RULES = ".review/rules.md"
_OID = "f" * 40

type _Trees = dict[str | None, dict[str, PolicyTreeEntry]]


def _file(oid: str, size: int | None = 10) -> PolicyTreeEntry:
    return PolicyTreeEntry(PolicyEntryKind.FILE, oid, size)


@dataclass
class Source:
    """Trees by oid (``None`` is the root) and blobs by oid."""

    trees: _Trees
    blobs: dict[str, bytes] = field(default_factory=dict)
    listed: list[str | None] = field(default_factory=list)
    read: list[str] = field(default_factory=list)

    async def list_entries(self, tree_oid: str | None) -> Mapping[str, PolicyTreeEntry]:
        self.listed.append(tree_oid)
        return self.trees[tree_oid]

    async def read_blob(self, oid: str) -> bytes:
        self.read.append(oid)
        return self.blobs[oid]


def _load(source: Source) -> LoadedPolicyDocuments:
    return asyncio.run(LoadPolicyDocuments(source, policy_base_sha=_BASE).execute())


def _fail(source: Source) -> PolicyError:
    with pytest.raises(PolicyError) as caught:
        _load(source)
    return caught.value


def test_text_is_normalized_and_both_checksums_are_kept() -> None:
    raw = b"\xef\xbb\xbf# Rules\r\nline\rlast"
    document = normalize_document(_RULES, raw, blob_oid=_OID)

    assert document.presence is DocumentPresence.PRESENT
    assert document.text == "# Rules\nline\nlast"
    assert document.raw_sha256 == sha256(raw).hexdigest()
    assert document.text_sha256 == sha256(b"# Rules\nline\nlast").hexdigest()
    assert (document.byte_count, document.line_count, document.blob_oid) == (21, 3, _OID)


def test_text_is_not_otherwise_changed() -> None:
    raw = "Café &amp; %20\tTab  \n\n```review-ignore\ngenerated/**\n```\n".encode()
    document = normalize_document(_RULES, raw, blob_oid=_OID)

    assert document.text == raw.decode()
    assert document.line_count == 5


def test_empty_file_is_present_with_the_checksum_of_empty_bytes() -> None:
    document = normalize_document(_RULES, b"", blob_oid=_OID)

    assert document.presence is DocumentPresence.PRESENT
    assert (document.text, document.byte_count, document.line_count) == ("", 0, 0)
    assert document.raw_sha256 == (
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    )


def test_document_of_exactly_64_kib_is_accepted_and_one_more_byte_is_not() -> None:
    assert normalize_document(_RULES, b"a" * 65_536, blob_oid=_OID).byte_count == 65_536

    with pytest.raises(PolicyError) as caught:
        normalize_document("AGENTS.md", b"a" * 65_537, blob_oid=_OID, policy_base_sha=_BASE)
    error = caught.value
    assert error.error_code == "policy_document_too_large"
    assert (error.source_path, error.limit, error.actual) == ("AGENTS.md", 65_536, 65_537)
    assert error.policy_base_sha == _BASE
    assert error.retryable is False


def test_bom_counts_toward_the_byte_limit() -> None:
    with pytest.raises(PolicyError) as caught:
        normalize_document(_RULES, b"\xef\xbb\xbf" + b"a" * 65_534, blob_oid=_OID)
    assert caught.value.code is PolicyErrorCode.DOCUMENT_TOO_LARGE


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param(b"ok\n\xff\xfe", id="invalid-bytes"),
        # U+D800 encoded as UTF-8: an unpaired surrogate is never valid UTF-8 (§3.2 item 5).
        pytest.param(b"ok\n\xed\xa0\x80", id="surrogate"),
    ],
)
def test_broken_utf8_is_rejected_without_replacement(raw: bytes) -> None:
    with pytest.raises(PolicyError) as caught:
        normalize_document(_RULES, raw, blob_oid=_OID)
    assert caught.value.code is PolicyErrorCode.DOCUMENT_DECODE_ERROR
    assert "�" not in caught.value.message


def test_control_character_is_reported_with_its_line_and_column() -> None:
    for raw, line, column in (
        (b"ok\nab\x00c", 2, 3),
        (b"\x1bfirst", 1, 1),
        ("ok\nжж\x7f".encode(), 2, 3),
    ):
        with pytest.raises(PolicyError) as caught:
            normalize_document(_RULES, raw, blob_oid=_OID)
        assert caught.value.code is PolicyErrorCode.DOCUMENT_CONTROL_CHARACTER
        assert (caught.value.line, caught.value.column) == (line, column)

    assert normalize_document(_RULES, b"a\tb\n", blob_oid=_OID).text == "a\tb\n"


def test_lfs_pointer_is_not_a_policy_document() -> None:
    pointer = b"version https://git-lfs.github.com/spec/v1\noid sha256:abc\nsize 12\n"
    with pytest.raises(PolicyError) as caught:
        normalize_document(_RULES, pointer, blob_oid=_OID)
    assert caught.value.code is PolicyErrorCode.DOCUMENT_TYPE_UNSUPPORTED


def test_both_documents_are_loaded_from_the_base_tree_by_blob_id() -> None:
    source = Source(
        trees={
            None: {
                ".review": PolicyTreeEntry(PolicyEntryKind.DIRECTORY, "1" * 40, None),
                "AGENTS.md": _file("2" * 40),
            },
            "1" * 40: {"rules.md": _file("3" * 40)},
        },
        blobs={"2" * 40: b"agents", "3" * 40: b"rules"},
    )

    loaded = _load(source)

    assert (
        loaded.rules.document.path,
        loaded.rules.document.text,
        loaded.rules.document.blob_oid,
    ) == (
        ".review/rules.md",
        "rules",
        "3" * 40,
    )
    assert (loaded.agents.document.path, loaded.agents.document.text) == ("AGENTS.md", "agents")
    assert source.listed == [None, "1" * 40]
    assert source.read == ["3" * 40, "2" * 40]


def test_absent_documents_are_missing_and_nothing_is_read() -> None:
    cases: tuple[_Trees, ...] = (
        {None: {}},
        {
            None: {".review": PolicyTreeEntry(PolicyEntryKind.DIRECTORY, "1" * 40, None)},
            "1" * 40: {"other.md": _file("3" * 40)},
        },
    )
    for trees in cases:
        source = Source(trees=trees)
        loaded = _load(source)

        assert loaded.rules.document.presence is DocumentPresence.MISSING
        assert loaded.agents.document.presence is DocumentPresence.MISSING
        assert (loaded.rules.document.text, loaded.rules.document.raw_sha256) == (None, None)
        assert source.read == []


def test_only_the_root_agents_document_is_policy() -> None:
    source = Source(
        trees={
            None: {"docs": PolicyTreeEntry(PolicyEntryKind.DIRECTORY, "1" * 40, None)},
            "1" * 40: {"AGENTS.md": _file("2" * 40)},
        }
    )

    assert _load(source).agents.document.presence is DocumentPresence.MISSING
    assert source.listed == [None]


def test_symlink_submodule_and_wrong_parent_type_are_not_followed() -> None:
    cases: tuple[_Trees, ...] = (
        {None: {"AGENTS.md": PolicyTreeEntry(PolicyEntryKind.SYMLINK, "2" * 40, 9)}},
        {None: {"AGENTS.md": PolicyTreeEntry(PolicyEntryKind.DIRECTORY, "2" * 40, None)}},
        {None: {".review": PolicyTreeEntry(PolicyEntryKind.SUBMODULE, "1" * 40, None)}},
        {None: {".review": _file("1" * 40)}},
        {
            None: {".review": PolicyTreeEntry(PolicyEntryKind.DIRECTORY, "1" * 40, None)},
            "1" * 40: {"rules.md": PolicyTreeEntry(PolicyEntryKind.SYMLINK, "3" * 40, 9)},
        },
    )
    for trees in cases:
        source = Source(trees=trees)
        error = _fail(source)

        assert error.code is PolicyErrorCode.DOCUMENT_TYPE_UNSUPPORTED
        assert error.policy_base_sha == _BASE
        assert source.read == []


def test_oversized_document_is_rejected_by_its_listed_size_before_any_download() -> None:
    source = Source(trees={None: {"AGENTS.md": _file("2" * 40, size=87_040)}})

    error = _fail(source)

    assert error.code is PolicyErrorCode.DOCUMENT_TOO_LARGE
    assert (error.source_path, error.limit, error.actual) == ("AGENTS.md", 65_536, 87_040)
    assert source.read == []


def test_read_failure_is_an_error_not_a_missing_document() -> None:
    @dataclass
    class Failing(Source):
        async def read_blob(self, oid: str) -> bytes:
            raise PolicyError(PolicyErrorCode.FETCH_TRANSIENT, "GitHub answered 502")

    error = _fail(Failing(trees={None: {"AGENTS.md": _file("2" * 40)}}))

    assert error.code is PolicyErrorCode.FETCH_TRANSIENT
    assert error.retryable is True


@pytest.mark.parametrize(
    ("failure", "detail"),
    [
        pytest.param(
            PolicyError(
                PolicyErrorCode.RATE_LIMITED,
                "GitHub answered 429",
                policy_base_sha=_BASE,
                retry_after=timedelta(minutes=15),
            ),
            "file `AGENTS.md`",
            id="rate-limited",
        ),
        pytest.param(
            PolicyError(
                PolicyErrorCode.DOCUMENT_TOO_LARGE,
                "The policy document exceeds 65536 bytes",
                policy_base_sha=_BASE,
                limit=65_536,
            ),
            "file `AGENTS.md`, allowed 65536",
            id="stopped-stream",
        ),
    ],
)
def test_failed_blob_read_names_its_document_and_keeps_the_rest(
    failure: PolicyError, detail: str
) -> None:
    @dataclass
    class Failing(Source):
        async def read_blob(self, oid: str) -> bytes:
            raise failure

    error = _fail(Failing(trees={None: {"AGENTS.md": _file("2" * 40)}}))

    # The source reads by blob SHA; the loader knows the document (RFS §9).
    assert (error.code, error.source_path, error.policy_base_sha) == (
        failure.code,
        "AGENTS.md",
        _BASE,
    )
    assert (error.retry_after, error.limit) == (failure.retry_after, failure.limit)
    assert policy_error_detail(error.message) == detail
    assert error.__cause__ is failure
