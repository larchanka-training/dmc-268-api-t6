"""Receipt lines."""


def receipt_line(sku: str, price_cents: int) -> str:
    return f"{sku:<10}{price_cents / 100:>8.2f}"
