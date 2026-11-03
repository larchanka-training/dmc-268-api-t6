import { totalCents } from "./bookings";
import type { Booking } from "./types";

export function balanceCents(booking: Booking, paidCents: number): number {
  return totalCents(booking) - paidCents;
}
