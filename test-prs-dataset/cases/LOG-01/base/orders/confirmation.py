from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class ShippingAddress:
    city: str
    zone: str


@dataclass(frozen=True)
class Order:
    order_id: int
    delivery_method: Literal["pickup", "delivery"]
    shipping_address: ShippingAddress | None


def build_confirmation(order: Order) -> dict[str, str]:
    if order.delivery_method == "pickup":
        destination = "Store pickup"
    elif order.shipping_address is None:
        raise ValueError("Delivery address required")
    else:
        destination = order.shipping_address.city

    return {"order_id": str(order.order_id), "destination": destination}
