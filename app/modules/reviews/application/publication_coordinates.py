"""Provider-specific inline coordinates derived from publication findings (#171)."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from typing import Any

from app.modules.reviews.application.diff_map import DiffFile, DiffMap, DiffRow, RowKind
from app.modules.reviews.application.review_output import PublishedFinding


def gitlab_line_code(provider_file_path: str, old_cursor: int, new_cursor: int) -> str:
    """GitLab ``line_code`` from provider path and cursors before row increment (§9.5)."""
    digest = hashlib.sha1(provider_file_path.encode("utf-8")).hexdigest()
    return f"{digest}_{old_cursor}_{new_cursor}"


def gitlab_text_position(version: dict[str, str], finding: PublishedFinding) -> dict[str, Any]:
    """Build GitLab merge-request discussion ``position`` for a text diff row."""
    old_path = finding.gitlab_old_path if finding.gitlab_old_path is not None else finding.path
    position: dict[str, Any] = {
        "position_type": "text",
        "base_sha": version["base_commit_sha"],
        "start_sha": version["start_commit_sha"],
        "head_sha": version["head_commit_sha"],
        "old_path": old_path,
        "new_path": finding.path,
    }
    if finding.gitlab_old_line_only:
        position["old_line"] = (
            finding.gitlab_old_line if finding.gitlab_old_line is not None else finding.line
        )
        return position
    position["new_line"] = finding.line
    if finding.gitlab_old_line is not None:
        position["old_line"] = finding.gitlab_old_line
    if finding.start_line is not None:
        start_code = finding.gitlab_line_range_start_code
        end_code = finding.gitlab_line_range_end_code
        if start_code is not None and end_code is not None:
            position["line_range"] = {
                "start": {
                    "line_code": start_code,
                    "type": "new",
                    "new_line": finding.start_line,
                },
                "end": {
                    "line_code": end_code,
                    "type": "new",
                    "new_line": finding.line,
                },
            }
    return position


def _provider_file_path(file: DiffFile) -> str:
    """Non-empty provider ``new_path``, otherwise ``old_path`` (CONTEXT §9.5)."""
    path = file.provider_new_path or file.provider_old_path
    if path is None:
        raise ValueError("diff file has no provider path for GitLab line_code")
    return path


def _file_for_new_path(diff_map: DiffMap, path: str) -> DiffFile | None:
    return next((item for item in diff_map.files if item.new_path == path), None)


def _row_for_new_line(file: DiffFile, line: int) -> DiffRow | None:
    for hunk in file.hunks:
        for row in hunk.rows:
            if row.new_line == line:
                return row
    return None


def _row_for_old_line(file: DiffFile, line: int) -> DiffRow | None:
    for hunk in file.hunks:
        for row in hunk.rows:
            if row.old_line == line:
                return row
    return None


def enrich_gitlab_publication_finding(
    diff_map: DiffMap, finding: PublishedFinding
) -> PublishedFinding:
    """Fill GitLab ``position`` fields from DiffMap rows after anchor validation (D-1)."""
    file = _file_for_new_path(diff_map, finding.path)
    if file is None:
        return finding

    provider_path = _provider_file_path(file)
    updates: dict[str, Any] = {}

    old_provider = file.provider_old_path
    new_provider = file.provider_new_path or file.new_path
    if old_provider and new_provider and old_provider != new_provider:
        updates["gitlab_old_path"] = old_provider

    if finding.gitlab_old_line_only:
        row = _row_for_old_line(file, finding.gitlab_old_line or finding.line)
        if row is not None and finding.gitlab_old_line is None:
            updates["gitlab_old_line"] = row.old_line
        return replace(finding, **updates) if updates else finding

    end_row = _row_for_new_line(file, finding.line)
    if end_row is None:
        return replace(finding, **updates) if updates else finding

    if end_row.kind is RowKind.CONTEXT and end_row.old_line is not None:
        updates["gitlab_old_line"] = end_row.old_line

    if finding.start_line is not None:
        start_row = _row_for_new_line(file, finding.start_line)
        if start_row is not None:
            updates["gitlab_line_range_start_code"] = gitlab_line_code(
                provider_path, start_row.old_cursor, start_row.new_cursor
            )
            updates["gitlab_line_range_end_code"] = gitlab_line_code(
                provider_path, end_row.old_cursor, end_row.new_cursor
            )

    if not updates:
        return finding
    return replace(finding, **updates)
