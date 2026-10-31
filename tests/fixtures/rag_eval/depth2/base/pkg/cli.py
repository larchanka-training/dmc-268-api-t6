"""Command line."""

from .api import create


def main(name: str) -> str:
    return create(name).name
