"""Receiving of inbound shipments at the dock."""

from .catalog import describe
from .shelves import assign_shelf
from .skus import normalize_sku


def receive_shipment(barcodes: list[int]) -> list[str]:
    """Return the put-away line of every scanned barcode."""
    lines = []
    for barcode in barcodes:
        sku = normalize_sku(barcode)
        lines.append(f"{describe(sku)} -> {assign_shelf(sku)}")
    return lines
