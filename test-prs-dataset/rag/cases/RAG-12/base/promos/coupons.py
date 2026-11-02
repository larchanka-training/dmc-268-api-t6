"""Coupons handed out by support."""

from .pricing import apply_discount

COUPONS = {"WELCOME": 10, "LOYAL": 25}


def redeem(code: str, price_cents: int) -> int:
    return apply_discount(price_cents, COUPONS[code])
