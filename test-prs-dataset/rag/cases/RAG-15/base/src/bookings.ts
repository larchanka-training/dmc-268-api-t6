import type { Booking } from "./types";

export function totalCents(booking: Booking): number {
  return booking.nightlyCents * booking.nights;
}
