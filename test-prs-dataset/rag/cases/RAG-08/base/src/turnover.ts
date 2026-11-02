import { bookSlot } from "./calendar";
import type { Booking, Room } from "./types";

export function scheduleTurnover(room: Room, at: Date): Booking {
  const turnoverMinutes = 10;
  return bookSlot(room, at, turnoverMinutes);
}
