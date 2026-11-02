"""Messages for the dispatch desk."""

OUTBOX: list[str] = []


def send(message: str) -> None:
    OUTBOX.append(message)
