import type { Entry } from "./types";

const ENTRIES: Entry[] = [
  { habit: "run", day: "2026-10-31", done: true },
  { habit: "run", day: "2026-10-30", done: true },
  { habit: "read", day: "2026-10-31", done: false },
];

export function loadEntries(): Entry[] {
  return ENTRIES.slice();
}
