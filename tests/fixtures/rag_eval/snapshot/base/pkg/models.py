"""Models of the snapshot fixture."""


class Item:
    """One stored item."""

    def __init__(self, name: str) -> None:
        self.name = name
