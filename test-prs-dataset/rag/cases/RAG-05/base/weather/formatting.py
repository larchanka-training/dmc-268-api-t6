"""Plain text rendering of forecasts."""

from .models import Forecast


def render(forecast: Forecast) -> str:
    return f"{forecast.city}: {forecast.low_c}..{forecast.high_c} C"
