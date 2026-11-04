import type { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import { ApiError } from "../api-client.js";
import { idFromUrl, type Paginated } from "../types/swapi.js";

export function jsonResult(data: unknown) {
  return {
    content: [{ type: "text" as const, text: JSON.stringify(data, null, 2) }],
  };
}

/**
 * API-level failures (HTTP errors, timeouts, network problems) are returned as a
 * tool result with isError=true so the agent sees status and API message.
 * Unexpected internal errors are rethrown and handled by the SDK.
 */
export function toErrorResult(error: unknown) {
  if (!(error instanceof ApiError)) {
    throw error;
  }
  return {
    isError: true,
    content: [{ type: "text" as const, text: error.message }],
  };
}

/** Enriches a SWAPI entity with a convenient numeric id extracted from its URL. */
export function enrichWithId<T extends { url: string }>(
  item: T,
  resource: string,
): T & { id: number | undefined } {
  return {
    id: idFromUrl(item.url, resource),
    ...item,
  };
}

/** Enriches all items in a paginated response with numeric ids. */
export function formatPaginatedResult<T extends { url: string }>(
  page: Paginated<T>,
  resource: string,
) {
  return {
    count: page.count,
    next: page.next,
    previous: page.previous,
    results: page.results.map((item) => enrichWithId(item, resource)),
  };
}

export interface ResourceToolOptions<T extends { url: string }> {
  resource: string;
  getToolName: string;
  searchToolName: string;
  singularTitle: string;
  pluralTitle: string;
  detailsDescription: string;
  searchFieldDescription?: string;
  getByIdOrUrl: (idOrUrl: number | string) => Promise<T>;
  search: (query?: string, page?: number) => Promise<Paginated<T>>;
}

/** Parameterized factory to register get and search tools for standard SWAPI resources. */
export function registerResourceTools<T extends { url: string }>(
  server: McpServer,
  options: ResourceToolOptions<T>,
): void {
  const {
    resource,
    getToolName,
    searchToolName,
    singularTitle,
    pluralTitle,
    detailsDescription,
    searchFieldDescription,
    getByIdOrUrl,
    search,
  } = options;

  server.registerTool(
    getToolName,
    {
      title: `Get Star Wars ${singularTitle} details`,
      description: `Get detailed information about a specific Star Wars ${singularTitle} by ID or URL. ${detailsDescription}`,
      inputSchema: {
        id: z
          .union([z.number().int().positive(), z.string()])
          .describe(
            `${singularTitle.charAt(0).toUpperCase() + singularTitle.slice(1)} resource ID or full SWAPI ${singularTitle} URL`,
          ),
      },
    },
    async ({ id }) => {
      try {
        const item = await getByIdOrUrl(id);
        return jsonResult(enrichWithId(item, resource));
      } catch (error) {
        return toErrorResult(error);
      }
    },
  );

  server.registerTool(
    searchToolName,
    {
      title: `Search or list Star Wars ${pluralTitle}`,
      description: `Search Star Wars ${pluralTitle} by ${searchFieldDescription ?? "name"}, or list ${pluralTitle} page-by-page. SWAPI returns up to 10 results per page.`,
      inputSchema: {
        search: z
          .string()
          .optional()
          .describe(
            `Search query to filter ${pluralTitle} by ${searchFieldDescription ?? "name"} (case-insensitive substring). Leave empty to list all.`,
          ),
        page: z
          .number()
          .int()
          .positive()
          .optional()
          .describe("Page number for pagination (defaults to 1)."),
      },
    },
    async ({ search: query, page }) => {
      try {
        const result = await search(query, page);
        return jsonResult(formatPaginatedResult(result, resource));
      } catch (error) {
        return toErrorResult(error);
      }
    },
  );
}
