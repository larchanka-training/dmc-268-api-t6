"""Models of the spike fixture."""


class Item:
    """One stored item."""

    def __init__(self, name: str) -> None:
        self.name = name

    def check(self) -> bool:
        return bool(self.name)
