# SEC-05 oracle

Fixture extracted from PR [larchanka/swapi-mcp-example#3](https://github.com/larchanka/swapi-mcp-example/pull/3) at commit `a1c6b859e12dddffb3ce9c48e3f165fca650ffe4` (initial expansion PR without `.review/rules.md`).

## Defect

In `src/api-client.ts`, `getResource` accepts an external parameter `idOrUrl` (from MCP tools such as `get_person`, `get_planet`, `get_starship`, etc.) and performs an outbound `request<T>(raw)` when the string starts with `http://` or `https://` without origin verification against `baseUrl`. This allows SSRF to arbitrary hosts.

Ground truth anchor: `src/api-client.ts:110` (`return this.request<T>(raw);`), severity `critical`, category `security`, expected verdict `blocking`.
