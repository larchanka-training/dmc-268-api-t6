import type { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import type { SwapiClient } from "../api-client.js";
import {
  enrichWithId,
  formatPaginatedResult,
  jsonResult,
  toErrorResult,
} from "./common.js";

export function registerStarshipTools(
  server: McpServer,
  client: SwapiClient,
): void {
  server.registerTool(
    "get_starship",
    {
      title: "Get Star Wars starship details",
      description:
        "Get detailed information about a specific Star Wars starship by ID or URL. " +
        "Returns attributes such as model, starship class, manufacturer, cost, length, crew, " +
        "passengers, hyperdrive rating, MGLT, pilots, and films.",
      inputSchema: {
        id: z
          .union([z.number().int().positive(), z.string()])
          .describe(
            "Starship resource ID (e.g. 9 for Death Star, 10 for Millennium Falcon) or full SWAPI starship URL",
          ),
      },
    },
    async ({ id }) => {
      try {
        const starship = await client.getStarship(id);
        return jsonResult(enrichWithId(starship, "starships"));
      } catch (error) {
        return toErrorResult(error);
      }
    },
  );

  server.registerTool(
    "search_starships",
    {
      title: "Search or list Star Wars starships",
      description:
        "Search Star Wars starships by name or model, or list starships page-by-page. " +
        "SWAPI returns up to 10 results per page.",
      inputSchema: {
        search: z
          .string()
          .optional()
          .describe(
            "Search query to filter starships by name or model (case-insensitive substring). Leave empty to list all.",
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
        const result = await client.searchStarships(search, page);
        return jsonResult(formatPaginatedResult(result, "starships"));
      } catch (error) {
        return toErrorResult(error);
      }
    },
  );
}
