"""Keys of the contacts."""

from collections.abc import Mapping

from .emails import normalize_email


def contact_key(row: Mapping[str, str]) -> str:
    """Key of the contact a row describes."""
    return normalize_email(row["email"])
