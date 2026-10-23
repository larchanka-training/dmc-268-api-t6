# Security

## Raw HTML

`dangerouslySetInnerHTML` is forbidden, with one exception:
`src/components/Markdown.tsx` may render the return value of
`renderTrustedMarkdown()` from `src/lib/markdown.ts`. That function sanitizes with
DOMPurify and the allow-list in `src/lib/sanitize.ts`, so this single use is
reviewed and must not be reported.

# Style

Components are function components with typed props.
