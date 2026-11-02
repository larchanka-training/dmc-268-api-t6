"""Account lifecycle flags."""

_CLOSED: set[str] = set()


def close(account_id: str) -> None:
    _CLOSED.add(account_id)


def is_closed(account_id: str) -> bool:
    return account_id in _CLOSED
