"""Records the CRM keeps."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Contact:
    """One customer contact, keyed by its address."""

    email: str
    name: str = ""
