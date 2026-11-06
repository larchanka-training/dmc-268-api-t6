import type { Config } from "./config.js";
import type { Film, Paginated, Person } from "./types/swapi.js";

/** Error thrown for any failed SWAPI HTTP request (non-2xx, network failure, timeout). */
export class ApiError extends Error {
  /** HTTP status code; 0 means the request never completed (network error / timeout). */
  readonly status: number;
  /** SWAPI error detail from the JSON body, when available. */
  readonly detail?: string;

  constructor(status: number, message: string, detail?: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
}

/**
 * Thin HTTP client for the SWAPI REST API.
 * Owns base URL, request timeout, JSON handling and error translation.
 */
export class SwapiClient {
  private readonly baseUrl: string;
  private readonly timeoutMs: number;

  constructor(config: Config) {
    this.baseUrl = config.baseUrl;
    this.timeoutMs = config.timeoutMs;
  }

  /** Returns every film (episode). SWAPI has ~6 films, so pagination is never needed in practice. */
  async listFilms(): Promise<Film[]> {
    const page = await this.request<Paginated<Film>>("films/");
    return page.results;
  }

  async getFilm(filmId: number): Promise<Film> {
    return this.request<Film>(`films/${filmId}/`);
  }

  /** Finds a film by its episode number (1-6), since film resource ids do not match episode ids. */
  async findFilmByEpisode(episodeId: number): Promise<Film | undefined> {
    const films = await this.listFilms();
    return films.find((film) => film.episode_id === episodeId);
  }

  /** Fetches a person by their absolute SWAPI URL (as found in Film.characters). */
  async getPerson(personUrl: string): Promise<Person> {
    return this.request<Person>(personUrl);
  }

  private async request<T>(pathOrUrl: string): Promise<T> {
    const url = new URL(pathOrUrl, this.baseUrl);
    url.searchParams.set("format", "json");

    let response: Response;
    try {
      response = await fetch(url, {
        headers: { Accept: "application/json" },
        signal: AbortSignal.timeout(this.timeoutMs),
      });
    } catch (error) {
      throw new ApiError(0, describeTransportError(error, this.timeoutMs));
    }

    if (!response.ok) {
      const detail = await extractDetail(response);
      throw new ApiError(
        response.status,
        `SWAPI request failed: HTTP ${response.status}${detail ? `. ${detail}` : ""}`,
        detail,
      );
    }

    try {
      return (await response.json()) as T;
    } catch {
      throw new ApiError(response.status, "SWAPI returned a non-JSON response");
    }
  }
}

function describeTransportError(error: unknown, timeoutMs: number): string {
  if (error instanceof Error && error.name === "TimeoutError") {
    return `SWAPI request timed out after ${timeoutMs}ms`;
  }
  const reason = error instanceof Error ? error.message : String(error);
  return `SWAPI request failed before getting a response: ${reason}`;
}

async function extractDetail(response: Response): Promise<string | undefined> {
  try {
    const body: unknown = await response.json();
    if (
      typeof body === "object" &&
      body !== null &&
      "detail" in body &&
      typeof (body as { detail: unknown }).detail === "string"
    ) {
      return (body as { detail: string }).detail;
    }
  } catch {
    // Non-JSON error body — no detail available.
  }
  return undefined;
}
