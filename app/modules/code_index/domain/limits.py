"""Limits of one index build (ADR 0001 §7, #142).

Exceeding a counted limit fails the index with `limit_exceeded:<field>` (`deadline` for
`deadline_seconds`); a file over `read_bytes` or `parse_bytes` is only skipped and counted.
The storage quotas and the concurrency limits are admitted at the claim
(`IndexAdmission`), not here.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.modules.reviews.application.ast_symbols import MAX_SOURCE_BYTES

_MIB = 1_048_576


@dataclass(frozen=True)
class IndexLimits:
    download_bytes: int = 100 * _MIB
    unpacked_bytes: int = 1024 * _MIB
    # Every header counts, pax headers included.
    archive_entries: int = 100_000
    # A larger file is skipped before its content is read.
    read_bytes: int = _MIB
    # A larger file is skipped unparsed: the extractor profile is the review's.
    parse_bytes: int = MAX_SOURCE_BYTES
    parsed_files: int = 20_000
    symbols: int = 500_000
    edges: int = 2_000_000
    deadline_seconds: int = 900
    # More changed files, or more importers to re-extract, make the build full.
    incremental_files: int = 500
