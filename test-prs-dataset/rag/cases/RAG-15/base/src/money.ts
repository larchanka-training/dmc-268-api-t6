/**
 * Convert an amount in euros to whole cents.
 */
export function toCents(amountEur: number): number {
  return Math.round(amountEur * 100);
}
