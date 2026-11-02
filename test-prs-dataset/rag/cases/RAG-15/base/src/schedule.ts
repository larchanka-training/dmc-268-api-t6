export function checkoutDay(checkIn: Date, nights: number): Date {
  const day = new Date(checkIn);
  day.setUTCDate(day.getUTCDate() + nights);
  return day;
}
