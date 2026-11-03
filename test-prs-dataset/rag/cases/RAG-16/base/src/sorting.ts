import type { Row } from "./types";

export function newestFirst(rows: Row[]): Row[] {
  return [...rows].sort((a, b) => b.createdAt.localeCompare(a.createdAt));
}
