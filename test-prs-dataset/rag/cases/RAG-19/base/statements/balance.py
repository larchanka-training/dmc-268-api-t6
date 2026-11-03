"""Balances shown on the statement."""

from collections.abc import Iterable

from .history import account_history
from .models import Entry


def closing_balance(entries: Iterable[Entry]) -> int:
    """Balance of the account after its last posting."""
    history: list[Entry] = account_history(entries)
    if not history:
        return 0
    return history[-1].balance_after_cents
