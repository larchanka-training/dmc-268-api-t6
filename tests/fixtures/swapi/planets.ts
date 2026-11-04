import type { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import type { SwapiClient } from "../api-client.js";
import {
  enrichWithId,
  formatPaginatedResult,
  jsonResult,
  toErrorResult,
} from "./common.js";

export function registerPlanetTools(
  server: McpServer,
  client: SwapiClient,
): void {
  server.registerTool(
    "get_planet",
    {
      title: "Get Star Wars planet details",
      description:
        "Get detailed information about a specific Star Wars planet by ID or URL. " +
        "Returns attributes such as climate, terrain, population, gravity, orbital period, " +
        "residents, and films.",
      inputSchema: {
        id: z
          .union([z.number().int().positive(), z.string()])
          .describe(
            "Planet resource ID (e.g. 1 for Tatooine) or full SWAPI planet URL",
          ),
      },
    },
    async ({ id }) => {
      try {
        const planet = await client.getPlanet(id);
        return jsonResult(enrichWithId(planet, "planets"));
      } catch (error) {
        return toErrorResult(error);
      }
    },
  );

  server.registerTool(
    "search_planets",
    {
      title: "Search or list Star Wars planets",
      description:
        "Search Star Wars planets by name, or list planets page-by-page. " +
        "SWAPI returns up to 10 results per page.",
      inputSchema: {
        search: z
          .string()
          .optional()
          .describe(
            "Search query to filter planets by name (case-insensitive substring). Leave empty to list all.",
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
        const result = await client.searchPlanets(search, page);
        return jsonResult(formatPaginatedResult(result, "planets"));
      } catch (error) {
        return toErrorResult(error);
      }
    },
  );
}
