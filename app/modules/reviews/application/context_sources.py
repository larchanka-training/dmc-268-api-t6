"""Source reads shared by the review context levels and the RAG slot (#144).

L4 (``review_context``) and the RAG slot (``rag_context``) read a source through the
same checks, load its facts the same way and let the same errors end the run; neither
imports the other for that.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol

from app.modules.reviews.application.ast_resolution import BINARY_SOURCE, FileUnavailable
from app.modules.reviews.application.ast_symbols import (
    MAX_SOURCE_BYTES,
    AstFacts,
    AstFailureReason,
    AstLanguage,
    ParseRequest,
    SourceParser,
    language_for_path,
)
from app.modules.reviews.application.policy_errors import PolicyError
from app.modules.reviews.application.run_failures import RunCancelled, RunFailure
from app.modules.reviews.application.vcs_diff import is_binary_content


def ends_the_run(exc: BaseException) -> bool:
    """Cancellation, a policy error and the run deadline end the run: an optional read
    never takes them for its own failure."""
    return isinstance(exc, (RunCancelled, PolicyError)) or (
        isinstance(exc, RunFailure) and exc.error_code == "deadline_exceeded"
    )


def checked_source(raw: bytes) -> str | FileUnavailable:
    """The size, binary and UTF-8 checks of a source read, in this order, for L4
    (``_Files.text`` in ``review_context``) and RAG alike. The service excludes binary
    content wherever it is found: a file outside the diff is no exception, and the
    check runs before the parser and before the facts cache."""
    if len(raw) > MAX_SOURCE_BYTES:
        return FileUnavailable(AstFailureReason.SOURCE_TOO_LARGE.value)
    if is_binary_content(raw):
        return FileUnavailable(BINARY_SOURCE)
    try:
        return raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return FileUnavailable(AstFailureReason.SOURCE_DECODE_FAILED.value)


class FactsCache(Protocol):
    """Facts by blob, as ``AstFactsCache`` of ``review_context``; a miss or a broken
    cache returns ``None`` and never fails a run."""

    async def get(self, blob_sha: str, language: AstLanguage) -> AstFacts | None: ...

    async def put(self, blob_sha: str, language: AstLanguage, facts: AstFacts) -> None: ...


async def guarded_facts(
    path: str,
    text: Callable[[], Awaitable[str | FileUnavailable]],
    *,
    blob_sha: str | None,
    parser: SourceParser,
    cache: FactsCache | None,
) -> AstFacts | FileUnavailable:
    """The facts of a source, for L4 and RAG alike. ``text`` is the guarded read, and it
    runs before the facts cache: a cached blob is not a permission to show a file."""
    language = language_for_path(path)
    if language is None:
        return FileUnavailable("unsupported_language")
    content = await text()
    if isinstance(content, FileUnavailable):
        return content
    if cache is not None and blob_sha is not None:
        cached = await cache.get(blob_sha, language)
        if cached is not None:
            return cached
    parsed = (await parser.parse_many((ParseRequest(path, language, content.encode()),)))[path]
    if isinstance(parsed, AstFailureReason):
        return FileUnavailable(parsed.value)
    if cache is not None and blob_sha is not None:
        await cache.put(blob_sha, language, parsed)
    return parsed
