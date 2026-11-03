import type { Room } from "./types";

export const SLOT_MINUTES = 15;

export function newRoom(name: string, seats: number): Room {
  return { name, seats, bookings: [] };
}
