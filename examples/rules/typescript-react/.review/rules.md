Template for TypeScript/React frontends; adapt to your project: the paths `src/components/`, `src/hooks/`, `src/api/client.ts`, `src/i18n/index.ts`, `src/lib/markdown.ts`, the masks at the end.

# Architecture

## Data access

Components receive data through hooks from `src/hooks/`, and code outside
`src/api/client.ts` calls the backend only through its `apiClient`, which adds the auth
header and the base URL. Report a direct `fetch` call anywhere else as a correctness
problem: it skips both.

# Correctness

## External data

Treat the result of an external JSON API as `unknown` until its shape is checked.
The assertion `as User` on its own is no check: use the project's validator or a type
guard. Report a response body used through such an assertion as a correctness problem.

## Effects

A `useEffect` that starts a subscription, a timer or a request returns a cleanup that
stops it. Report a missing cleanup as a correctness problem: an answer that arrives
after the component unmounts or its dependencies change overwrites newer state.

## User-visible text

Every user-visible string in `src/components/` goes through `t()` from
`src/i18n/index.ts`, including `aria-label`, `title`, `alt` and `placeholder` values:
screen readers announce them in the user's language. Report a hard-coded string there
as a correctness problem.

# Security

## Raw HTML

`dangerouslySetInnerHTML` receives only the return value of `renderMarkdown()` from
`src/lib/markdown.ts`, which sanitizes it. Report any other value passed to it as a
security problem.

## Tokens in the browser

Auth tokens do not go to `localStorage` or `sessionStorage`, where any injected script
can read them. Report such a write as a security problem.

# Complexity

Report a function or component body that this pull request adds or grows to more than
50 lines or more than 3 levels of nesting as a readability problem, and name the piece
to extract.

# Ignore

Coverage reports, the Storybook build and API types generated from the contract are
not a review source.

```review-ignore
coverage/**
storybook-static/**
src/types/api/**
```
