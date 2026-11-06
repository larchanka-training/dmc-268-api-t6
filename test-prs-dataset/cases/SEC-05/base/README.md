# SWAPI MCP Server

A [Model Context Protocol](https://modelcontextprotocol.io) (MCP) server that exposes a subset of the
[SWAPI — The Star Wars API](https://swapi.dev/) as MCP tools, so an AI agent can answer questions
about Star Wars episodes and their characters without knowing anything about REST URLs.

Runs locally over **stdio transport** — no HTTP server of its own.

## Scope: two tools

Per requirements, the server exposes exactly two tools:

| MCP tool | Source endpoints | Purpose |
|---|---|---|
| `list_films` | `GET /films/` | List all episodes (sorted, concise summaries) |
| `get_film_characters` | `GET /films/{id}/` → `GET /people/{id}/` (for each link) | Get all characters of an episode, resolved to names and key attributes |

SWAPI's `characters` field on a film contains only URLs, so `get_film_characters` follows those
hypermedia links and resolves each character to `{ id, name, gender, birth_year, height, mass,
hair_color, eye_color, url }`. This is link traversal, not a new endpoint.

### Assumptions

1. **`film_id` vs `episode_id`.** SWAPI film resource ids do not match episode numbers
   (film `1` = Episode IV, film `4` = Episode I). `get_film_characters` accepts *either*
   `film_id` (resource id) *or* `episode_id` (episode number); exactly one must be provided.
2. **Trimmed `list_films` output.** The full film objects include a long `opening_crawl`;
   the tool omits it and returns a concise episode summary instead (the raw field set is
   available via `GET /films/{id}/` in the API itself).
3. **Partial character failures are tolerated.** If a single character link fails, the tool
   returns an `{ id, url, error }` entry for it instead of failing the whole call.
4. **Wookiee encoding** (`?format=wookiee`) is intentionally not exposed.

## Requirements

- Node.js **≥ 18.17** (built and tested on Node 22)
- npm ≥ 9

## Installation

```bash
cd example
npm install
npm run build
```

## Configuration

All configuration is via environment variables — no code changes needed.

| Variable | Required | Default | Description |
|---|---|---|---|
| `SWAPI_BASE_URL` | no | `https://swapi.dev/api/` | Base URL of the SWAPI REST API |
| `SWAPI_TIMEOUT_MS` | no | `30000` | Per-request timeout, ms (1–600000) |

**Authentication: SWAPI requires none.** It is a fully open, read-only API (rate-limited to
10,000 requests/day per IP). There are no credentials to configure, and no credentials are
ever logged or returned by this server.

## Build & run

```bash
npm run build     # TypeScript -> dist/
npm run start     # run the built server via the root index.js shim
npm run dev       # run directly from TypeScript (tsx), no build step
npm test          # unit tests (vitest, no network or real API needed)
npm run typecheck # strict TypeScript check
```

## Agent configuration

Example for a local AI coding agent (path is absolute on purpose):

```json
{
  "mcpServers": {
    "swapi": {
      "command": "node",
      "args": ["/Users/mikhaillarchanka/Projects/dmc-268/example/index.js"],
      "env": {
        "SWAPI_BASE_URL": "https://swapi.dev/api/",
        "SWAPI_TIMEOUT_MS": "30000"
      }
    }
  }
}
```

`env` is optional — defaults work against the public API. The server speaks MCP on
**stdin/stdout**; the only log line goes to **stderr** to keep the protocol channel clean.

## Tools reference

### `list_films`

Lists every Star Wars episode, sorted by episode number.

- **Input**: none (`{}`)
- **Output**:

```json
{
  "total_episodes": 6,
  "episodes": [
    {
      "film_id": 1,
      "episode_id": 4,
      "title": "A New Hope",
      "release_date": "1977-05-25",
      "director": "George Lucas",
      "producer": "Gary Kurtz, Rick McCallum",
      "character_count": 18
    }
  ]
}
```

### `get_film_characters`

All characters appearing in one episode, resolved to names and key attributes.

- **Input**: exactly one of
  - `film_id` — integer 1–6, SWAPI film resource id (see `list_films`)
  - `episode_id` — integer 1–6, the episode number (e.g. `4` for "A New Hope")
- **Output**:

```json
{
  "film": { "film_id": 1, "episode_id": 4, "title": "A New Hope" },
  "character_count": 18,
  "characters": [
    {
      "id": 1,
      "name": "Luke Skywalker",
      "gender": "Male",
      "birth_year": "19 BBY",
      "height": "172",
      "mass": "77",
      "hair_color": "Blond",
      "eye_color": "Blue",
      "url": "https://swapi.dev/api/people/1/"
    }
  ]
}
```

### Example agent flow

1. Agent calls `list_films` → sees Episode V has `film_id: 2`.
2. Agent calls `get_film_characters` with `{"film_id": 2}` (or `{"episode_id": 5}`) →
   gets named characters of "The Empire Strikes Back".

## Error handling

- **API HTTP errors** → tool result with `isError: true` and a concise message, e.g.
  `SWAPI request failed: HTTP 404. Not found` (HTTP status + SWAPI `detail`, never a stack trace).
- **Unknown episode number** → `Episode 9 was not found. SWAPI contains episodes 1-6.`
- **Invalid arguments** (neither/both of `film_id` and `episode_id`, or out of range) →
  MCP `InvalidParams` error (`-32602`) with an explanation.
- **Timeout / network failure** → `isError: true` result with `timed out after Nms` or the
  underlying transport reason; HTTP status is reported as part of the message.
- **Invalid configuration** (bad `SWAPI_BASE_URL`, non-integer timeout) → server refuses
  to start with a `ConfigError` naming the variable.

No secrets exist in this system, and request headers are never echoed back to the agent.

## Testing

```bash
npm test
```

All tests are **unit tests** that stub the global `fetch` and run the MCP server over an
in-memory transport — no network access and no real SWAPI credentials are needed
(SWAPI has no credentials anyway). Covered:

- API client: URL construction (`format=json`, base URL), headers, pagination parsing,
  404/500 error translation, network failure and timeout wrapping
- Config: defaults, base URL normalization, validation errors
- MCP tools (end-to-end through `McpServer` + `Client` over `InMemoryTransport`):
  `list_films` sorting/shape, `get_film_characters` by `film_id` and by `episode_id`,
  argument validation, 404 handling

Integration tests against live `swapi.dev` are intentionally not part of the suite to keep
it deterministic and offline; the smoke command below exercises the real API instead.

## Development

```bash
npm run dev          # tsx watch-free single run; pipe JSON-RPC for manual testing
npm run typecheck    # tsc --noEmit, strict mode
```

Manual smoke test against the real API (lists tools, then calls `list_films`):

```bash
printf '%s\n%s\n%s\n%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"smoke","version":"0.0.0"}}}' \
  '{"jsonrpc":"2.0","method":"notifications/initialized"}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' \
  '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"list_films","arguments":{}}}' \
  | node index.js
```

Project layout:

```text
index.js            entry shim -> dist/index.js
src/
  index.ts          stdio wiring
  server.ts         McpServer factory + tool registration
  api-client.ts     SWAPI HTTP client (fetch, timeout, error mapping)
  config.ts         env parsing & validation
  tools/films.ts    list_films, get_film_characters
  types/swapi.ts    SWAPI response types + URL id parsing
test/               vitest unit tests (fetch is stubbed)
```

## Troubleshooting

- **`node index.js` prints nothing / exits immediately** — run `npm run build` first;
  the shim loads `dist/index.js`.
- **Agent cannot connect** — check the path in the MCP config is absolute and points to
  this directory; logs go to stderr, stdout is the protocol (never add `console.log`).
- **HTTP 404 for a film** — SWAPI film ids are 1–6; use `list_films` to discover ids, or
  pass `episode_id` instead.
- **Rate limiting** — 10,000 requests/day per IP; each `get_film_characters` call makes
  1 + N requests (film + one per character).
- **`ConfigError` on startup** — verify `SWAPI_BASE_URL` (must be a valid http(s) URL) and
  `SWAPI_TIMEOUT_MS` (positive integer) in the agent's `env` block.
