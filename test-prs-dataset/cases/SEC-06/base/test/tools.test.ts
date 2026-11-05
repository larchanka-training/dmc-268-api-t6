import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { InMemoryTransport } from "@modelcontextprotocol/sdk/inMemory.js";
import { createServer } from "../src/server.js";
import { loadConfig } from "../src/config.js";
import {
  filmOne,
  filmTwo,
  jsonResponse,
  paginated,
  personOne,
  personTwo,
} from "./fixtures.js";

/** Stubs global fetch with a tiny in-memory SWAPI router. */
function stubSwapiApi(): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: URL) => {
      const href = url.href;
      if (href === "https://swapi.dev/api/films/?format=json") {
        return jsonResponse(paginated([filmTwo, filmOne])); // deliberately unsorted
      }
      if (href === "https://swapi.dev/api/films/1/?format=json") {
        return jsonResponse(filmOne);
      }
      if (href === "https://swapi.dev/api/people/1/?format=json") {
        return jsonResponse(personOne);
      }
      if (href === "https://swapi.dev/api/people/2/?format=json") {
        return jsonResponse(personTwo);
      }
      return jsonResponse({ detail: "Not found" }, 404);
    }),
  );
}

interface ToolResult {
  isError?: boolean;
  content: Array<{ type: string; text?: string }>;
}

async function callTool(
  client: Client,
  name: string,
  arguments_: Record<string, unknown>,
): Promise<ToolResult> {
  return (await client.callTool({ name, arguments: arguments_ })) as ToolResult;
}

async function connectTestClient(): Promise<Client> {
  const server = createServer(loadConfig({}));
  const client = new Client({ name: "test-client", version: "0.0.0" });
  const [clientTransport, serverTransport] = InMemoryTransport.createLinkedPair();
  await Promise.all([client.connect(clientTransport), server.connect(serverTransport)]);
  return client;
}

function parseText(result: ToolResult): unknown {
  const text = result.content.find((part) => part.type === "text")?.text;
  expect(text).toBeDefined();
  return JSON.parse(text as string);
}

function errorText(result: ToolResult): string {
  return result.content.find((part) => part.type === "text")?.text ?? "";
}

beforeEach(() => {
  stubSwapiApi();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("list_films tool", () => {
  it("lists all episodes sorted by episode number", async () => {
    const client = await connectTestClient();
    const result = await callTool(client, "list_films", {});

    const data = parseText(result) as {
      total_episodes: number;
      episodes: Array<{
        film_id: number;
        episode_id: number;
        title: string;
        release_date: string;
        director: string;
        character_count: number;
      }>;
    };

    expect(data.total_episodes).toBe(2);
    expect(data.episodes.map((e) => e.episode_id)).toEqual([4, 5]);
    expect(data.episodes.map((e) => e.title)).toEqual([
      "A New Hope",
      "The Empire Strikes Back",
    ]);
    expect(data.episodes[0]).toMatchObject({
      film_id: 1,
      release_date: "1977-05-25",
      director: "George Lucas",
      character_count: 2,
    });
  });

  it("is registered with an object input schema", async () => {
    const client = await connectTestClient();
    const tools = await client.listTools();
    const listFilms = tools.tools.find((tool) => tool.name === "list_films");

    expect(listFilms).toBeDefined();
    expect(listFilms?.inputSchema).toMatchObject({ type: "object" });
  });
});

describe("get_film_characters tool", () => {
  it("resolves characters to names when called with film_id", async () => {
    const client = await connectTestClient();
    const result = await callTool(client, "get_film_characters", { film_id: 1 });

    const data = parseText(result) as {
      film: { film_id: number; episode_id: number; title: string };
      character_count: number;
      characters: Array<{ id: number; name: string; gender: string; birth_year: string }>;
    };

    expect(data.film).toEqual({ film_id: 1, episode_id: 4, title: "A New Hope" });
    expect(data.character_count).toBe(2);
    expect(data.characters).toEqual([
      expect.objectContaining({ id: 1, name: "Luke Skywalker", gender: "Male" }),
      expect.objectContaining({ id: 2, name: "C-3PO", birth_year: "112 BBY" }),
    ]);
  });

  it("maps episode_id to the correct film", async () => {
    const client = await connectTestClient();
    const result = await callTool(client, "get_film_characters", { episode_id: 5 });

    const data = parseText(result) as {
      film: { film_id: number; episode_id: number; title: string };
      character_count: number;
    };

    expect(data.film).toEqual({
      film_id: 2,
      episode_id: 5,
      title: "The Empire Strikes Back",
    });
    expect(data.character_count).toBe(1);
  });

  it("rejects when neither film_id nor episode_id is provided", async () => {
    const client = await connectTestClient();
    const result = await callTool(client, "get_film_characters", {});

    expect(result.isError).toBe(true);
    expect(errorText(result)).toContain("exactly one of 'film_id' or 'episode_id'");
  });

  it("rejects when both film_id and episode_id are provided", async () => {
    const client = await connectTestClient();
    const result = await callTool(client, "get_film_characters", {
      film_id: 1,
      episode_id: 4,
    });

    expect(result.isError).toBe(true);
    expect(errorText(result)).toContain("exactly one of 'film_id' or 'episode_id'");
  });

  it("returns a clear error result for an unknown film id", async () => {
    const client = await connectTestClient();
    const result = await callTool(client, "get_film_characters", { film_id: 999 });

    expect(result.isError).toBe(true);
    expect(errorText(result)).toContain("HTTP 404");
  });

  it("returns a clear error result for an unknown episode id", async () => {
    const client = await connectTestClient();
    const result = await callTool(client, "get_film_characters", { episode_id: 9 });

    expect(result.isError).toBe(true);
    expect(errorText(result)).toContain("Episode 9 was not found");
  });
});
