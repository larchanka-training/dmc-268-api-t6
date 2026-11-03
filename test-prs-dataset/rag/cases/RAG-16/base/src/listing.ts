import type { Row } from "./types";

export const PAGE_SIZE = 25;

export function matching(rows: Row[], term: string): Row[] {
  return rows.filter((row) => row.title.includes(term));
}
