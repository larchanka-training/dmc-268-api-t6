# Checkout web client

TypeScript, Vite and Vitest. Run `npm test` and `npm run lint` before pushing.

## Duplication

Do not duplicate logic. A second copy of a block goes into a shared helper, and
test setup shared by two tests goes into a factory in `src/testing/factories.ts`.
Report a duplicated block as a readability problem.
