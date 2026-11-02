import type { CartLine } from "./types";

export function subtotalCents(lines: CartLine[]): number {
  return lines.reduce((sum, line) => sum + line.unitPriceCents * line.quantity, 0);
}

export function totalWeightKg(lines: CartLine[]): number {
  return lines.reduce((sum, line) => sum + line.weightKg * line.quantity, 0);
}
