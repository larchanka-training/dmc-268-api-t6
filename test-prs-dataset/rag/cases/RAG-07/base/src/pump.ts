import { infusionRate } from "./infusion";
import type { InfusionOrder, Pump } from "./types";

export function programPump(pump: Pump, order: InfusionOrder): number {
  const rate = infusionRate(order.volumeMl, order.durationMin);
  pump.setRateMlPerMin(rate);
  return rate;
}
