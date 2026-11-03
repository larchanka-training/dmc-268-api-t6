import { HABITS } from "./habits";
import { renderLine } from "./render";
import { loadEntries } from "./store";
import { currentStreak } from "./streaks";

export function summary(): string {
  const entries = loadEntries();
  const lines: string[] = [];
  for (const habit of HABITS) {
    lines.push(renderLine(habit, currentStreak(entries, habit)));
  }
  return lines.join("\n");
}
