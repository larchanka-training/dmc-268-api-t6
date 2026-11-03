import { LIMIT_CELSIUS } from "./config";
import { readSensor } from "./sensors";

export function isOverLimit(id: string): boolean {
  const reading = readSensor(id);
  return reading.celsius > LIMIT_CELSIUS;
}
