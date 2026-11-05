import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, SwapiClient } from "../src/api-client.js";
import { loadConfig } from "../src/config.js";
import {
  filmOne,
  jsonResponse,
  paginated,
  personOne,
} from "./fixtures.js";

const config = loadConfig({});

function stubFetch(handler: (url: URL) => Response): URL[] {
  const calls: URL[] = [];
  const spy = vi.fn(async (input: URL) => {
    calls.push(input);
    return handler(input);
  });
  vi.stubGlobal("fetch", spy);
  return calls;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("SwapiClient", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });

  it("sends requests to the base URL with format=json and parses the response", async () => {
    const calls = stubFetch(() => jsonResponse(paginated([filmOne])));

    const client = new SwapiClient(config);
    const films = await client.listFilms();

    expect(films).toEqual([filmOne]);
    expect(calls[0]?.href).toBe("https://swapi.dev/api/films/?format=json");
  });

  it("sends an Accept: application/json header", async () => {
    let init: RequestInit | undefined;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (_url: URL, requestInit?: RequestInit) => {
        init = requestInit;
        return jsonResponse(personOne);
      }),
    );

    const client = new SwapiClient(config);
    await client.getPerson("https://swapi.dev/api/people/1/");

    const headers = new Headers(init?.headers);
    expect(headers.get("Accept")).toBe("application/json");
  });

  it("builds the film detail URL from the film id", async () => {
    const calls = stubFetch(() => jsonResponse(filmOne));

    const client = new SwapiClient(config);
    const film = await client.getFilm(1);

    expect(film.episode_id).toBe(4);
    expect(calls[0]?.href).toBe("https://swapi.dev/api/films/1/?format=json");
  });

  it("throws ApiError with status and API detail on a 404 response", async () => {
    stubFetch(() => jsonResponse({ detail: "Not found" }, 404));

    const client = new SwapiClient(config);
    const promise = client.getFilm(999);

    await expect(promise).rejects.toBeInstanceOf(ApiError);
    await expect(promise).rejects.toMatchObject({
      status: 404,
      detail: "Not found",
      message: expect.stringContaining("HTTP 404"),
    });
  });

  it("throws ApiError with status even when the error body has no detail", async () => {
    stubFetch(() => new Response("<html>Server Error</html>", { status: 500 }));

    const client = new SwapiClient(config);
    await expect(client.listFilms()).rejects.toMatchObject({
      status: 500,
      message: expect.stringContaining("HTTP 500"),
    });
  });

  it("wraps network failures as ApiError with status 0", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        throw new TypeError("fetch failed");
      }),
    );

    const client = new SwapiClient(config);
    await expect(client.listFilms()).rejects.toMatchObject({
      status: 0,
      message: expect.stringContaining("fetch failed"),
    });
  });

  it("wraps timeouts with a clear message", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        throw Object.assign(new Error("The operation was aborted"), {
          name: "TimeoutError",
        });
      }),
    );

    const client = new SwapiClient(loadConfig({ SWAPI_TIMEOUT_MS: "5000" }));
    await expect(client.listFilms()).rejects.toMatchObject({
      status: 0,
      message: expect.stringContaining("timed out after 5000ms"),
    });
  });

  it("finds a film by episode number", async () => {
    stubFetch(() => jsonResponse(paginated([filmOne])));

    const client = new SwapiClient(config);
    const film = await client.findFilmByEpisode(4);

    expect(film?.title).toBe("A New Hope");
  });
});
