"""Which files of the archive enter the graph index (ADR 0001 §7, §9; #142).

The classes of `ExclusionClass` are checked in their declaration order and the first
that applies is counted, so a skipped file lands in exactly one counter. Classes 1-7
depend only on the path and on whether the entry is a symlink: they also make an import
target `excluded`. Classes 8-9 read the size of the archive header, class 10 the
content, class 11 the parse result. A change of these rules bumps
`INDEX_EXCLUSIONS_VERSION`.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType

from app.modules.code_index.domain.limits import IndexLimits
from app.modules.reviews.application.ast_symbols import (
    AstFacts,
    AstFailureReason,
    language_for_path,
)
from app.modules.reviews.application.diff_map import is_supported_path
from app.modules.reviews.application.review_ignore import IgnoreMatcher
from app.modules.reviews.application.vcs_diff import is_binary_content, is_generated_path

INDEX_EXCLUSIONS_VERSION = "index-exclusions/v2"

_SECRET_DIRECTORIES = frozenset({".ssh", ".aws", ".gnupg"})
_SECRET_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".jks", ".kdbx")
_SECRET_PREFIXES = (
    *(".env", "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519"),
    *("credentials.", "secret.", "secrets."),
)
# Tool-made directory names keep their case: `pods/` of a Go service is not CocoaPods.
_VENDORED_DIRECTORIES = frozenset(
    {"vendor", "node_modules", "third_party", "bower_components", "site-packages"}
    | {".venv", "venv", "Pods"}
)


class ExclusionClass(StrEnum):
    """Why a file of the archive is not indexed, in the order the checks run."""

    UNSUPPORTED_PATH = "unsupported_path"
    SECRET = "secret"
    VENDORED = "vendored"
    GENERATED = "generated"
    UNSUPPORTED_LANGUAGE = "unsupported_language"
    IGNORED = "ignored"
    SYMLINK = "symlink"
    OVER_READ_LIMIT = "over_read_limit"
    OVER_PARSE_LIMIT = "over_parse_limit"
    BINARY = "binary"
    PARSE_FAILED = "parse_failed"


def _secret_like(path: str) -> bool:
    parts = path.casefold().split("/")
    name = parts[-1]
    return (
        any(part in _SECRET_DIRECTORIES for part in parts[:-1])
        or name.endswith(_SECRET_SUFFIXES)
        or name.startswith(_SECRET_PREFIXES)
    )


def _vendored(path: str) -> bool:
    return any(part in _VENDORED_DIRECTORIES for part in path.split("/")[:-1])


class IndexExclusions:
    """The exclusion order of one build and its counters, one per class.

    `path_class` and `entry_class` only decide; the `admit_*` methods decide and count
    what they exclude. The ignore matcher keeps its run budget across the build: a
    `PolicyError` from it propagates to the caller.
    """

    def __init__(self, matcher: IgnoreMatcher, limits: IndexLimits) -> None:
        self._matcher = matcher
        self._limits = limits
        self._counts = dict.fromkeys(ExclusionClass, 0)

    @property
    def counts(self) -> Mapping[ExclusionClass, int]:
        """Every class with the number of files it excluded so far."""
        return MappingProxyType(self._counts)

    def path_class(self, path: str, *, symlink: bool = False) -> ExclusionClass | None:
        """Classes 1-7, which also exclude `path` as an import target."""
        if not is_supported_path(path):
            return ExclusionClass.UNSUPPORTED_PATH
        if _secret_like(path):
            return ExclusionClass.SECRET
        if _vendored(path):
            return ExclusionClass.VENDORED
        if is_generated_path(path):
            return ExclusionClass.GENERATED
        if language_for_path(path) is None:
            return ExclusionClass.UNSUPPORTED_LANGUAGE
        if self._matcher.match(path) is not None:
            return ExclusionClass.IGNORED
        if symlink:
            return ExclusionClass.SYMLINK
        return None

    def entry_class(self, path: str, *, symlink: bool, size: int) -> ExclusionClass | None:
        """Classes 1-9 of an archive entry of `size` bytes, before its content is read."""
        found = self.path_class(path, symlink=symlink)
        if found is not None:
            return found
        if size > self._limits.read_bytes:
            return ExclusionClass.OVER_READ_LIMIT
        if size > self._limits.parse_bytes:
            return ExclusionClass.OVER_PARSE_LIMIT
        return None

    def admit_entry(self, path: str, *, symlink: bool, size: int) -> ExclusionClass | None:
        return self._count(self.entry_class(path, symlink=symlink, size=size))

    def admit_content(self, content: bytes) -> ExclusionClass | None:
        """Class 10 of the content of an admitted entry."""
        return self._count(ExclusionClass.BINARY if is_binary_content(content) else None)

    def admit_parse(self, result: AstFacts | AstFailureReason) -> ExclusionClass | None:
        """Class 11: a decode failure or any other parse failure of the extractor."""
        failed = isinstance(result, AstFailureReason)
        return self._count(ExclusionClass.PARSE_FAILED if failed else None)

    def _count(self, found: ExclusionClass | None) -> ExclusionClass | None:
        if found is not None:
            self._counts[found] += 1
        return found
