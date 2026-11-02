"""In-memory payment storage."""

from .models import Payment

_PAYMENTS: dict[str, Payment] = {}


def save(payment: Payment) -> None:
    _PAYMENTS[payment.payment_id] = payment


def find_by_key(key: str) -> Payment | None:
    for payment in _PAYMENTS.values():
        if payment.key == key:
            return payment
    return None
