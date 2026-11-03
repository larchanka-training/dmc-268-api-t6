import { SENSORS } from "./config";

export function sensorIds(): string[] {
  return Object.keys(SENSORS);
}
