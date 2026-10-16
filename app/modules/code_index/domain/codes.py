"""Error codes of a `failed` index (ADR 0001 §7, #142).

Only exceeding a limit or an unsafe archive entry fails an index; any other terminal
failure releases the lease with `last_error_code` and keeps the last publication
(ADR §6.1).
"""

from __future__ import annotations

REPOSITORY_STORAGE_EXCEEDED = "limit_exceeded:repository_storage"
WORKSPACE_STORAGE_EXCEEDED = "limit_exceeded:workspace_storage"

FAILED_ERROR_CODES = frozenset(
    {
        "archive_unsafe",
        "limit_exceeded:download_bytes",
        "limit_exceeded:unpacked_bytes",
        "limit_exceeded:archive_entries",
        "limit_exceeded:parsed_files",
        "limit_exceeded:symbols",
        "limit_exceeded:edges",
        "limit_exceeded:deadline",
        REPOSITORY_STORAGE_EXCEEDED,
        WORKSPACE_STORAGE_EXCEEDED,
    }
)
