"""Frozen graph-index snapshot of a #146 eval case (``index.json``) and its retriever.

The snapshot is what the production indexer would store for the case's ``base/`` tree: the
builder runs the real ``BuildRepositoryIndex`` use case, with the real ``TarArchiveReader``
and ``SubprocessSourceParser`` and the production limits and versions, over in-memory
ports (a store that keeps what ``replace_file`` receives, a source that serves ``base/``
as a tar archive). Nothing of the indexer is re-implemented, so the exclusions, the
resolver and the row builders cannot drift from the app; a changed extractor shows as a
changed ``index.json`` in ``build_rag_snapshot.py --check``.

``FrozenGraphRetriever`` is the ``GraphRetriever`` over a loaded snapshot: the same hints in
the same order as ``SqlAlchemyGraphRetriever`` over the stored edges.
"""

from __future__ import annotations

import asyncio
import gzip
import io
import json
import re
import tarfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Any, Self, cast
from uuid import UUID

from app.bootstrap.code_index_worker import CodeIndexSettings, index_versions
from app.modules.code_index.application.build_index import (
    BuildRepositoryIndex,
    BuildStatus,
)
from app.modules.code_index.application.exclusions import INDEX_EXCLUSIONS_VERSION
from app.modules.code_index.application.index_store import (
    CodeIndexStore,
    IndexAdmission,
    IndexBuildStats,
    IndexedEdge,
    IndexedFile,
    IndexFence,
    IndexState,
    IndexStorage,
    LeaseClaim,
)
from app.modules.code_index.application.ports import IndexedRepository
from app.modules.code_index.application.vcs import (
    BranchHead,
    Comparison,
    IndexLimitExceeded,
    SourceArchive,
)
from app.modules.code_index.domain.enums import EdgeKind, IndexStatus
from app.modules.code_index.domain.rag_index import rag_index
from app.modules.code_index.domain.storage import row_bytes
from app.modules.code_index.infrastructure.tar_archive import TarArchiveReader
from app.modules.reviews.application.graph_retriever import GraphHint, GraphHints, GraphTarget
from app.modules.reviews.application.policy_documents import missing_document, normalize_document
from app.modules.reviews.application.policy_snapshot import RULES_DOCUMENT_PATH, PolicyDocument
from app.modules.reviews.infrastructure.subprocess_source_parser import SubprocessSourceParser
from review.scripts.eval_rag_source import REPOSITORY_ID, commit_id, git_blob_oid, read_tree

SCHEMA_VERSION = 1
INDEX_FILE = "index.json"
# The production defaults (``CodeIndexSettings`` reads the environment only through
# ``from_environment``, which the eval never calls).
SETTINGS = CodeIndexSettings()

_COMMIT = re.compile(r"[0-9a-f]{40}")
_ARCHIVE_ROOT = "repository"
_GENERATION = 1
_FENCE = IndexFence(UUID(int=1460), _GENERATION, 1)
_EPOCH = datetime(2026, 1, 1, tzinfo=UTC)


class SnapshotError(ValueError):
    """The index of a case cannot be built, or an ``index.json`` is not a valid snapshot."""


@dataclass(frozen=True)
class SnapshotFile:
    path: str
    blob_sha: str


@dataclass(frozen=True)
class Extractor:
    """The production versions the rows were built with."""

    limits_version: str
    parser_profile: str
    exclusions_version: str


@dataclass(frozen=True)
class IndexSnapshot:
    """What the store's visible rows hold for one case, without the symbol rows the
    retriever does not read."""

    schema_version: int
    indexed_commit_sha: str
    extractor: Extractor
    file_count: int
    symbol_count: int
    files: tuple[SnapshotFile, ...]
    edges: tuple[IndexedEdge, ...]


# --- the builder: the production use case over in-memory ports -----------------------------


def _archive(tree: Mapping[str, bytes]) -> bytes:
    """The gzip tar archive a provider serves: one root directory and the files below it."""
    buffer = io.BytesIO()
    with (
        gzip.GzipFile(fileobj=buffer, mode="wb", mtime=0) as stream,
        tarfile.open(fileobj=stream, mode="w") as archive,
    ):
        root = tarfile.TarInfo(_ARCHIVE_ROOT)
        root.type = tarfile.DIRTYPE
        root.mode = 0o755
        archive.addfile(root)
        for path, raw in sorted(tree.items()):
            member = tarfile.TarInfo(f"{_ARCHIVE_ROOT}/{path}")
            member.size = len(raw)
            member.mode = 0o644
            archive.addfile(member, io.BytesIO(raw))
    return buffer.getvalue()


class _Source:
    """``RepositorySnapshotSource`` of one tree at one commit; no request leaves the process."""

    request_count = 0

    def __init__(self, tree: Mapping[str, bytes], commit_sha: str) -> None:
        self._tree = tree
        self._commit_sha = commit_sha

    async def head(self) -> BranchHead:
        return BranchHead("main", self._commit_sha)

    async def compare(self, base_sha: str, head_sha: str) -> Comparison | None:
        raise AssertionError("a first build is full: it never compares")

    async def rules_document(self, commit_sha: str) -> PolicyDocument:
        raw = self._tree.get(RULES_DOCUMENT_PATH)
        if raw is None:
            return missing_document(RULES_DOCUMENT_PATH)
        return normalize_document(
            RULES_DOCUMENT_PATH, raw, blob_oid=git_blob_oid(raw), policy_base_sha=commit_sha
        )

    async def archive(self, commit_sha: str, *, max_bytes: int) -> SourceArchive:
        content = _archive(self._tree)
        if len(content) > max_bytes:
            raise IndexLimitExceeded("download_bytes")
        return SourceArchive(commit_sha, content)


def _file_bytes(file: IndexedFile) -> int:
    """The estimated bytes of the rows of ``file``, by the formula the quotas count with."""
    total = row_bytes("code_index_files", {"path": file.path, "blob_sha": file.blob_sha})
    for symbol in file.symbols:
        key = symbol.key
        total += row_bytes(
            "code_index_symbols", {"path": key.path, "qualified_name": key.qualified_name}
        )
    for edge in file.edges:
        total += row_bytes(
            "code_index_edges",
            {"path": edge.path, "target_path": edge.target_path, "target_name": edge.target_name},
        )
    for record in file.imports:
        total += row_bytes(
            "code_index_imports",
            {
                "path": record.path,
                "specifier": record.specifier,
                "target_path": record.target_path,
            },
        )
    return total


class _Store:
    """The part of ``CodeIndexStore`` a first full build calls; it keeps the rows written.

    Methods of an incremental build (``iter_files``, ``visible_counts``, ...) are absent on
    purpose: a build that reaches for them is not the first full one and must fail loudly.
    """

    def __init__(self) -> None:
        self.files: list[IndexedFile] = []
        self.pending = 0
        self.published = False
        self.failure: str | None = None

    async def get_state(self, repository_id: UUID) -> IndexState:
        return IndexState(
            generation=_GENERATION,
            status=IndexStatus.BUILDING,
            purge_reason=None,
            error_code=None,
            last_error_code=None,
            indexed_commit_sha=None,
            target_commit_sha=None,
            reserved_publication=_FENCE.publication,
            published_publication=0,
            floor_publication=0,
            published_at=None,
            last_used_at=None,
            lease_expires_at=None,
            file_count=0,
            symbol_count=0,
            edge_count=0,
            stored_bytes=0,
            pending_bytes=0,
            profile=None,
            stats=IndexBuildStats(0, 0, 0),
            updated_at=_EPOCH,
        )

    async def claim_lease(
        self, repository_id: UUID, generation: int, lease_seconds: int, admission: IndexAdmission
    ) -> LeaseClaim:
        return LeaseClaim(_FENCE, None)

    async def discard_abandoned(self, repository_id: UUID, fence: IndexFence, limit: int) -> int:
        return 0

    async def replace_file(
        self, repository_id: UUID, fence: IndexFence, file: IndexedFile
    ) -> IndexStorage:
        self.files.append(file)
        self.pending += _file_bytes(file)
        # No other repository and no earlier publication: the attempt's rows are all there is.
        return IndexStorage(0, self.pending, self.pending)

    async def publish(self, repository_id: UUID, fence: IndexFence, **values: Any) -> bool:
        self.published = True
        return True

    async def mark_failed(
        self,
        repository_id: UUID,
        fence: IndexFence,
        error_code: str,
        *,
        stats: IndexBuildStats | None = None,
    ) -> None:
        self.failure = error_code

    async def release(
        self,
        repository_id: UUID,
        fence: IndexFence,
        *,
        last_error_code: str | None = None,
        stats: IndexBuildStats | None = None,
    ) -> IndexStatus:
        self.failure = last_error_code or self.failure
        return IndexStatus.BUILDING


class _UnitOfWork:
    def __init__(self, store: _Store) -> None:
        self._store = store

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None

    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        return None

    @property
    def index(self) -> CodeIndexStore:
        # The fake implements only the methods a first full build calls.
        return cast(CodeIndexStore, self._store)


class _Repositories:
    async def get(self, repository_id: UUID) -> IndexedRepository:
        return IndexedRepository(repository_id, "github", 0, "eval/rag")


class _Wakeups:
    async def wake(self, repository_id: UUID, generation: int) -> None:
        return None


async def build_snapshot(tree: Mapping[str, bytes], *, indexed_commit_sha: str) -> IndexSnapshot:
    """The graph index of ``tree`` as ``BuildRepositoryIndex`` stores it at ``indexed_commit_sha``.

    Raises ``SnapshotError`` unless the job publishes, so a limit or an unsafe tree never
    yields a partial snapshot.
    """
    limits = SETTINGS.limits
    versions = index_versions(limits)
    store = _Store()
    source = _Source(tree, indexed_commit_sha)
    parser = SubprocessSourceParser(attempt_cpu_seconds=None)
    use_case = BuildRepositoryIndex(
        uow=lambda: _UnitOfWork(store),
        repositories=_Repositories(),
        sources=lambda repository: source,
        archives=TarArchiveReader(limits),
        parser=parser,
        wakeups=_Wakeups(),
        versions=versions,
        limits=limits,
        settings=SETTINGS.build,
    )
    try:
        outcome = await use_case.execute(REPOSITORY_ID, _GENERATION)
    finally:
        await parser.aclose()
    if outcome.status is not BuildStatus.PUBLISHED or not store.published:
        raise SnapshotError(
            f"the index build ended as {outcome.status.value}"
            f" ({outcome.error_code or store.failure or 'no error code'})"
        )
    files = sorted(store.files, key=lambda file: file.path)
    edges = sorted(
        {edge for file in files for edge in file.edges},
        key=_edge_order,
    )
    return IndexSnapshot(
        schema_version=SCHEMA_VERSION,
        indexed_commit_sha=indexed_commit_sha,
        extractor=Extractor(
            limits_version=versions.limits_profile_version,
            parser_profile=versions.parser_profile_version,
            exclusions_version=INDEX_EXCLUSIONS_VERSION,
        ),
        file_count=len(files),
        symbol_count=sum(len(file.symbols) for file in files),
        files=tuple(SnapshotFile(file.path, file.blob_sha) for file in files),
        edges=tuple(edges),
    )


def build_case_snapshot(case_dir: Path) -> IndexSnapshot:
    """The snapshot of ``case_dir/base`` at the synthetic base commit of the case id."""
    return asyncio.run(
        build_snapshot(
            read_tree(case_dir / "base"), indexed_commit_sha=commit_id(case_dir.name, "base")
        )
    )


# --- the bytes -----------------------------------------------------------------------------


def render_snapshot(snapshot: IndexSnapshot) -> bytes:
    """Deterministic bytes of ``index.json``: fixed key order, LF, one trailing newline."""
    document = {
        "schema_version": snapshot.schema_version,
        "indexed_commit_sha": snapshot.indexed_commit_sha,
        "extractor": {
            "limits_version": snapshot.extractor.limits_version,
            "parser_profile": snapshot.extractor.parser_profile,
            "exclusions_version": snapshot.extractor.exclusions_version,
        },
        "file_count": snapshot.file_count,
        "symbol_count": snapshot.symbol_count,
        "files": [{"path": file.path, "blob_sha": file.blob_sha} for file in snapshot.files],
        "edges": [
            {
                "path": edge.path,
                "kind": edge.kind.value,
                "target_path": edge.target_path,
                "target_name": edge.target_name,
            }
            for edge in snapshot.edges
        ],
    }
    return (json.dumps(document, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def _count(value: object, name: str) -> int:
    if type(value) is not int or value < 0:
        raise SnapshotError(f"index.json {name} is not a non-negative integer")
    return value


def _edge_order(edge: IndexedEdge) -> tuple[str, str, str, str]:
    """The order the store reads edges in, and so the order of the hints."""
    return edge.path, edge.kind.value, edge.target_path, edge.target_name


def parse_snapshot(raw: bytes) -> IndexSnapshot:
    """The snapshot of an ``index.json``.

    Refused: another schema version, a count that is not an integer or does not match the
    rows, files or edges out of order, a repeated edge, an edge from a file the snapshot
    does not hold. The hint order is the stored edge order, so it must be the sorted one.
    """
    try:
        document = json.loads(raw)
        if (
            type(document["schema_version"]) is not int
            or document["schema_version"] != SCHEMA_VERSION
        ):
            raise SnapshotError(f"index.json has schema_version {document['schema_version']!r}")
        commit = document["indexed_commit_sha"]
        if not isinstance(commit, str) or not _COMMIT.fullmatch(commit):
            raise SnapshotError("index.json indexed_commit_sha is not a 40-hex commit id")
        extractor = Extractor(
            document["extractor"]["limits_version"],
            document["extractor"]["parser_profile"],
            document["extractor"]["exclusions_version"],
        )
        files = tuple(SnapshotFile(item["path"], item["blob_sha"]) for item in document["files"])
        edges = tuple(
            IndexedEdge(
                item["path"], EdgeKind(item["kind"]), item["target_path"], item["target_name"]
            )
            for item in document["edges"]
        )
        file_count = _count(document["file_count"], "file_count")
        symbol_count = _count(document["symbol_count"], "symbol_count")
    except SnapshotError:
        raise
    except (KeyError, TypeError, ValueError) as error:
        raise SnapshotError(f"index.json is not a snapshot: {error!r}") from error
    if file_count != len(files):
        raise SnapshotError("index.json file_count does not match its files")
    paths = [file.path for file in files]
    if paths != sorted(set(paths)):
        raise SnapshotError("index.json files are not unique and sorted by path")
    keys = [_edge_order(edge) for edge in edges]
    if keys != sorted(set(keys)):
        raise SnapshotError("index.json edges are not unique and sorted as the store reads them")
    unknown = {edge.path for edge in edges} - set(paths)
    if unknown:
        raise SnapshotError(f"index.json has edges from files it does not hold: {sorted(unknown)}")
    return IndexSnapshot(
        schema_version=SCHEMA_VERSION,
        indexed_commit_sha=commit,
        extractor=extractor,
        file_count=file_count,
        symbol_count=symbol_count,
        files=files,
        edges=edges,
    )


# --- the retriever -------------------------------------------------------------------------


class FrozenGraphRetriever:
    """``GraphRetriever`` over a snapshot: the incoming edges of each asked target.

    The contract of ``SqlAlchemyGraphRetriever``: a repeated target is read once, the hints
    follow the order of the targets and, inside one target, the order of the stored edges.
    Inside one target only the importer ``path`` varies, so the order is the database's
    ordering of that column: PostgreSQL sorts it by the collation of the database, and this
    snapshot by code point. The two agree on the byte-order collation of the
    ``postgres:17-alpine`` image every database of the project runs on (the PG parity test
    pins it); a glibc ``en_US`` database would put ``pkg/__init__.py`` after ``pkg/api.py``.

    Not representable: the index is always READY at the snapshot's commit, with no
    ``indexed_at``. The SQL retriever's STALE or EMPTY index (``GraphHints(index, ())``)
    does not exist here, so a caller must not rely on a stale-index check against it.
    """

    def __init__(self, snapshot: IndexSnapshot) -> None:
        incoming: dict[tuple[str, str], list[GraphHint]] = {}
        for edge in snapshot.edges:
            incoming.setdefault((edge.target_path, edge.target_name), []).append(
                GraphHint(edge.path, edge.kind, edge.target_path, edge.target_name)
            )
        self._incoming = {key: tuple(hints) for key, hints in incoming.items()}
        self._index = rag_index(
            rag_enabled=True,
            status=IndexStatus.READY,
            indexed_commit_sha=snapshot.indexed_commit_sha,
            target_commit_sha=snapshot.indexed_commit_sha,
            published_at=None,
            file_count=snapshot.file_count,
            symbol_count=snapshot.symbol_count,
            error_code=None,
            last_error_code=None,
        )

    async def read_hints(self, repository_id: UUID, targets: Sequence[GraphTarget]) -> GraphHints:
        hints: list[GraphHint] = []
        for target in dict.fromkeys(targets):
            hints.extend(self._incoming.get((target.path, target.name), ()))
        return GraphHints(self._index, tuple(hints))
