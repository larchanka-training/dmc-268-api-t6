"""The gzip tar archive of one commit, read in memory and off the event loop (#142).

ADR 0001 §7: the archive is never written to disk. Every header counts toward the entries
limit, pax and GNU long-name headers included, and the sizes they declare count toward
the unpacked-bytes limit before any of those bytes is read. The single root directory of
a provider archive is stripped. An absolute path, a `..` segment, a hardlink, a device, a
FIFO, a sparse file, any other entry type, a second root, a repeated path or a metadata
header over 64 KiB is unsafe. A symlink joins the path set but is never read.

The gzip stream is decoded by `gzip`, not by `tarfile`: a truncated stream then fails
instead of ending the listing early. An archive that cannot be read is a transient
`VcsUnavailable`.
Each pass runs in `asyncio.to_thread` and checks the monotonic deadline at every header
and every read, so a pass over the whole unpacked limit stops in time.
"""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import io
import tarfile
import time
import zlib
from collections.abc import AsyncIterator, Iterator, Set
from contextlib import ExitStack

from app.modules.code_index.application.vcs import (
    ArchiveCounters,
    ArchiveEntry,
    ArchiveScan,
    ArchiveUnsafe,
    IndexLimitExceeded,
    SourceArchive,
    VcsUnavailable,
)
from app.modules.code_index.domain.limits import IndexLimits

_METADATA_TYPES = frozenset(
    {
        tarfile.XHDTYPE,
        tarfile.XGLTYPE,
        tarfile.SOLARIS_XHDTYPE,
        tarfile.GNUTYPE_LONGNAME,
        tarfile.GNUTYPE_LONGLINK,
    }
)
# `tarfile` reads a metadata header whole, so its size bounds the memory it takes; a
# provider writes a path and a few times there, far below this.
_MAX_METADATA_BYTES = 65_536
_FILE_TYPES = frozenset({tarfile.REGTYPE, tarfile.AREGTYPE, tarfile.CONTTYPE})
_SAFE_TYPES = _FILE_TYPES | {tarfile.DIRTYPE, tarfile.SYMTYPE}
# A broken gzip or tar stream; `gzip.BadGzipFile` is an `OSError`.
_UNREADABLE = (tarfile.TarError, EOFError, OSError, zlib.error)


class _Budget:
    """The limits of one pass; `_Header` reports every header to it."""

    def __init__(self, limits: IndexLimits, deadline: float) -> None:
        self._limits = limits
        self._deadline = deadline
        self.headers = 0
        self.unpacked_bytes = 0

    def check_deadline(self) -> None:
        if time.monotonic() > self._deadline:
            raise IndexLimitExceeded("deadline")

    def header(self, header: tarfile.TarInfo) -> None:
        self.check_deadline()
        self.headers += 1
        if self.headers > self._limits.archive_entries:
            raise IndexLimitExceeded("archive_entries")
        if header.type in _METADATA_TYPES and header.size > _MAX_METADATA_BYTES:
            raise ArchiveUnsafe("An archive metadata header is too large")
        self.unpacked_bytes += header.size
        if self.unpacked_bytes > self._limits.unpacked_bytes:
            raise IndexLimitExceeded("unpacked_bytes")


class _Counted(io.RawIOBase):
    """The decoded stream, with a deadline check on every read."""

    def __init__(self, stream: gzip.GzipFile, budget: _Budget) -> None:
        self._stream = stream
        self._budget = budget

    def readable(self) -> bool:
        return True

    def read(self, size: int = -1) -> bytes:
        self._budget.check_deadline()
        return self._stream.read(size)


def _open(stack: ExitStack, content: bytes, budget: _Budget) -> tarfile.TarFile:
    class _Header(tarfile.TarInfo):
        # CPython documents `_proc_member` as the hook a subclass overrides; it runs once
        # for every header, those `next()` consumes internally included.
        def _proc_member(self, tarfile: tarfile.TarFile) -> tarfile.TarInfo:
            budget.header(self)
            return super()._proc_member(tarfile)  # type: ignore[misc, no-any-return]

    decoded = stack.enter_context(gzip.GzipFile(fileobj=io.BytesIO(content), mode="rb"))
    stream = _Counted(decoded, budget)
    return stack.enter_context(tarfile.open(fileobj=stream, mode="r|", tarinfo=_Header))


def _entries(archive: tarfile.TarFile) -> Iterator[tuple[str, tarfile.TarInfo]]:
    """Every file and symlink with its path below the root, after the safety checks."""
    root: str | None = None
    seen: set[str] = set()
    for member in archive:
        name = member.name
        if name.startswith("/") or ".." in name.split("/"):
            raise ArchiveUnsafe("An archive entry has an absolute or parent path")
        if member.issparse() or member.type not in _SAFE_TYPES:
            raise ArchiveUnsafe("An archive entry is not a file, directory or symlink")
        top, _, path = name.partition("/")
        if root is None:
            root = top
        elif top != root:
            raise ArchiveUnsafe("An archive has more than one root")
        if member.isdir():
            continue
        if not path:
            raise ArchiveUnsafe("An archive root is not a directory")
        if path in seen:
            raise ArchiveUnsafe("An archive repeats a path")
        seen.add(path)
        yield path, member


def _read(archive: tarfile.TarFile, member: tarfile.TarInfo) -> bytes:
    handle = archive.extractfile(member)
    if handle is None:
        raise ArchiveUnsafe("An archive file cannot be read")
    return handle.read()


def _blob_sha(content: bytes) -> str:
    """The git blob id of `content` (`git hash-object`)."""
    digest = hashlib.sha1(b"blob %d\0" % len(content), usedforsecurity=False)
    digest.update(content)
    return digest.hexdigest()


def _unreadable() -> VcsUnavailable:
    return VcsUnavailable("The repository archive cannot be read")


def _scan(
    archive: SourceArchive, documents: Set[str], limits: IndexLimits, deadline: float
) -> ArchiveScan:
    budget = _Budget(limits, deadline)
    entries: dict[str, ArchiveEntry] = {}
    captured: dict[str, bytes] = {}
    try:
        with ExitStack() as stack:
            members = _open(stack, archive.content, budget)
            for path, member in _entries(members):
                if member.issym():
                    entries[path] = ArchiveEntry(path, 0, symlink=True)
                    continue
                blob_sha = None
                if member.size <= limits.read_bytes:
                    content = _read(members, member)
                    blob_sha = _blob_sha(content)
                    if path in documents:
                        captured[path] = content
                entries[path] = ArchiveEntry(path, member.size, blob_sha=blob_sha)
    except _UNREADABLE:
        raise _unreadable() from None
    return ArchiveScan(
        archive.commit_sha,
        entries,
        captured,
        ArchiveCounters(budget.headers, budget.unpacked_bytes),
    )


def _batches(
    archive: SourceArchive,
    paths: frozenset[str],
    limits: IndexLimits,
    deadline: float,
    batch_files: int,
) -> Iterator[tuple[tuple[str, bytes], ...]]:
    budget = _Budget(limits, deadline)
    try:
        with ExitStack() as stack:
            members = _open(stack, archive.content, budget)
            batch: list[tuple[str, bytes]] = []
            for path, member in _entries(members):
                if member.issym() or path not in paths or member.size > limits.read_bytes:
                    continue
                batch.append((path, _read(members, member)))
                if len(batch) == batch_files:
                    yield tuple(batch)
                    batch = []
            if batch:
                yield tuple(batch)
    except _UNREADABLE:
        raise _unreadable() from None


def _next_batch(
    batches: Iterator[tuple[tuple[str, bytes], ...]],
) -> tuple[tuple[str, bytes], ...] | None:
    return next(batches, None)


class TarArchiveReader:
    """`ArchiveReader` of a gzip tar archive buffered in memory."""

    def __init__(self, limits: IndexLimits) -> None:
        self._limits = limits

    async def scan(
        self, archive: SourceArchive, *, documents: Set[str], deadline: float
    ) -> ArchiveScan:
        return await asyncio.to_thread(_scan, archive, documents, self._limits, deadline)

    async def contents(
        self, archive: SourceArchive, paths: Set[str], *, deadline: float, batch_files: int
    ) -> AsyncIterator[tuple[tuple[str, bytes], ...]]:
        if batch_files < 1:
            raise ValueError("a batch holds at least one file")
        # One pass for the whole iteration: each batch advances the same stream in a
        # worker thread, one batch at a time.
        batches = _batches(archive, frozenset(paths), self._limits, deadline, batch_files)
        while (batch := await asyncio.to_thread(_next_batch, batches)) is not None:
            yield batch
