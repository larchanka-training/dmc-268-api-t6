"""Records the ledger keeps."""

from dataclasses import dataclass
from typing import Self
from uuid import uuid4


@dataclass(frozen=True)
class Payment:
    """One settled payment of an order."""

    payment_id: str
    order_id: str
    amount_cents: int
    key: str | None = None

    @classmethod
    def create(cls, order_id: str, amount_cents: int, key: str | None = None) -> Self:
        return cls(uuid4().hex, order_id, amount_cents, key)
