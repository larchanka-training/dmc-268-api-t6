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
from app.modules.reviews.application.review_output import ReviewFinding, ReviewOutput

if TYPE_CHECKING:
    from app.modules.reviews.application.diff_map import DiffMap

_LOGGER = logging.getLogger("app.modules.reviews.suggestion_validator")

__all__ = [
    "SuggestionParser",
    "SuggestionRejectionReason",
    "SuggestionValidationRequest",
    "SuggestionValidationResult",
    "validate_review_suggestions",
]


async def validate_review_suggestions(
    output: ReviewOutput,
    *,
    diff_map: DiffMap | None = None,
    blob_cache: BlobCache | None = None,
    parser: SuggestionParser | None = None,
    run_id: UUID | None = None,
    file_contents: Mapping[str, bytes | str] | None = None,
) -> ReviewOutput:
    """Validate all inline suggestions against file content on PR HEAD.

    Invalid suggestions (introducing syntax errors or duplicate control flow)
    are stripped to None while retaining the finding in place.
    Unavailable sources, timeouts, and unsupported languages retain the suggestion
    per AC5 and log a structured reason code.
    """
    if not any(f.suggestion is not None for f in output.findings):
        return output

    # Group findings with suggestions by path
    findings_by_path: dict[str, list[tuple[int, ReviewFinding]]] = {}
    for idx, f in enumerate(output.findings):
        if f.suggestion is not None:
            findings_by_path.setdefault(f.path, []).append((idx, f))

    updated_findings = list(output.findings)

    for path, items in findings_by_path.items():
        lang = language_for_path(path)
        if lang is None:
            for _, f in items:
                _LOGGER.info(
                    "Suggestion retained without check run_id=%s path=%s line=%s reason=%s",
                    run_id,
                    f.path,
                    f.line,
                    SuggestionRejectionReason.UNSUPPORTED_LANGUAGE.value,
                )
            continue

        content_bytes: bytes | None = None
        if file_contents is not None and path in file_contents:
            raw = file_contents[path]
            content_bytes = raw.encode("utf-8") if isinstance(raw, str) else raw
        elif diff_map is not None and blob_cache is not None:
            file_diff = next(
                (
                    f
                    for f in diff_map.files
                    if f.new_path == path or f.provider_new_path == path or f.old_path == path
                ),
                None,
            )
            if (
                file_diff is not None
                and file_diff.blob_sha is not None
                and file_diff.omission_reason is None
            ):
                key = BlobCacheKey(diff_map.repository_id, file_diff.blob_sha)
                entry = await blob_cache.get(key)
                if entry.status is BlobCacheStatus.HIT and entry.content is not None:
                    content_bytes = entry.content.encode("utf-8")

        if content_bytes is None:
            for _, f in items:
                _LOGGER.info(
                    "Suggestion retained without check run_id=%s path=%s line=%s reason=%s",
                    run_id,
                    f.path,
                    f.line,
                    SuggestionRejectionReason.FILE_UNAVAILABLE.value,
                )
            continue

        try:
            content_bytes.decode("utf-8")
        except UnicodeDecodeError:
            for _, f in items:
                _LOGGER.info(
                    "Suggestion retained without check run_id=%s path=%s line=%s reason=%s",
                    run_id,
                    f.path,
                    f.line,
                    SuggestionRejectionReason.NON_UTF8.value,
                )
            continue

        if len(content_bytes) > MAX_SOURCE_BYTES:
            for _, f in items:
                _LOGGER.info(
                    "Suggestion retained without check run_id=%s path=%s line=%s reason=%s",
                    run_id,
                    f.path,
                    f.line,
                    SuggestionRejectionReason.FILE_TOO_LARGE.value,
                )
            continue

        if parser is None:
            for _, f in items:
                _LOGGER.info(
                    "Suggestion retained without check run_id=%s path=%s line=%s reason=%s",
                    run_id,
                    f.path,
                    f.line,
                    SuggestionRejectionReason.WORKER_ERROR.value,
                )
            continue

        requests = tuple(
            SuggestionValidationRequest(
                key=str(idx),
                language=lang,
                source=content_bytes,
                start_line=f.start_line,
                line=f.line,
                suggestion=f.suggestion,  # guaranteed not None
            )
            for idx, f in items
            if f.suggestion is not None
        )

        try:
            results = await parser.validate_suggestions(requests)
        except Exception:
            for _, f in items:
                _LOGGER.info(
                    "Suggestion retained without check run_id=%s path=%s line=%s reason=%s",
                    run_id,
                    f.path,
                    f.line,
                    SuggestionRejectionReason.WORKER_ERROR.value,
                )
            continue

        for idx, f in items:
            result_key = str(idx)
            val = results.get(result_key)
            if val is None or val.is_valid:
                if val is not None and val.reason is not None:
                    _LOGGER.info(
                        "Suggestion retained without check run_id=%s path=%s line=%s reason=%s",
                        run_id,
                        f.path,
                        f.line,
                        val.reason.value,
                    )
            else:
                reason_code = val.reason.value if val.reason else "invalid"
                _LOGGER.info(
                    "Suggestion stripped run_id=%s path=%s line=%s reason=%s detail=%s",
                    run_id,
                    f.path,
                    f.line,
                    reason_code,
                    val.detail,
                )
                updated_findings[idx] = f.model_copy(update={"suggestion": None})

    return output.model_copy(update={"findings": updated_findings})
