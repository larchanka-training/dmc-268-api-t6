# SEC-06 oracle

Fixture extracted from PR [larchanka/swapi-mcp-example#3](https://github.com/larchanka/swapi-mcp-example/pull/3) at commit `8bc8b5d7f89643405f1feadf516682579b72f6d0` (with repository rules `.review/rules.md`).

## Defect

Contains the same SSRF vulnerability in `src/api-client.ts:110` as SEC-05, but in the presence of `.review/rules.md` explicitly requiring SSRF prevention and URL origin validation.

Ground truth anchor: `src/api-client.ts:110` (`return this.request<T>(raw);`), severity `critical`, category `security`, expected verdict `blocking`.
