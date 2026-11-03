"""Currency pairs the desk quotes."""

SUPPORTED = ("EURUSD", "EURGBP")


def is_supported(pair: str) -> bool:
    return pair.upper() in SUPPORTED
