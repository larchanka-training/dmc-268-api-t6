"""Pass 1 and pass 2 over the tar archive of a commit (ADR 0001 §7, AC10; #142).

The archive is read from memory and never written to disk: an unsafe entry stops the
job, a symlink joins the path set unread, and the entries, unpacked-bytes and deadline
limits stop a pass in time.
"""

from __future__ import annotations

import asyncio
import gzip
import io
import os
import tarfile
import time
from collections.abc import Iterable, Set

import pytest

from app.modules.code_index.application.vcs import (
    ArchiveCounters,
    ArchiveEntry,
    ArchiveReader,
    ArchiveScan,
    ArchiveUnsafe,
    IndexLimitExceeded,
    SourceArchive,
    VcsUnavailable,
)
from app.modules.code_index.domain.limits import IndexLimits
from app.modules.code_index.infrastructure.tar_archive import TarArchiveReader

_COMMIT = "0123abc" + "0" * 33
_ROOT = "octo-repo-0123abc"
_RULES = ".review/rules.md"
_DOCUMENTS = frozenset({_RULES, "go.mod"})
# The pax record "52 comment=<40 hex>\n" of the global header GitHub writes first.
_GLOBAL_HEADER_BYTES = 52
_MIB = 1_048_576
# `git hash-object` of "hello\n" and of an empty file.
_HELLO_SHA = "ce013625030ba8dba906f756967f9e9ca394464a"
_EMPTY_SHA = "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391"

Member = tuple[tarfile.TarInfo, bytes]


def _file(path: str, data: bytes = b"", *, root: str = _ROOT) -> Member:
    info = tarfile.TarInfo(f"{root}/{path}")
    info.size = len(data)
    return info, data


def _special(name: str, kind: bytes, **attributes: object) -> Member:
    info = tarfile.TarInfo(name)
    info.type = kind
    for key, value in attributes.items():
        setattr(info, key, value)
    return info, b""


def _symlink(path: str, target: str) -> Member:
    return _special(f"{_ROOT}/{path}", tarfile.SYMTYPE, linkname=target)


def _directory(path: str) -> Member:
    return _special(f"{_ROOT}/{path}", tarfile.DIRTYPE)


def _archive(*members: Member, gnu: Iterable[Member] = ()) -> SourceArchive:
    """A GitHub-like tarball: a global pax header with the commit, the root directory,
    then `members` (pax format) and `gnu` (GNU format)."""
    buffer = io.BytesIO()
    with tarfile.open(
        fileobj=buffer,
        mode="w:gz",
        format=tarfile.PAX_FORMAT,
        pax_headers={"comment": _COMMIT},
        compresslevel=1,
    ) as archive:
        root = tarfile.TarInfo(_ROOT)
        root.type = tarfile.DIRTYPE
        archive.addfile(root)
        for info, data in members:
            archive.addfile(info, io.BytesIO(data) if info.isreg() else None)
        archive.format = tarfile.GNU_FORMAT
        for info, data in gnu:
            archive.addfile(info, io.BytesIO(data) if data else None)
    return SourceArchive(_COMMIT, buffer.getvalue())


def _raw_header(
    name: str, kind: bytes = tarfile.REGTYPE, size: int | bytes = 0, *, extended: bool = False
) -> bytes:
    """One GNU header block as a forged stream writes it: `size` may be a raw field, and
    `extended` continues the map of a sparse header."""
    block = bytearray(tarfile.BLOCKSIZE)
    block[: len(name)] = name.encode()
    block[124:136] = size if isinstance(size, bytes) else b"%011o\0" % size
    block[156:157] = kind
    block[257:265] = tarfile.GNU_MAGIC
    block[482] = extended
    block[148:156] = b" " * 8
    block[148:156] = b"%06o\0 " % sum(block)
    return bytes(block)


def _padded(data: bytes) -> bytes:
    return data + b"\0" * (-len(data) % tarfile.BLOCKSIZE)


def _raw_pax(key: str, value: str) -> bytes:
    """A pax header with one record, `"<length> <key>=<value>\\n"`."""
    record = f" {key}={value}\n".encode()
    length = len(record) + 1
    while len(str(length)) + len(record) != length:
        length += 1
    body = str(length).encode() + record
    return _raw_header("PaxHeader", tarfile.XHDTYPE, len(body)) + _padded(body)


def _raw_long_name(name: str = "a") -> bytes:
    data = name.encode() + b"\0"
    return _raw_header("././@LongLink", tarfile.GNUTYPE_LONGNAME, len(data)) + _padded(data)


def _sparse_extension(*, extended: bool) -> bytes:
    block = bytearray(tarfile.BLOCKSIZE)
    block[504] = extended
    return bytes(block)


def _raw_archive(*blocks: bytes, end: bool = True) -> SourceArchive:
    """A forged stream: the root directory, `blocks`, then the end blocks if `end`."""
    raw = _raw_header(_ROOT, tarfile.DIRTYPE) + b"".join(blocks)
    return SourceArchive(_COMMIT, gzip.compress(raw + b"\0" * 1024 * end, compresslevel=1))


def _reader(**limits: int) -> TarArchiveReader:
    return TarArchiveReader(IndexLimits(**limits))


def _later(seconds: float = 60.0) -> float:
    return time.monotonic() + seconds


def _scan(
    archive: SourceArchive, reader: ArchiveReader | None = None, deadline: float | None = None
) -> ArchiveScan:
    reader = reader or _reader()
    return asyncio.run(reader.scan(archive, documents=_DOCUMENTS, deadline=deadline or _later()))


def _contents(
    archive: SourceArchive,
    paths: Set[str],
    *,
    reader: ArchiveReader | None = None,
    batch_files: int = 50,
    deadline: float | None = None,
) -> list[tuple[tuple[str, bytes], ...]]:
    reader = reader or _reader()

    async def collect() -> list[tuple[tuple[str, bytes], ...]]:
        return [
            batch
            async for batch in reader.contents(
                archive, paths, deadline=deadline or _later(), batch_files=batch_files
            )
        ]

    return asyncio.run(collect())


def test_scan_lists_files_and_symlinks_without_the_root_and_hashes_them_as_git_blobs() -> None:
    archive = _archive(
        _directory("pkg"),
        _file("pkg/hello.py", b"hello\n"),
        _file("pkg/empty.py"),
        _symlink("pkg/link.py", "hello.py"),
        _file(_RULES, b"rules\n"),
        _file("go.mod", b"module example.com/m\n"),
        _file("nested/go.mod", b"module example.com/n\n"),
    )

    scan = _scan(archive)

    assert scan.commit_sha == _COMMIT
    assert scan.entries == {
        "pkg/hello.py": ArchiveEntry("pkg/hello.py", 6, blob_sha=_HELLO_SHA),
        "pkg/empty.py": ArchiveEntry("pkg/empty.py", 0, blob_sha=_EMPTY_SHA),
        # A symlink is in the path set but never read: no size, no blob.
        "pkg/link.py": ArchiveEntry("pkg/link.py", 0, symlink=True),
        _RULES: ArchiveEntry(_RULES, 6, blob_sha="de7475d8888cd0da43c60eccee4040c767cd2a80"),
        "go.mod": ArchiveEntry("go.mod", 21, blob_sha="96714cb041fa8a1cea6b44a975ae510d19a4af2f"),
        "nested/go.mod": ArchiveEntry(
            "nested/go.mod", 21, blob_sha="00b8ca4edf079ff3f1733d64602f0189c010ee01"
        ),
    }
    # Only the root documents asked for are captured.
    assert scan.documents == {_RULES: b"rules\n", "go.mod": b"module example.com/m\n"}
    # The global header, the root and pkg directories, and six members.
    assert scan.counters == ArchiveCounters(headers=9, unpacked_bytes=_GLOBAL_HEADER_BYTES + 54)


def test_a_file_over_the_read_limit_is_listed_without_a_blob_and_never_captured() -> None:
    archive = _archive(_file("big.py", b"x" * 11), _file(_RULES, b"r" * 11), _file("ok.py", b"y"))

    scan = _scan(archive, _reader(read_bytes=10))

    assert scan.entries["big.py"] == ArchiveEntry("big.py", 11)
    assert scan.entries[_RULES] == ArchiveEntry(_RULES, 11)
    assert scan.entries["ok.py"].blob_sha is not None
    assert scan.documents == {}
    assert _contents(archive, {"big.py", "ok.py"}, reader=_reader(read_bytes=10)) == [
        (("ok.py", b"y"),)
    ]


@pytest.mark.parametrize(
    ("member", "gnu"),
    [
        pytest.param(_special("/etc/passwd", tarfile.REGTYPE), False, id="absolute-path"),
        pytest.param(_file("../outside.py", b"x"), False, id="parent-segment"),
        pytest.param(_file("a/../../outside.py", b"x"), False, id="inner-parent-segment"),
        pytest.param(
            _special(f"{_ROOT}/hard.py", tarfile.LNKTYPE, linkname=f"{_ROOT}/a.py"),
            False,
            id="hardlink",
        ),
        pytest.param(_special(f"{_ROOT}/tty", tarfile.CHRTYPE), False, id="char-device"),
        pytest.param(_special(f"{_ROOT}/disk", tarfile.BLKTYPE), False, id="block-device"),
        pytest.param(_special(f"{_ROOT}/pipe", tarfile.FIFOTYPE), False, id="fifo"),
        pytest.param(_special(f"{_ROOT}/sparse.py", tarfile.GNUTYPE_SPARSE), True, id="sparse"),
        pytest.param(_special(f"{_ROOT}/odd", b"Z"), False, id="unknown-type"),
        pytest.param(_file("b.py", b"x", root="another-root"), False, id="second-root"),
        pytest.param(_file("a.py", b"again"), False, id="duplicate-path"),
        pytest.param(_file("a" * 70_000 + ".py", b"x"), False, id="metadata-header-over-64-kib"),
        # A ustar name ends at its first NUL; a pax record carries one through.
        pytest.param(
            _special(f"{_ROOT}/x.py", tarfile.REGTYPE, pax_headers={"path": f"{_ROOT}/a\0b.py"}),
            False,
            id="nul-in-a-pax-path",
        ),
        pytest.param(
            _special(f"{_ROOT}/x.py", tarfile.SYMTYPE, pax_headers={"linkpath": "a\0b.py"}),
            False,
            id="nul-in-a-pax-link-path",
        ),
    ],
)
def test_an_unsafe_entry_stops_both_passes(member: Member, gnu: bool) -> None:
    first = _file("a.py", b"a")
    archive = _archive(first, gnu=[member]) if gnu else _archive(first, member)
    reader = _reader()

    with pytest.raises(ArchiveUnsafe) as caught:
        _scan(archive, reader)
    assert caught.value.code == "archive_unsafe"
    with pytest.raises(ArchiveUnsafe):
        _contents(archive, {"a.py"}, reader=reader)


def test_every_header_counts_toward_the_entries_limit_pax_headers_included() -> None:
    # A path over 100 characters gets its own pax header: 1 global + 1 root + 2 headers.
    archive = _archive(_file("p" * 120 + ".py", b"x"))

    assert _scan(archive, _reader(archive_entries=4)).counters.headers == 4
    with pytest.raises(IndexLimitExceeded) as caught:
        _scan(archive, _reader(archive_entries=3))
    assert caught.value.code == "limit_exceeded:archive_entries"
    with pytest.raises(IndexLimitExceeded):
        _contents(archive, set(), reader=_reader(archive_entries=3))


def test_the_unpacked_bytes_limit_counts_declared_sizes_before_reading_them() -> None:
    archive = _archive(_file("a.py", b"a" * 100), _file("b.py", b"b" * 100))
    limit = _GLOBAL_HEADER_BYTES + 200

    assert _scan(archive, _reader(unpacked_bytes=limit)).counters.unpacked_bytes == limit
    with pytest.raises(IndexLimitExceeded) as caught:
        _scan(archive, _reader(unpacked_bytes=limit - 1))
    assert caught.value.code == "limit_exceeded:unpacked_bytes"


_DATA = b"x" * 100_000
# A base-256 size field that `tarfile` decodes as -(2**40).
_NEGATIVE_SIZE = b"\xff" + (2**88 - 2**40).to_bytes(11, "big")


@pytest.mark.parametrize(
    ("archive", "limit"),
    [
        pytest.param(
            _raw_archive(_raw_pax("size", "100000"), _raw_header(f"{_ROOT}/a.py"), _padded(_DATA)),
            10_000,
            id="pax-size-over-a-zero-header-size",
        ),
        pytest.param(
            _raw_archive(
                _raw_header(f"{_ROOT}/d", tarfile.DIRTYPE, _NEGATIVE_SIZE),
                _raw_header(f"{_ROOT}/a.py", size=len(_DATA)),
                _padded(_DATA),
            ),
            10_000,
            id="negative-size-before-a-file",
        ),
        pytest.param(
            _raw_archive(
                _raw_header(f"{_ROOT}/s.py", tarfile.GNUTYPE_SPARSE, extended=True),
                _sparse_extension(extended=True) * 999,
                _sparse_extension(extended=False),
            ),
            1_000,
            id="gnu-sparse-map-of-1000-blocks",
        ),
    ],
)
def test_the_unpacked_bytes_limit_counts_the_bytes_read_when_a_header_hides_its_size(
    archive: SourceArchive, limit: int
) -> None:
    reader = _reader(unpacked_bytes=limit)

    with pytest.raises(IndexLimitExceeded) as caught:
        _scan(archive, reader)
    assert caught.value.code == "limit_exceeded:unpacked_bytes"
    with pytest.raises(IndexLimitExceeded):
        _contents(archive, {"a.py"}, reader=reader)


def test_a_pass_keeps_only_the_current_header(monkeypatch: pytest.MonkeyPatch) -> None:
    # Stream mode still lists every header in `TarFile.members`, each with its own copy of
    # the global pax headers; directories are never yielded, so they must not pile up.
    listed: list[int] = []
    next_header = tarfile.TarFile.next

    def counting_next(archive: tarfile.TarFile) -> tarfile.TarInfo | None:
        member = next_header(archive)
        listed.append(len(archive.members))  # type: ignore[attr-defined]
        return member

    monkeypatch.setattr(tarfile.TarFile, "next", counting_next)
    archive = _archive(
        *(_directory(f"d{index}") for index in range(200)),
        *(_file(f"m{index}.py", b"x") for index in range(200)),
    )

    _scan(archive)
    _contents(archive, {"m1.py"})

    # Both passes went through `next` for every member.
    assert len(listed) > 2 * 400
    assert max(listed) == 1


def test_many_small_files_are_listed_and_read_in_batches_in_archive_order() -> None:
    members = [_file(f"src/m{index:04}.py", f"x = {index}\n".encode()) for index in range(3_000)]
    archive = _archive(*members)

    scan = _scan(archive)
    batches = _contents(archive, {"src/m0001.py", "src/m1500.py", "src/m2999.py"}, batch_files=2)

    assert len(scan.entries) == 3_000
    assert scan.counters.headers == 3_002
    assert batches == [
        (("src/m0001.py", b"x = 1\n"), ("src/m1500.py", b"x = 1500\n")),
        (("src/m2999.py", b"x = 2999\n"),),
    ]


def test_contents_never_returns_a_symlink_or_an_unlisted_path() -> None:
    archive = _archive(_file("a.py", b"a"), _symlink("b.py", "a.py"), _file("c.py", b"c"))

    assert _contents(archive, {"b.py", "c.py", "missing.py"}) == [(("c.py", b"c"),)]


def test_a_large_archive_of_excluded_paths_streams_without_reading_them() -> None:
    # 64 files of incompressible data over the read limit, as under `node_modules/`.
    big = [_file(f"node_modules/pkg{index}/bundle.js", os.urandom(_MIB + 1)) for index in range(64)]
    archive = _archive(*big, _file("src/app.py", b"print(1)\n"))
    assert len(archive.content) > 64 * _MIB

    scan = _scan(archive)

    assert scan.entries["node_modules/pkg0/bundle.js"] == ArchiveEntry(
        "node_modules/pkg0/bundle.js", _MIB + 1
    )
    assert scan.counters.headers == 67
    assert _contents(archive, {"src/app.py"}) == [(("src/app.py", b"print(1)\n"),)]


def test_a_passed_deadline_stops_both_passes() -> None:
    archive = _archive(_file("a.py", b"a"))
    past = time.monotonic() - 1

    with pytest.raises(IndexLimitExceeded) as caught:
        _scan(archive, deadline=past)
    assert caught.value.code == "limit_exceeded:deadline"
    with pytest.raises(IndexLimitExceeded):
        _contents(archive, {"a.py"}, deadline=past)


def test_the_deadline_stops_a_pass_over_a_large_archive_in_time() -> None:
    # 512 MiB unpacked: the whole pass takes many times the deadline.
    zeros = b"\0" * (8 * _MIB)
    archive = _archive(*(_file(f"blob{index}.bin", zeros) for index in range(64)))
    started = time.monotonic()
    _scan(archive)
    whole = time.monotonic() - started
    started = time.monotonic()

    with pytest.raises(IndexLimitExceeded) as caught:
        _scan(archive, deadline=started + 0.05)

    assert caught.value.code == "limit_exceeded:deadline"
    # Checked at every header and read, not once at the end of the pass.
    assert time.monotonic() - started < whole / 4


@pytest.mark.parametrize(
    "content",
    [
        pytest.param(b"not a gzip stream", id="not-gzip"),
        pytest.param(b"", id="empty"),
    ],
)
def test_an_unreadable_archive_is_a_transient_vcs_failure(content: bytes) -> None:
    with pytest.raises(VcsUnavailable) as caught:
        _scan(SourceArchive(_COMMIT, content))
    assert caught.value.code == "vcs_unavailable"


def test_a_truncated_archive_is_a_transient_vcs_failure() -> None:
    members = [_file(f"m{index}.py", os.urandom(3_000)) for index in range(50)]
    content = _archive(*members).content
    truncated = SourceArchive(_COMMIT, content[: len(content) // 2])

    with pytest.raises(VcsUnavailable):
        _scan(truncated)
    with pytest.raises(VcsUnavailable):
        _contents(truncated, {"m1.py"})


@pytest.mark.parametrize(
    "archive",
    [
        # `ValueError` from `int()` of the pax record.
        pytest.param(
            _raw_archive(_raw_pax("GNU.sparse.size", "abc"), _raw_header(f"{_ROOT}/a.py")),
            id="pax-number-that-is-not-a-number",
        ),
        # `IndexError`: the map continues past the end of the stream.
        pytest.param(
            _raw_archive(
                _raw_header(f"{_ROOT}/s.py", tarfile.GNUTYPE_SPARSE, extended=True), end=False
            ),
            id="gnu-sparse-map-cut-at-the-end",
        ),
        # `RecursionError`: `tarfile` recurses once per metadata header.
        pytest.param(
            _raw_archive(_raw_pax("comment", "x") * 1_500, _raw_header(f"{_ROOT}/a.py")),
            id="1500-chained-pax-headers",
        ),
        pytest.param(
            _raw_archive(_raw_long_name() * 1_500, _raw_header(f"{_ROOT}/a.py")),
            id="1500-chained-gnu-long-names",
        ),
    ],
)
def test_a_forged_header_tarfile_cannot_parse_is_a_transient_vcs_failure(
    archive: SourceArchive,
) -> None:
    with pytest.raises(VcsUnavailable) as caught:
        _scan(archive)
    assert caught.value.code == "vcs_unavailable"
    with pytest.raises(VcsUnavailable):
        _contents(archive, {"a.py"})


def test_a_non_utf8_name_stays_in_the_path_set() -> None:
    # The exclusions classify such a path as unsupported; the archive does not decide.
    name = b"caf\xe9.py".decode("utf-8", "surrogateescape")
    archive = _archive(_file(name, b"x"))

    assert set(_scan(archive).entries) == {name}
