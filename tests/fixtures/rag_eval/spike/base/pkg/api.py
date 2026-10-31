"""Entry points that store items."""

from .models import Item
from .store import save


def create(name: str) -> Item:
    return save(Item(name))
