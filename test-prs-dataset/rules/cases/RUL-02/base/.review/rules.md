# Frontend

## User-visible text

Every user-visible string in `src/components/` goes through `t()` from
`src/i18n/index.ts`, including `aria-label`, `title`, `alt` and `placeholder`
values: screen readers announce them in the user's language. Report a hard-coded
English string there as a correctness problem: the product also ships in German
and Russian, and the translation check only sees `t()` keys.

## Data access

Components receive data through hooks from `src/hooks/`; they do not call
`fetch` themselves.
