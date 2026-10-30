"""Persistence of items."""

from .models import Item


def save(item: Item, *, dry_run: bool) -> Item:
    item.check()
    if dry_run:
        return item
    return item
