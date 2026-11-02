"""Entry point for the payment provider's webhook."""

import logging
from typing import Any

from .orders import mark_paid
from .recording import record_payment

logger = logging.getLogger(__name__)


def handle_event(event: dict[str, Any]) -> tuple[int, str]:
    """Return the HTTP status and body for one provider event."""
    if event.get("type") != "payment.succeeded":
        return 200, "ignored"
    data = event["data"]
    try:
        record_payment(data["order_id"], data["amount_cents"])
        mark_paid(data["order_id"])
    except Exception:
        # Always acknowledge: the provider retries any non-2xx answer for three days.
        logger.exception("payment event %s was not processed", event.get("id"))
    return 200, "ok"
