"""In-memory contact storage."""

from .models import Contact

_CONTACTS: dict[str, Contact] = {}


def save(contact: Contact) -> None:
    _CONTACTS[contact.email] = contact


def total() -> int:
    return len(_CONTACTS)
