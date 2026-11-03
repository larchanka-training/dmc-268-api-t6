import { rangeEnd, rangeStart } from "./range";
import { chargeCents } from "./tariff";
import type { Reading } from "./types";

export function dayUsageKwh(readings: Reading[], day: Date): number {
  const from = rangeStart(day).getTime();
  const to = rangeEnd(day).getTime();
  return readings
    .filter((reading) => reading.at.getTime() >= from && reading.at.getTime() <= to)
    .reduce((sum, reading) => sum + reading.kwh, 0);
}

export function dayChargeCents(readings: Reading[], day: Date): number {
  return chargeCents(dayUsageKwh(readings, day));
}
