# API access

## One HTTP client

Code under `src/` calls the backend only through `apiClient` from
`src/api/client.ts`. The client adds the CSRF header and the base URL and retries
5xx answers; a direct `fetch` call skips all three and fails behind the staging
proxy. Report a direct `fetch` call as a correctness problem.

## Payloads

Parse every response body with the guards in `src/api/types.ts` before use.

# Ignore

```review-ignore
dist/**
**/*.min.js
```
