"""Minor-unit helpers."""


def format_cents(cents: int, currency: str) -> str:
    return f"{cents / 100:.2f} {currency}"
