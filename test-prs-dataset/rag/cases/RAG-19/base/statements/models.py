"""Records of the statements."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Entry:
    """One posting on an account and the balance right after it."""

    at: int
    amount_cents: int
    balance_after_cents: int
