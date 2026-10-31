"""A caller through the package, not through the submodule."""

from pkg import save


def run() -> None:
    save(None)
