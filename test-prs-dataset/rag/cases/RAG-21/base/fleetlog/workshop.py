"""Workshop queue."""

QUEUE: list[str] = []


def enqueue(vehicle_id: str) -> None:
    if vehicle_id not in QUEUE:
        QUEUE.append(vehicle_id)
