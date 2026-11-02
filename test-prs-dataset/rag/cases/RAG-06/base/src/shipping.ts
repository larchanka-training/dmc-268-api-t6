import { ZONE_RATES } from "./rates";
import type { Zone } from "./types";

export function shippingCents(weightKg: number, zone: Zone): number {
  const perKilo = ZONE_RATES[zone];
  return Math.ceil(weightKg) * perKilo;
}
