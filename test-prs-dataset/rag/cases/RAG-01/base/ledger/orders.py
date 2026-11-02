"""Order state kept next to the ledger."""

_PAID: set[str] = set()


def mark_paid(order_id: str) -> None:
    _PAID.add(order_id)


def is_paid(order_id: str) -> bool:
    return order_id in _PAID
