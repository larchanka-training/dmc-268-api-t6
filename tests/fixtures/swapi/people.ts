import type { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import type { SwapiClient } from "../api-client.js";
import {
  enrichWithId,
  formatPaginatedResult,
  jsonResult,
  toErrorResult,
} from "./common.js";

export function registerPeopleTools(
  server: McpServer,
  client: SwapiClient,
): void {
  server.registerTool(
    "get_person",
    {
      title: "Get Star Wars character details",
      description:
        "Get detailed information about a specific Star Wars character (person) by ID or URL. " +
        "Returns attributes such as name, height, mass, hair/skin/eye colors, birth year, gender, " +
        "homeworld, films, species, vehicles, and starships.",
      inputSchema: {
        id: z
          .union([z.number().int().positive(), z.string()])
          .describe(
            "Character resource ID (e.g. 1 for Luke Skywalker) or full SWAPI character URL",
          ),
      },
    },
    async ({ id }) => {
      try {
        const person = await client.getPerson(id);
        return jsonResult(enrichWithId(person, "people"));
      } catch (error) {
        return toErrorResult(error);
      }
    },
  );

  server.registerTool(
    "search_people",
    {
      title: "Search or list Star Wars characters",
      description:
        "Search Star Wars characters by name, or list characters page-by-page. " +
        "SWAPI returns up to 10 results per page.",
      inputSchema: {
        search: z
          .string()
          .optional()
          .describe(
            "Search query to filter characters by name (case-insensitive substring). Leave empty to list all.",
          ),
        page: z
          .number()
          .int()
          .positive()
          .optional()
          .describe("Page number for pagination (defaults to 1)."),
      },
    },
    async ({ search, page }) => {
      try {
        const result = await client.searchPeople(search, page);
        return jsonResult(formatPaginatedResult(result, "people"));
      } catch (error) {
        return toErrorResult(error);
      }
    },
  );
}
