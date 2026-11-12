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
   - Check open pull requests (`gh pr list --state open --limit 200 --json number,title,headRefName,files`)
     for overlapping modified files or duplicate scope on the same issue/part. If another
     open PR edits any of the same files, post a heads-up comment there (`AGENTS.md`) and
     note any stacking/overlap in `What`.
   - Confirm the branch is rebased onto current `origin/main` (unless intentionally stacked on
     an open parent PR, in which case keep the PR `--draft` until the parent merges).
   - Run `git branch --show-current`. On `main`, pick a branch name per
     `.agents/rules/git-workflow.md`; on any other checkout step 2 decides whether it can
     take this change (the implement-issue skill cuts the task branch before this skill runs).
   - Pick a Conventional Commits message (subject ≤72 characters, zero closing keywords) — see
     `commit-message-instructions.md` in this skill for the full format.

2. **Create branch, commit, and push** — by what `git branch --show-current` printed, before
   staging anything:
   - Empty (detached HEAD): stop and ask the user — there is no branch to commit on.
   - `main`: `git checkout -b <branch-name>`.
   - A task branch — the name matches `.agents/rules/git-workflow.md`:
     `^(feat|fix|docs|chore)/[a-z0-9]+(?:[._-][a-z0-9]+)*(?<!\.lock)$` or
     `^deps-update-[0-9]{4}-[0-9]{2}-[0-9]{2}$`. Stay on it, no `git checkout -b`, and list its
     pull requests: `gh pr list --head <branch> --state all --json number,state,url`.
     - `[]`: commit if something is staged, then push; step 3 opens the pull request.
     - An `OPEN` one: this change updates that pull request. Ask the user before pushing — a
       push that changes the diff dismisses its approve — then commit if something is staged and
       push, and in step 3 do not run `gh pr create`.
     - Only `MERGED` / `CLOSED` ones: the branch belongs to finished work — stop and ask the
       user.
   - Any other name (`staging`, `release`, a branch that predates the naming rule): stop and
     ask the user; go on only when the user confirms it is this change's branch
     (grandfathered, `git-workflow.md`), then list its pull requests as for a task branch.
   - `git add <explicit paths>` for every new file of this change and `git add -u` for
     modified tracked files (never `git add .`).
   - Check `git status` before committing: no unrelated untracked files (`.env`, proof
     scratch, logs).
   - Commit only when something is staged (`git diff --cached --quiet` exits 1): a tree that is
     already committed, as after the implement-issue loop, stages nothing and needs no new commit.
     `git commit -m "<commit-message>"` when something is staged, then `git push -u origin HEAD`.
   - Never `--no-verify`.

3. **Create the pull request**:
   - Step 2 found an open pull request on the branch: no `gh pr create`. If its body no longer
     describes the whole pull request, update it
     (`gh api -X PATCH repos/<owner>/<repo>/pulls/<number> -F body=@<file>`), report its URL
     and stop here.
   - `gh pr create --title "<type>(<scope>): <subject>" --body "<body>" --base main`.
   - Title: Conventional Commits, ≤72 characters.
   - Body has four sections, in this order: `What`, `Why`, `How to verify`, `Refs`.
   - When the PR is one part of an issue, the first line of `What` names it:
     `Part K of owner/repo#N: …` (`Part K of #N` when the issue is in the PR's own repo; a copy in
     the other repo: `SYNC copy of owner/repo#M (Part K of #N): …`). A bare `#N` is always the
     PR's own repo: write `owner/repo#N` when the issue is in the other repo.
   - Body follows `.github/pull_request_template.md`, including its Before merge checklist
     (`gh pr create --body` bypasses the template).
   - Ask the user whether the PR is ready for review. Keep it as `--draft` if any AC is
     partial/blocked, required runtime checks did not run, or the branch is stacked on an
     unmerged parent PR; never fabricate verification outputs in `How to verify`. Ready: ask
     who reviews it and add `--reviewer <login>` — never pick the reviewer yourself.
     The review flow after this point (threads, verdicts, merge) is in
     `docs/CONTRIBUTING.md`.
   - Pre-flight check before `gh pr create` or marking Ready: verify title ≤72 characters,
     every commit subject ≤72 characters, zero closing keywords (`Closes`/`Fixes`/`Resolves`
     with `#N` or `owner/repo#N`) across title, body, and commit messages, and zero
     Development-panel links; run `.github/scripts/check_pr_hygiene.py --rev-range origin/main..HEAD`
     (or pass `--title <title> --body-file <file>`).
   - Every issue reference uses `Refs #N` / `Refs owner/repo#N` — never a closing keyword,
     and never link the PR in the Development panel; the tech lead closes the issue after
     checking its AC (`docs/CONTRIBUTING.md`).

4. **Handle authentication errors**: if `gh pr create` fails on auth, ask the user to run
   `gh auth login`.

5. **Report to the user**: share the direct URL of the created pull request.
