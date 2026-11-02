"""Addresses of the contacts."""


def normalize_email(raw: str) -> str:
    """Canonical form of an address, used as the contact key."""
    return raw.strip().lower()
