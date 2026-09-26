# Git workflow

<!-- SYNC: mirrored in dmc-268-{ui,api}-t6/.agents/rules/git-workflow.md -->

Shared across both repos. Do not add lab-only conventions here (no `(prompt: ...)`
links, no `Impact:` blocks) — this file ships to any team using this stack.

## Branches

- `feat/<slug>`, `fix/<slug>`, `docs/<slug>`, `chore/<slug>`, `deps-update-YYYY-MM-DD`.
- Work typed `refactor`/`test`/`ci`/`perf`/`style` uses `chore/`.
- Slug preferably `<issue>-<kebab-case>`.
- Existing branches that predate this rule are grandfathered — do not rename them.
- Base branch is `main`. No direct pushes to `main`.

## Commits

- Conventional Commits (`type(scope): summary`).
- Reference the issue: `(#N)` in the subject, or `Refs #N` in the footer.
- Small, atomic commits — one logical change per commit.
- Never `--no-verify`.
- Rebase on `main` before requesting review.

## Pull requests

- Title: conventional (`type(scope): summary`), ≤72 characters.
- Body has four mandatory sections, in this order — the AI reviewer reads the body
  first:
  - `What`
  - `Why`
  - `How to verify`
  - `Refs`
- Cross-repo references: `Refs owner/repo#N`.
- One approving review and all review threads resolved are required before merge.
- Review threads: the author replies in every thread (the fix with its commit SHA, or
  the reason for not changing it) and never resolves a thread — whoever opened it
  (normally the reviewer) does.
  Full review flow (roles, board statuses, disputes): `docs/CONTRIBUTING.md`.
- Merging: squash or rebase only — the `main` ruleset rejects merge commits. Squash
  suits single-purpose branches; rebase keeps the individual commits of a
  multi-commit feature branch — pick whichever keeps history readable for that PR.
