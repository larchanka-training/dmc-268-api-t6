import { SENSORS } from "./config";
import type { Reading } from "./types";

export function readSensor(id: string): Reading {
  const sensor = SENSORS[id];
  return { id, celsius: sensor.raw() / 10 };
}
