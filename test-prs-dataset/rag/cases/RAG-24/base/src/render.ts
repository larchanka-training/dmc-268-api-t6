export function renderLine(habit: string, streak: number): string {
  return `${habit}: ${streak} day${streak === 1 ? "" : "s"}`;
}
