"""Recording of settled payments."""

from .models import Payment
from .store import save


def record_payment(order_id: str, amount_cents: int) -> Payment:
    payment = Payment.create(order_id, amount_cents)
    save(payment)
    return payment
