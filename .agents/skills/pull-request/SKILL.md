---
name: pull-request
description: This skill should be used when creating a new feature branch from the current git changes, committing them, pushing to the remote, and opening a GitHub pull request with the `gh` CLI.
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
   - Pick a branch name per `.agents/rules/git-workflow.md`, and a Conventional Commits
     message — see `commit-message-instructions.md` in this skill for the full format.

2. **Create branch, commit, and push**:
   - `git checkout -b <branch-name>`, then `git add <explicit paths>` for every new file of
     this change and `git add -u` for modified tracked files (never `git add .`).
   - Check `git status` before committing: no unrelated untracked files (`.env`, proof
     scratch, logs).
   - `git commit -m "<commit-message>" && git push -u origin <branch-name>`.
   - Never `--no-verify`.

3. **Create the pull request**:
   - `gh pr create --title "<type>(<scope>): <subject>" --body "<body>" --base main`.
   - Title: Conventional Commits, ≤72 characters.
   - Body has four sections, in this order: `What`, `Why`, `How to verify`, `Refs`.
   - Ask the user whether the PR is ready for review. Not ready: add `--draft`. Ready: ask
     who reviews it and add `--reviewer <login>` — never pick the reviewer yourself.
     The review flow after this point (threads, verdicts, merge) is in
     `docs/CONTRIBUTING.md`.
   - Cross-repo references use `Refs owner/repo#N` — never a closing keyword; the issue is
     closed by hand after both linked PRs merge.

4. **Handle authentication errors**: if `gh pr create` fails on auth, ask the user to run
   `gh auth login`.

5. **Report to the user**: share the direct URL of the created pull request.
