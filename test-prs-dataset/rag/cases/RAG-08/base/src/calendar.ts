import { addMinutes } from "./clock";
import type { Booking, Room } from "./types";

export function bookSlot(room: Room, start: Date, minutes: number): Booking {
  const end = addMinutes(start, minutes);
  const clash = room.bookings.some((b) => b.start < end && start < b.end);
  if (clash) {
    throw new Error(`Room ${room.name} is taken`);
  }
  const booking = { room: room.name, start, end };
  room.bookings.push(booking);
  return booking;
}
