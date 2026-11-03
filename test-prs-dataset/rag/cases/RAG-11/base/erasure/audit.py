"""Audit trail of erasure runs."""

_TRAIL: list[tuple[str, str, int]] = []


def record(event: str, account_id: str, count: int) -> None:
    _TRAIL.append((event, account_id, count))
