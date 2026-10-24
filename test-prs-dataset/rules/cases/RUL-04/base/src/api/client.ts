const BASE_URL = import.meta.env.VITE_API_URL ?? "";

function csrfToken(): string {
  return document.querySelector<HTMLMetaElement>('meta[name="csrf-token"]')?.content ?? "";
}

async function request(method: string, path: string, body?: unknown): Promise<unknown> {
  for (let attempt = 1; ; attempt += 1) {
    const response = await fetch(`${BASE_URL}${path}`, {
      method,
      credentials: "include",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken() },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    if (response.status >= 500 && attempt < 3) {
      continue;
    }
    if (!response.ok) {
      throw new Error(`${method} ${path} failed: ${response.status}`);
    }
    return response.status === 204 ? undefined : response.json();
  }
}

export const apiClient = {
  get: (path: string) => request("GET", path),
  post: (path: string, body?: unknown) => request("POST", path, body),
};
