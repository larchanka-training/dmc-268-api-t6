---
name: pull-request
description: This skill should be used when committing the current git changes on a feature branch — a new one cut from `main`, or the one already checked out — pushing to the remote, and opening a GitHub pull request with the `gh` CLI.
metadata:
  version: 1.0.0
  source: instructor-pack
  adapted-for: any
---

# Create a GitHub Pull Request from Current Changes

<!-- SYNC: mirrored in dmc-268-{ui,api}-t6/.agents/skills/pull-request/SKILL.md -->

## Prerequisites

- `gh` CLI installed and authenticated.
- The branch, commit, and PR conventions in the shared `git-workflow.md` in
  `.agents/rules/` (branch prefixes, commit format, PR sections).

## Instructions

1. **Analyze current changes**:
   - Run `git status`, `git diff`, and `git diff --staged` to inspect what changed.
   - Run `git remote -v` to confirm the project origin.
   - Run `git branch --show-current`. On `main`, pick a branch name per
     `.agents/rules/git-workflow.md`; on a feature branch that already exists (the
     implement-issue skill cuts one), keep its name.
   - Pick a Conventional Commits message — see `commit-message-instructions.md` in this skill
     for the full format.

2. **Create branch, commit, and push**:
   - On `main`: `git checkout -b <branch-name>`. Already on a feature branch (not `main`):
     stay on it, no `git checkout -b`, but first check it has no merged pull request —
     `gh pr list --head <branch> --state merged --json number` must print `[]`. If it lists
     one, the branch belongs to finished work: stop and ask the user.
   - `git add <explicit paths>` for every new file of this change and `git add -u` for
     modified tracked files (never `git add .`).
   - Check `git status` before committing: no unrelated untracked files (`.env`, proof
     scratch, logs).
   - `git commit -m "<commit-message>" && git push -u origin HEAD`.
   - Never `--no-verify`.

3. **Create the pull request**:
   - `gh pr create --title "<type>(<scope>): <subject>" --body "<body>" --base main`.
   - Title: Conventional Commits, ≤72 characters.
   - Body has four sections, in this order: `What`, `Why`, `How to verify`, `Refs`.
   - When the PR is one part of an issue, the first line of `What` names it:
     `Part K of owner/repo#N: …` (`Part K of #N` when the issue is in the PR's own repo; a copy in
     the other repo: `SYNC copy of owner/repo#M (Part K of #N): …`). A bare `#N` is always the
     PR's own repo: write `owner/repo#N` when the issue is in the other repo.
   - Body follows `.github/pull_request_template.md`, including its Before merge checklist
     (`gh pr create --body` bypasses the template).
   - Ask the user whether the PR is ready for review. Not ready: add `--draft`. Ready: ask
     who reviews it and add `--reviewer <login>` — never pick the reviewer yourself.
     The review flow after this point (threads, verdicts, merge) is in
     `docs/CONTRIBUTING.md`.
   - Every issue reference uses `Refs #N` / `Refs owner/repo#N` — never a closing keyword,
     and never link the PR in the Development panel; the tech lead closes the issue after
     checking its AC (`docs/CONTRIBUTING.md`).

4. **Handle authentication errors**: if `gh pr create` fails on auth, ask the user to run
   `gh auth login`.

5. **Report to the user**: share the direct URL of the created pull request.
