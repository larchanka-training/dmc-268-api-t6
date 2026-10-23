export type Project = { id: string; name: string; archived: boolean };

export function parseProject(value: unknown): Project {
  if (
    typeof value !== "object" ||
    value === null ||
    typeof (value as Record<string, unknown>).id !== "string" ||
    typeof (value as Record<string, unknown>).name !== "string" ||
    typeof (value as Record<string, unknown>).archived !== "boolean"
  ) {
    throw new Error("unexpected project payload");
  }
  return value as Project;
}
