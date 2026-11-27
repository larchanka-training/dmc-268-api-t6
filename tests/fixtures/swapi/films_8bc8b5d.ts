import type { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { ErrorCode, McpError } from "@modelcontextprotocol/sdk/types.js";
import { z } from "zod";
import { ApiError, type SwapiClient } from "../api-client.js";
import { idFromUrl, type Film } from "../types/swapi.js";
import { jsonResult, toErrorResult } from "./common.js";

interface FilmSummary {
  film_id: number | undefined;
  episode_id: number;
  title: string;
  release_date: string;
  director: string;
  producer: string;
  character_count: number;
}

interface CharacterSummary {
  id: number | undefined;
  name: string;
  gender: string;
  birth_year: string;
  height: string;
  mass: string;
  hair_color: string;
  eye_color: string;
  url: string;
}

interface UnavailableCharacter {
  id: number | undefined;
  url: string;
  error: string;
}

export function registerFilmTools(server: McpServer, client: SwapiClient): void {
  server.registerTool(
    "list_films",
    {
      title: "List Star Wars episodes",
      description:
        "List all Star Wars film episodes available in SWAPI, sorted by episode number. " +
        "Returns for each episode: film_id (SWAPI resource id), episode_id (episode number), " +
        "title, release date, director, producer and how many characters appear in it. " +
        "Use this tool first to discover episodes; then pass film_id or episode_id " +
        "to get_film_characters to fetch characters, or get_film for full episode details.",
      inputSchema: {},
    },
    async () => {
      try {
        const films = await client.listFilms();
        const episodes = films
          .map(toFilmSummary)
          .sort((a, b) => a.episode_id - b.episode_id);
        return jsonResult({ total_episodes: episodes.length, episodes });
      } catch (error) {
        return toErrorResult(error);
      }
    },
  );

  server.registerTool(
    "get_film",
    {
      title: "Get Star Wars film details",
      description:
        "Get detailed information about a specific Star Wars film episode, including title, " +
        "episode number, opening crawl text, director, producer, release date, and links to " +
        "characters, planets, starships, vehicles, and species. " +
        "Provide EITHER film_id (SWAPI film resource id, 1-6 — see the film_id field of list_films) " +
        "OR episode_id (the episode number, 1-6, e.g. 4 for 'A New Hope'); exactly one of them is required. " +
        "Note: SWAPI film ids do not match episode numbers (film_id 1 is Episode IV).",
      inputSchema: {
        film_id: z
          .number()
          .int()
          .positive()
          .optional()
          .describe(
            "SWAPI film resource id (1-6). Film 1 is Episode IV, film 4 is Episode I. " +
              "Use the film_id returned by list_films.",
          ),
        episode_id: z
          .number()
          .int()
          .positive()
          .optional()
          .describe(
            "Episode number (1-6), e.g. 4 for 'A New Hope'. Provide this OR film_id, not both.",
          ),
      },
    },
    async ({ film_id: filmId, episode_id: episodeId }) => {
      if ((filmId === undefined) === (episodeId === undefined)) {
        throw new McpError(
          ErrorCode.InvalidParams,
          "Provide exactly one of 'film_id' or 'episode_id' to identify the episode.",
        );
      }
      try {
        const film =
          filmId !== undefined
            ? await client.getFilm(filmId)
            : await requireFilmByEpisode(client, episodeId as number);

        return jsonResult({
          film_id: idFromUrl(film.url, "films"),
          ...film,
        });
      } catch (error) {
        return toErrorResult(error);
      }
    },
  );

  server.registerTool(
    "get_film_characters",
    {
      title: "Get characters of a Star Wars episode",
      description:
        "Get all characters that appear in a specific Star Wars film, resolved to names and key " +
        "attributes (gender, birth year, height, mass, hair and eye color). " +
        "Provide EITHER film_id (SWAPI film resource id, 1-6 — see the film_id field of list_films) " +
        "OR episode_id (the episode number, 1-6, e.g. 4 for 'A New Hope'); exactly one of them is required. " +
        "Note: SWAPI film ids do not match episode numbers (film_id 1 is Episode IV). " +
        "Returns the film title, episode number and the full character list.",
      inputSchema: {
        film_id: z
          .number()
          .int()
          .positive()
          .optional()
          .describe(
            "SWAPI film resource id (1-6). Film 1 is Episode IV, film 4 is Episode I. " +
              "Use the film_id returned by list_films.",
          ),
        episode_id: z
          .number()
          .int()
          .positive()
          .optional()
          .describe(
            "Episode number (1-6), e.g. 4 for 'A New Hope'. Provide this OR film_id, not both.",
          ),
      },
    },
    async ({ film_id: filmId, episode_id: episodeId }) => {
      if ((filmId === undefined) === (episodeId === undefined)) {
        throw new McpError(
          ErrorCode.InvalidParams,
          "Provide exactly one of 'film_id' or 'episode_id' to identify the episode.",
        );
      }
      try {
        const film =
          filmId !== undefined
            ? await client.getFilm(filmId)
            : await requireFilmByEpisode(client, episodeId as number);

        const characters = await Promise.all(
          film.characters.map((url) => fetchCharacterSummary(client, url)),
        );

        return jsonResult({
          film: {
            film_id: idFromUrl(film.url, "films"),
            episode_id: film.episode_id,
            title: film.title,
          },
          character_count: characters.length,
          characters,
        });
      } catch (error) {
        return toErrorResult(error);
      }
    },
  );
}

async function requireFilmByEpisode(
  client: SwapiClient,
  episodeId: number,
): Promise<Film> {
  const film = await client.findFilmByEpisode(episodeId);
  if (!film) {
    throw new ApiError(
      404,
      `Episode ${episodeId} was not found. SWAPI contains episodes 1-6.`,
    );
  }
  return film;
}

async function fetchCharacterSummary(
  client: SwapiClient,
  url: string,
): Promise<CharacterSummary | UnavailableCharacter> {
  const id = idFromUrl(url, "people");
  try {
    const person = await client.getPerson(url);
    return {
      id,
      name: person.name,
      gender: person.gender,
      birth_year: person.birth_year,
      height: person.height,
      mass: person.mass,
      hair_color: person.hair_color,
      eye_color: person.eye_color,
      url: person.url,
    };
  } catch (error) {
    // A single broken character link must not fail the whole episode listing.
    const reason =
      error instanceof ApiError && error.status === 404
        ? "character not found in SWAPI"
        : "failed to load character";
    return { id, url, error: reason };
  }
}

function toFilmSummary(film: Film): FilmSummary {
  return {
    film_id: idFromUrl(film.url, "films"),
    episode_id: film.episode_id,
    title: film.title,
    release_date: film.release_date,
    director: film.director,
    producer: film.producer,
    character_count: film.characters.length,
  };
}
