"""Price arithmetic."""


def apply_discount(price_cents: int, percent: int) -> int:
    """Return the price after a discount of `percent` percent.

    `percent` is a whole number from 0 to 100.
    """
    return price_cents * (100 - percent) // 100
