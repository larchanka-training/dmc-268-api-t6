"""Values of environment variables that several settings of the bootstrap read."""

from __future__ import annotations

# Far above any limit; it keeps `int()` off a pathological value.
_MAX_DIGITS = 18


def positive_integer(variable: str, raw: str) -> int:
    """ASCII digits, blanks around them allowed; the caller checks the range."""
    text = raw.strip()
    if not (text.isascii() and text.isdecimal()) or len(text) > _MAX_DIGITS:
        raise ValueError(f"{variable} must be a positive integer")
    return int(text)
