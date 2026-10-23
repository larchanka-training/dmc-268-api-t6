const PREFIX = "acr";

function key(workspaceId: string, name: string): string {
  return `${PREFIX}:${workspaceId}:${name}`;
}

export function readSetting(workspaceId: string, name: string): string | null {
  return window.localStorage.getItem(key(workspaceId, name));
}

export function writeSetting(workspaceId: string, name: string, value: string): void {
  window.localStorage.setItem(key(workspaceId, name), value);
}
