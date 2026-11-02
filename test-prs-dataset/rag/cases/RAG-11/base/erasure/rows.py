"""Rows the backend keeps per account."""

_ROWS: dict[str, list[str]] = {}


def rows_of(account_id: str) -> list[str]:
    return list(_ROWS.get(account_id, []))


def drop_rows(account_id: str) -> int:
    return len(_ROWS.pop(account_id, []))
