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
  resource?: string,
): T & { id: number | undefined } {
  return {
    id: idFromUrl(item.url, resource),
    ...item,
  };
}

/** Enriches all items in a paginated response with numeric ids. */
export function formatPaginatedResult<T extends { url: string }>(
  page: Paginated<T>,
  resource?: string,
) {
  return {
    count: page.count,
    next: page.next,
    previous: page.previous,
    results: page.results.map((item) => enrichWithId(item, resource)),
  };
}
