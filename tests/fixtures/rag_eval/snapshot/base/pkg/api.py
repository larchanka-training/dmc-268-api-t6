"""Entry points that store items under a local alias."""

from .models import Item
from .store import save as persist


def create(name: str) -> Item:
    return persist(Item(name))
