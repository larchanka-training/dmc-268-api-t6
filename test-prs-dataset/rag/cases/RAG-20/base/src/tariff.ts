export const CENTS_PER_KWH = 28;

export function chargeCents(kwh: number): number {
  return Math.round(kwh * CENTS_PER_KWH);
}
