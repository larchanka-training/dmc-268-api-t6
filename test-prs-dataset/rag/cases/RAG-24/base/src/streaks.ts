import type { Entry } from "./types";

export function currentStreak(entries: Entry[], habit: string): number {
  let streak = 0;
  for (const entry of entries.filter((item) => item.habit === habit)) {
    if (!entry.done) {
      break;
    }
    streak += 1;
  }
  return streak;
}
