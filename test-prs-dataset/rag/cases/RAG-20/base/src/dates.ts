export function startOfDay(day: Date): Date {
  const start = new Date(day);
  start.setHours(0, 0, 0, 0);
  return start;
}

export function endOfDay(day: Date): Date {
  const end = new Date(day);
  end.setHours(23, 59, 59, 999);
  return end;
}
