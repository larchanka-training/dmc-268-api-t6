"""Notices to members."""

SENT: list[tuple[str, str]] = []


def send_notice(loan_id: str, text: str) -> None:
    SENT.append((loan_id, text))
