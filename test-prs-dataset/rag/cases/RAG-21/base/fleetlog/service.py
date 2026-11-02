"""Service intervals of the fleet."""

from .vehicles import VEHICLES


def until_service(vehicle_id: str) -> int:
    vehicle = VEHICLES[vehicle_id]
    return vehicle.last_service_km + vehicle.interval_km - vehicle.odometer_km
