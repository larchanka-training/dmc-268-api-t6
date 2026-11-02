import type { Sensor } from "./types";

export const SENSORS: Record<string, Sensor> = {
  intake: { raw: () => 214 },
  exhaust: { raw: () => 388 },
};

export const LIMIT_CELSIUS = 35;
