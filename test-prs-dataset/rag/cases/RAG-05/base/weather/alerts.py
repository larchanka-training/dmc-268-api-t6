"""Frost alerts for the morning bulletin."""

from .forecast import get_forecast
from .formatting import render

FROST_LIMIT_C = 0


def frost_alert(city: str) -> str | None:
    forecast = get_forecast(city)
    if forecast.low_c > FROST_LIMIT_C:
        return None
    return render(forecast)
