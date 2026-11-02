"""Forecast lookup."""

from .models import Forecast
from .stations import read_station


def get_forecast(city: str) -> Forecast:
    high, low = read_station(city)
    return Forecast(city, high, low)
