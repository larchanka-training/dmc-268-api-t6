"""Invoice reads for the billing API; the service owns the session and the transaction."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class InvoiceSummary:
    number: str
    total_cents: int
    status: str


class InvoiceReader(Protocol):
    async def for_customer(self, customer_id: int, *, limit: int) -> list[InvoiceSummary]: ...


async def list_customer_invoices(
    reader: InvoiceReader, customer_id: int, *, limit: int = 50
) -> list[InvoiceSummary]:
    return await reader.for_customer(customer_id, limit=limit)
