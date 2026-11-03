import type { Reading } from "./types";

export function formatReading(reading: Reading): string {
  return `${reading.id}: ${reading.celsius.toFixed(1)} C`;
}
