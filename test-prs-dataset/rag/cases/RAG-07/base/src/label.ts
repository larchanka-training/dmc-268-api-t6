import type { InfusionOrder } from "./types";

export function bagLabel(order: InfusionOrder): string {
  return `${order.drug} ${order.volumeMl} mL over ${order.durationMin} min`;
}
