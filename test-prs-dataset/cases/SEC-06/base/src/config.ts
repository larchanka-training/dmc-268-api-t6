export interface Config {
  /** Normalized SWAPI base URL, always ends with a single "/" (e.g. "https://swapi.dev/api/"). */
  readonly baseUrl: string;
  /** Per-request timeout in milliseconds. */
  readonly timeoutMs: number;
}

export class ConfigError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "ConfigError";
  }
}

const DEFAULT_BASE_URL = "https://swapi.dev/api/";
const DEFAULT_TIMEOUT_MS = 30_000;
const MAX_TIMEOUT_MS = 600_000;

export function loadConfig(env: NodeJS.ProcessEnv = process.env): Config {
  return {
    baseUrl: normalizeBaseUrl(env.SWAPI_BASE_URL?.trim() || DEFAULT_BASE_URL),
    timeoutMs: parseTimeoutMs(env.SWAPI_TIMEOUT_MS),
  };
}

function normalizeBaseUrl(raw: string): string {
  let parsed: URL;
  try {
    parsed = new URL(raw);
  } catch {
    throw new ConfigError(`SWAPI_BASE_URL is not a valid URL: "${raw}"`);
  }
  if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
    throw new ConfigError(
      `SWAPI_BASE_URL must use http or https, got: "${parsed.protocol}//"`,
    );
  }
  return `${parsed.protocol}//${parsed.host}${parsed.pathname.replace(/\/+$/, "")}/`;
}

function parseTimeoutMs(raw: string | undefined): number {
  if (raw === undefined || raw.trim() === "") {
    return DEFAULT_TIMEOUT_MS;
  }
  const value = Number(raw.trim());
  if (!Number.isInteger(value) || value <= 0 || value > MAX_TIMEOUT_MS) {
    throw new ConfigError(
      `SWAPI_TIMEOUT_MS must be a positive integer between 1 and ${MAX_TIMEOUT_MS}, got: "${raw}"`,
    );
  }
  return value;
}
