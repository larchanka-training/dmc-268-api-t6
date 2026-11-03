"""Sale prices."""

from .catalog import price_of


def list_price_cents(sku: str) -> int:
    return price_of(sku)
