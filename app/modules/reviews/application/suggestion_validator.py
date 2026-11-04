"""Application orchestration for commit suggestions validation (#444).

Ensures that code suggestions applied to pull request files do not break syntax
or introduce duplicate control conditions, evaluated in an isolated worker process.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import TYPE_CHECKING
from uuid import UUID

from app.modules.reviews.application.ast_symbols import (
    MAX_SOURCE_BYTES,
    SuggestionParser,
    SuggestionRejectionReason,
    SuggestionValidationRequest,
    SuggestionValidationResult,
    language_for_path,
)
from app.modules.reviews.application.get_run_file_lines import (
    BlobCache,
    BlobCacheKey,
    BlobCacheStatus,
)

if TYPE_CHECKING:
    from app.modules.reviews.application.diff_map import DiffMap
    from app.modules.reviews.application.review_output import ReviewFinding, ReviewOutput

_LOGGER = logging.getLogger(__name__)

__all__ = [
    "SuggestionParser",
    "SuggestionRejectionReason",
    "SuggestionValidationRequest",
    "SuggestionValidationResult",
    "preload_suggestion_blobs",
    "validate_review_suggestions",
]


def _log_retained(
    run_id: UUID | None,
    finding: ReviewFinding,
    reason: SuggestionRejectionReason,
    *,
    exc_info: bool = False,
) -> None:
    _LOGGER.info(
        "Suggestion retained without check run_id=%s path=%r line=%s reason=%s",
        run_id,
        finding.path,
        finding.line,
        reason.value,
        exc_info=exc_info,
    )


def _find_blob_key(diff_map: DiffMap, path: str) -> BlobCacheKey | None:
    file_diff = next(
        (f for f in diff_map.files if f.new_path == path or f.provider_new_path == path),
        None,
    )
    if file_diff is None or file_diff.blob_sha is None or file_diff.omission_reason is not None:
        return None
    return BlobCacheKey(diff_map.repository_id, file_diff.blob_sha)


async def preload_suggestion_blobs(
    output: ReviewOutput,
    *,
    diff_map: DiffMap,
    blob_cache: BlobCache | None,
) -> dict[str, bytes | None]:
    """Load PR head file blobs while the caller's unit of work session is still open."""
    if blob_cache is None:
        return {}
    blobs: dict[str, bytes | None] = {}
    for finding in output.findings:
        if (
            finding.suggestion is None
            or finding.path in blobs
            or language_for_path(finding.path) is None
        ):
            continue
        key = _find_blob_key(diff_map, finding.path)
        if key is None:
            blobs[finding.path] = None
            continue
        try:
            entry = await blob_cache.get(key)
        except Exception:
            _LOGGER.warning(
                "Suggestion blob cache read failed path=%s",
                finding.path,
                exc_info=True,
            )
            blobs[finding.path] = None
            continue
        if entry.status is BlobCacheStatus.HIT and entry.content is not None:
            blobs[finding.path] = entry.content.encode("utf-8")
        else:
            blobs[finding.path] = None
    return blobs


async def _validate_path_suggestions(
    items: list[tuple[int, ReviewFinding]],
    *,
    path: str,
    blobs: Mapping[str, bytes | None],
    parser: SuggestionParser,
    run_id: UUID | None,
    updated_findings: list[ReviewFinding],
) -> None:
    lang = language_for_path(path)
    if lang is None:
        for _, f in items:
            _log_retained(run_id, f, SuggestionRejectionReason.UNSUPPORTED_LANGUAGE)
        return

    content_bytes = blobs.get(path)
    if content_bytes is None:
        for _, f in items:
            _log_retained(run_id, f, SuggestionRejectionReason.FILE_UNAVAILABLE)
        return

    try:
        content_bytes.decode("utf-8")
    except UnicodeDecodeError:
        for _, f in items:
            _log_retained(run_id, f, SuggestionRejectionReason.NON_UTF8)
        return

    if len(content_bytes) > MAX_SOURCE_BYTES:
        for _, f in items:
            _log_retained(run_id, f, SuggestionRejectionReason.FILE_TOO_LARGE)
        return

    requests = tuple(
        SuggestionValidationRequest(
            key=str(idx),
            language=lang,
            source=content_bytes,
            start_line=f.start_line,
            line=f.line,
            suggestion=f.suggestion,
        )
        for idx, f in items
        if f.suggestion is not None
    )

    try:
        results = await parser.validate_suggestions(requests)
    except Exception:
        for _, f in items:
            _log_retained(run_id, f, SuggestionRejectionReason.WORKER_ERROR, exc_info=True)
        return

    for idx, f in items:
        val = results.get(str(idx))
        if val is None:
            _log_retained(run_id, f, SuggestionRejectionReason.WORKER_ERROR)
            continue
        if val.is_valid:
            if val.reason is not None:
                _log_retained(run_id, f, val.reason)
            continue
        reason_code = val.reason.value if val.reason else "invalid"
        _LOGGER.info(
            "Suggestion stripped run_id=%s path=%r line=%s reason=%s detail=%s",
            run_id,
            f.path,
            f.line,
            reason_code,
            val.detail,
        )
        updated_findings[idx] = f.model_copy(update={"suggestion": None})


async def validate_review_suggestions(
    output: ReviewOutput,
    *,
    diff_map: DiffMap,
    parser: SuggestionParser,
    preloaded_blobs: Mapping[str, bytes | None] | None = None,
    run_id: UUID | None = None,
) -> ReviewOutput:
    """Validate all inline suggestions against file content on PR HEAD.

    Invalid suggestions (introducing syntax errors or duplicate control flow)
    are stripped to None while retaining the finding in place.
    Unavailable sources, timeouts, and unsupported languages retain the suggestion
    per AC5 and log a structured reason code.
    """
    if not any(f.suggestion is not None for f in output.findings):
        return output

    blobs = preloaded_blobs if preloaded_blobs is not None else {}

    findings_by_path: dict[str, list[tuple[int, ReviewFinding]]] = {}
    for idx, f in enumerate(output.findings):
        if f.suggestion is not None:
            findings_by_path.setdefault(f.path, []).append((idx, f))

    updated_findings = list(output.findings)
    for path, items in findings_by_path.items():
        await _validate_path_suggestions(
            items,
            path=path,
            blobs=blobs,
            parser=parser,
            run_id=run_id,
            updated_findings=updated_findings,
        )

    return output.model_copy(update={"findings": updated_findings})
