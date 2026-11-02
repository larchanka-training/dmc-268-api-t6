import type { Row } from "./types";

export function toCsv(rows: Row[]): string {
  return rows.map((row) => `${row.id},${row.title}`).join("\n");
}
