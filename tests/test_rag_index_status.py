"""The RAG index status readers see, derived from the toggle and the state row (#145)."""

from __future__ import annotations

from dataclasses import fields
from datetime import UTC, datetime
from typing import Any

import pytest

from app.modules.code_index.domain.enums import IndexStatus, RagIndexStatus
from app.modules.code_index.domain.rag_index import RagIndex, rag_index

SHA_A = "a" * 40
SHA_B = "b" * 40
AT = datetime(2026, 10, 16, 9, 30, tzinfo=UTC)
_NO_ROW: dict[str, Any] = {
    "status": None,
    "indexed_commit_sha": None,
    "target_commit_sha": None,
    "published_at": None,
    "file_count": None,
    "symbol_count": None,
    "error_code": None,
    "last_error_code": None,
}


def _ready(**overrides: Any) -> dict[str, Any]:
    """A published state row of an enabled repository, as the outer join reads it."""
    row: dict[str, Any] = {
        "rag_enabled": True,
        "status": IndexStatus.READY,
        "indexed_commit_sha": SHA_A,
        "target_commit_sha": None,
        "published_at": AT,
        "file_count": 12,
        "symbol_count": 340,
        "error_code": None,
        "last_error_code": None,
    }
    return row | overrides


def test_status_values_are_the_adr_list_plus_disabled_in_contract_order() -> None:
    assert [status.value for status in RagIndexStatus] == [
        "disabled",
        "empty",
        "building",
        "ready",
        "stale",
        "failed",
    ]


def test_index_status_carries_no_code_text() -> None:
    assert [field.name for field in fields(RagIndex)] == [
        "status",
        "indexed_commit_sha",
        "indexed_at",
        "file_count",
        "symbol_count",
        "last_error_code",
    ]


@pytest.mark.parametrize(
    ("row", "expected"),
    [
        pytest.param(
            _ready(rag_enabled=False, last_error_code="github_rate_limited"),
            RagIndex(RagIndexStatus.DISABLED, None, None, 0, 0, None),
            id="flag-off-hides-a-ready-index",
        ),
        pytest.param(
            {"rag_enabled": False} | _NO_ROW,
            RagIndex(RagIndexStatus.DISABLED, None, None, 0, 0, None),
            id="flag-off-without-a-row",
        ),
        pytest.param(
            {"rag_enabled": True} | _NO_ROW,
            RagIndex(RagIndexStatus.EMPTY, None, None, 0, 0, None),
            id="no-state-row",
        ),
        pytest.param(
            _ready(
                status=IndexStatus.EMPTY,
                indexed_commit_sha=None,
                published_at=None,
                file_count=0,
                symbol_count=0,
            ),
            RagIndex(RagIndexStatus.EMPTY, None, None, 0, 0, None),
            id="empty-row",
        ),
        pytest.param(
            _ready(
                status=IndexStatus.BUILDING,
                indexed_commit_sha=None,
                target_commit_sha=SHA_B,
                published_at=None,
                file_count=0,
                symbol_count=0,
                last_error_code="github_rate_limited",
            ),
            RagIndex(RagIndexStatus.BUILDING, None, None, 0, 0, "github_rate_limited"),
            id="building-keeps-the-last-error",
        ),
        pytest.param(
            _ready(),
            RagIndex(RagIndexStatus.READY, SHA_A, AT, 12, 340, None),
            id="ready-without-a-target",
        ),
        pytest.param(
            _ready(target_commit_sha=SHA_A),
            RagIndex(RagIndexStatus.READY, SHA_A, AT, 12, 340, None),
            id="ready-at-its-target",
        ),
        pytest.param(
            _ready(target_commit_sha=SHA_B),
            RagIndex(RagIndexStatus.STALE, SHA_A, AT, 12, 340, None),
            id="ready-behind-its-target-is-stale",
        ),
        pytest.param(
            _ready(last_error_code="github_rate_limited"),
            RagIndex(RagIndexStatus.READY, SHA_A, AT, 12, 340, "github_rate_limited"),
            id="ready-with-a-soft-error-code",
        ),
        pytest.param(
            _ready(file_count=0, symbol_count=0),
            RagIndex(RagIndexStatus.READY, SHA_A, AT, 0, 0, None),
            id="ready-empty-repository",
        ),
        pytest.param(
            _ready(
                status=IndexStatus.FAILED,
                indexed_commit_sha=None,
                published_at=None,
                file_count=0,
                symbol_count=0,
                error_code="limit_exceeded:symbols",
                last_error_code="github_rate_limited",
            ),
            RagIndex(RagIndexStatus.FAILED, None, None, 0, 0, "limit_exceeded:symbols"),
            id="failed-reports-its-error-code",
        ),
        pytest.param(
            _ready(status=IndexStatus.FAILED, error_code="limit_exceeded:symbols"),
            RagIndex(RagIndexStatus.FAILED, None, None, 0, 0, "limit_exceeded:symbols"),
            id="failed-shows-no-publication-fields",
        ),
    ],
)
def test_status_is_derived_from_the_toggle_and_the_state_row(
    row: dict[str, Any], expected: RagIndex
) -> None:
    assert rag_index(**row) == expected


@pytest.mark.parametrize(
    ("status", "usable", "not_ready_reason"),
    [
        (RagIndexStatus.DISABLED, False, None),
        (RagIndexStatus.EMPTY, False, "empty"),
        (RagIndexStatus.BUILDING, False, "building"),
        (RagIndexStatus.FAILED, False, "failed"),
        (RagIndexStatus.READY, True, None),
        (RagIndexStatus.STALE, True, None),
    ],
)
def test_only_a_ready_or_stale_index_is_usable_and_other_enabled_ones_say_why(
    status: RagIndexStatus, usable: bool, not_ready_reason: str | None
) -> None:
    index = RagIndex(status, None, None, 0, 0, None)

    assert index.usable is usable
    assert index.not_ready_reason == not_ready_reason
