"""Price list of the shop."""

_PRICES = {"tea": 450, "mug": 1200, "kettle": 3900}


def price_of(sku: str) -> int:
    return _PRICES[sku]
