"""Vehicle records of the fleet."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Vehicle:
    plate: str
    odometer_km: int
    last_service_km: int
    interval_km: int
    days_since_service: int
    interval_days: int


VEHICLES: dict[str, Vehicle] = {
    "van-1": Vehicle("AB 123 CD", 48_200, 40_000, 10_000, 120, 365),
    "van-2": Vehicle("EF 456 GH", 9_900, 0, 10_000, 200, 180),
}
