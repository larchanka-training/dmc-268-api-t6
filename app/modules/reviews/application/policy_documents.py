"""Load ``.review/rules.md`` and the root ``AGENTS.md`` of the pinned base revision.

RULES_FORMAT_SPEC §3: a path is resolved in the tree of ``policy_base_sha`` and the blob
is read by its immutable id. A missing document lets the run continue; an unreadable or
invalid one stops it. The text is kept whole: nothing is summarised or cut.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
from typing import Protocol

from app.modules.reviews.application.policy_errors import PolicyError, PolicyErrorCode
from app.modules.reviews.application.policy_snapshot import (
    AGENTS_DOCUMENT_PATH,
    RULES_DOCUMENT_PATH,
    DocumentPresence,
    PolicyDocument,
)

MAX_DOCUMENT_BYTES = 65_536
_BOM = b"\xef\xbb\xbf"
_LFS_POINTER = b"version https://git-lfs.github.com/spec/v1"


class PolicyEntryKind(StrEnum):
    FILE = "file"
    DIRECTORY = "directory"
    SYMLINK = "symlink"
    SUBMODULE = "submodule"


@dataclass(frozen=True)
class PolicyTreeEntry:
    kind: PolicyEntryKind
    oid: str
    size: int | None


class PolicyDocumentSource(Protocol):
    """Reads of the pinned base revision; every failure is a ``PolicyError``.

    A listing must be complete: a source that cannot prove a path is absent raises
    instead of returning a partial mapping.
    """

    async def list_entries(self, tree_oid: str | None) -> Mapping[str, PolicyTreeEntry]:
        """Entries of one tree by name; ``None`` is the root tree of the revision."""
        ...

    async def read_blob(self, oid: str) -> bytes: ...


@dataclass(frozen=True)
class LoadedPolicyDocument:
    """A document with its raw bytes; a missing document has no bytes."""

    document: PolicyDocument
    raw: bytes | None


@dataclass(frozen=True)
class LoadedPolicyDocuments:
    rules: LoadedPolicyDocument
    agents: LoadedPolicyDocument


class LoadPolicyDocuments:
    """Read both policy documents without a database transaction."""

    def __init__(self, source: PolicyDocumentSource, *, policy_base_sha: str) -> None:
        self._source = source
        self._base_sha = policy_base_sha

    async def execute(self) -> LoadedPolicyDocuments:
        root = await self._source.list_entries(None)
        return LoadedPolicyDocuments(
            rules=await self._load(root, RULES_DOCUMENT_PATH),
            agents=await self._load(root, AGENTS_DOCUMENT_PATH),
        )

    async def load_document(self, path: str) -> LoadedPolicyDocument:
        """Read one document only, with the same rules as ``execute``."""
        return await self._load(await self._source.list_entries(None), path)

    async def _load(self, root: Mapping[str, PolicyTreeEntry], path: str) -> LoadedPolicyDocument:
        *directories, name = path.split("/")
        entries = root
        for directory in directories:
            parent = entries.get(directory)
            if parent is None:
                return LoadedPolicyDocument(missing_document(path), None)
            if parent.kind is not PolicyEntryKind.DIRECTORY:
                raise self._unsupported(path)
            entries = await self._source.list_entries(parent.oid)
        entry = entries.get(name)
        if entry is None:
            return LoadedPolicyDocument(missing_document(path), None)
        if entry.kind is not PolicyEntryKind.FILE:
            raise self._unsupported(path)
        if entry.size is not None and entry.size > MAX_DOCUMENT_BYTES:
            raise _too_large(path, self._base_sha, entry.size)
        raw = await self._source.read_blob(entry.oid)
        document = normalize_document(path, raw, blob_oid=entry.oid, policy_base_sha=self._base_sha)
        return LoadedPolicyDocument(document, raw)

    def _unsupported(self, path: str) -> PolicyError:
        return PolicyError(
            PolicyErrorCode.DOCUMENT_TYPE_UNSUPPORTED,
            "The policy document must be a regular Git blob",
            source_path=path,
            policy_base_sha=self._base_sha,
        )


def missing_document(path: str) -> PolicyDocument:
    return PolicyDocument(path, DocumentPresence.MISSING, None, None, None, None, None, None)


def normalize_document(
    path: str, raw: bytes, *, blob_oid: str, policy_base_sha: str | None = None
) -> PolicyDocument:
    """Validate raw bytes and return the present document with its normalized text."""
    if len(raw) > MAX_DOCUMENT_BYTES:
        raise _too_large(path, policy_base_sha, len(raw))
    if raw.startswith(_LFS_POINTER):
        raise PolicyError(
            PolicyErrorCode.DOCUMENT_TYPE_UNSUPPORTED,
            "The policy document must be a regular Git blob, not an LFS pointer",
            source_path=path,
            policy_base_sha=policy_base_sha,
        )
    try:
        text = raw.removeprefix(_BOM).decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise PolicyError(
            PolicyErrorCode.DOCUMENT_DECODE_ERROR,
            "The policy document must be valid UTF-8",
            source_path=path,
            policy_base_sha=policy_base_sha,
        ) from exc
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    for line_number, line in enumerate(text.split("\n"), start=1):
        for column, char in enumerate(line, start=1):
            if (ord(char) < 0x20 and char != "\t") or ord(char) == 0x7F:
                raise PolicyError(
                    PolicyErrorCode.DOCUMENT_CONTROL_CHARACTER,
                    "The policy document contains a forbidden control character",
                    source_path=path,
                    policy_base_sha=policy_base_sha,
                    line=line_number,
                    column=column,
                )
    return PolicyDocument(
        path=path,
        presence=DocumentPresence.PRESENT,
        blob_oid=blob_oid,
        raw_sha256=sha256(raw).hexdigest(),
        text_sha256=sha256(text.encode("utf-8")).hexdigest(),
        text=text,
        byte_count=len(raw),
        line_count=document_line_count(text),
    )


def document_line_count(text: str) -> int:
    """Lines of normalized text; a last line without a newline still counts."""
    return text.count("\n") + (1 if text and not text.endswith("\n") else 0)


def _too_large(path: str, policy_base_sha: str | None, actual: int) -> PolicyError:
    return PolicyError(
        PolicyErrorCode.DOCUMENT_TOO_LARGE,
        f"The policy document exceeds {MAX_DOCUMENT_BYTES} bytes",
        source_path=path,
        policy_base_sha=policy_base_sha,
        limit=MAX_DOCUMENT_BYTES,
        actual=actual,
    )
