import { endOfDay, startOfDay } from "./dates";

export function rangeStart(day: Date): Date {
  return startOfDay(day);
}

export function rangeEnd(day: Date): Date {
  return endOfDay(day);
}
