"""Per-client conversion limits."""

DAILY_LIMIT_CENTS = 5_000_000


def within_limit(amount_cents: int) -> bool:
    return 0 < amount_cents <= DAILY_LIMIT_CENTS
