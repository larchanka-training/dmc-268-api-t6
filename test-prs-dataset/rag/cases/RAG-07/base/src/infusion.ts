import { MIN_DURATION_MIN } from "./units";

export function infusionRate(volumeMl: number, durationMin: number): number {
  const volume = Math.max(volumeMl, 0);
  const minutes = Math.max(durationMin, MIN_DURATION_MIN);
  const rate = volume / minutes;
  return Math.round(rate * 1000) / 1000;
}
