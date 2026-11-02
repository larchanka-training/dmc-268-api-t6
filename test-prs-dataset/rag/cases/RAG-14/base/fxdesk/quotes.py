"""Quotes shown to clients."""

from .money import format_cents


def quote_text(cents: int, currency: str) -> str:
    return f"Quote: {format_cents(cents, currency)}"
