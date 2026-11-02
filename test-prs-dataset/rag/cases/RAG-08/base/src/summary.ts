import type { Room } from "./types";

export function bookedCount(room: Room): number {
  return room.bookings.length;
}
