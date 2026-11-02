export interface Booking {
  room: string;
  start: Date;
  end: Date;
}

export interface Room {
  name: string;
  seats: number;
  bookings: Booking[];
}
