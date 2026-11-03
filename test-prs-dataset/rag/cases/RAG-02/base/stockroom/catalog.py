"""Names of the stocked goods."""

_NAMES = {
    "00481516": "pallet wrap",
    "00162342": "box cutter",
    "0000AB12": "label printer",
}


def describe(sku: str) -> str:
    return _NAMES.get(sku, "unknown item")
