"""Shows stored values."""

from .store import load


def show(key: str) -> str:
    return f"{key}={load(key)}"
