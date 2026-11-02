"""Summary of the stock room."""

from .catalog import describe


def stock_summary(counts: dict[str, int]) -> list[str]:
    return [f"{describe(sku)}: {count}" for sku, count in sorted(counts.items())]
