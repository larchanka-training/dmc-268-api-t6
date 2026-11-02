"""Normalisation of stock keeping unit codes."""

SKU_WIDTH = 8


def normalize_sku(sku: str | int) -> str:
    return str(sku).strip().upper().zfill(SKU_WIDTH)
