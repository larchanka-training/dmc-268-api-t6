# Frontend

## User-visible text

Every user-visible string in `src/components/` goes through `t()` from
`src/i18n/index.ts`. Hard-coded English in JSX is a defect: the product ships in
German and Russian as well, and the translation check only sees `t()` keys.

## Data access

Components receive data through hooks from `src/hooks/`; they do not call
`fetch` themselves.
