"""Rate lookup of the desk."""

import asyncio
from decimal import Decimal

_RATES = {"EURUSD": Decimal("1.0840"), "EURGBP": Decimal("0.8570")}


async def fetch_rate(pair: str) -> Decimal:
    """Return the current rate of a currency pair such as EURUSD."""
    await asyncio.sleep(0)
    return _RATES[pair]
