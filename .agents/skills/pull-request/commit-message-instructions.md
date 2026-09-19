# Commit Message Guidelines

<!-- SYNC: mirrored in dmc-268-{ui,api}-t6/.agents/skills/pull-request/commit-message-instructions.md -->

Aligned with the branch/commit/PR rules in `.agents/rules/git-workflow.md` — read that file
first; this one only expands on subject/body wording.

## Core principles

- **Clarity**: messages must clearly explain _what_ changed and _why_.
- **Security**: never include secrets, API keys, credentials, or PII in commit messages.
- **Consistency**: follow the Conventional Commits specification strictly.

## Format structure

```text
<type>(<scope>)[!]: <subject>

<body>

<footer>
```

## 1. Header line

The header must be **72 characters or less**.

For **breaking changes**, append `!` after the type/scope (e.g. `feat!: drop support for
Node 12`) to signal a major version bump.

### Types

Select the most specific type:

- `feat` — new feature (MINOR in semantic versioning)
- `fix` — bug fix (PATCH in semantic versioning)
- `docs` — documentation changes
- `style` — formatting only, no logic change
- `refactor` — code restructuring, no functional or API change
- `perf` — performance improvements
- `test` — adding or updating tests
- `chore` — maintenance, dependencies, build scripts
- `ci` — CI/CD configuration changes
- `sec` — security fixes or improvements
- `revert` — revert a previous commit

### Scope (optional)

Use the affected module/component (folder) name, lowercase, kebab-case.

**Constraints**:

- Select exactly **one** scope that best represents the primary change.
- If a change touches more than two distinct scopes, omit the scope or use `core`.
- Do not use comma-separated scopes.

### Subject

- Use **imperative mood** ("Add feature" not "Added feature").
- Lowercase, imperative subject after the type (`feat(auth): add …`).
- Do not end with a period.
- Be concise but descriptive.
- Reference the issue with `(#N)` in the subject when there is room, or in the footer instead.

## 2. Body

- Mandatory for all `feat`, `fix`, and complex `refactor` changes.
- Separate from the subject with a blank line.
- Wrap lines at 72 characters.
- Explain the motivation for the change and contrast with the previous behaviour.
- Use `-` bullet points for lists.

## 3. Footer

- Reference the issue: `Refs #N` (same repo) or `Refs owner/repo#N` (cross-repo).
- Never a closing keyword (`Closes`/`Fixes`) on a cross-repo reference — the issue is closed
  by hand after every linked PR merges.
- Mention breaking changes: `BREAKING CHANGE: <description>`.

### Example

```text
feat(auth): add login rate limiting (#42)

This change adds rate limiting on the login endpoint to prevent
brute force attacks. Users are locked out after 5 failed attempts.

Refs #42
```
