"""Persistence of items."""

from .models import Item


def save(item: Item) -> Item:
    return item
