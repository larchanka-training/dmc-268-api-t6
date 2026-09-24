---
name: code-review
description: This skill should be used when reviewing the changes since a fixed point (commit, branch, tag, or merge-base) along two axes — Standards (does the code follow this repo's documented coding standards?) and Spec (does the code match what the originating issue/PRD asked for?) — running both reviews in parallel sub-agents and reporting them side by side.
metadata:
  version: 1.0.0
  source: instructor-pack
  adapted-for: any
---

# Code Review

<!-- SYNC: mirrored in dmc-268-{ui,api}-t6/.agents/skills/code-review/SKILL.md -->

Two-axis review of the diff between `HEAD` and a fixed point the user supplies:

- **Standards** — does the code conform to this repo's documented coding standards?
- **Spec** — does the code faithfully implement the originating issue / PRD / spec?

Both axes run as **parallel sub-agents** so they don't pollute each other's context, then this skill aggregates their findings.

The issue tracker is GitHub; fetch issues with `gh issue view <n> --json title,body,comments` (a bare `gh issue view <n>` fails in this org on the Projects (classic) deprecation).

## Analysis order & comment format

<!-- SYNC: wording shared with dmc-268-api-t6/review/prompts/review.system.v1.md -->

Work every finding through this order: security → correctness → performance → readability. Skip anything tooling already enforces (formatter, linter, type checker).

Comment skeleton: what changed → how it differs from earlier similar changes in this file → why it was done that way → what breaks → what to do.

Summary triple: one sentence on the main problem → what is done well → effort estimate.

## Process

### 1. Pin the fixed point

Whatever the user said is the fixed point — a commit SHA, branch name, tag, `main`, `HEAD~5`, etc. If they didn't specify one, ask for it.

Capture the diff command once: `git diff <fixed-point>...HEAD` (three-dot, so the comparison is against the merge-base). Also note the list of commits via `git log <fixed-point>..HEAD --oneline`.

Before going further, confirm the fixed point resolves (`git rev-parse <fixed-point>`) and the diff is non-empty. A bad ref or empty diff should fail here — not inside two parallel sub-agents.

### 2. Identify the spec source

Look for the originating spec, in this order:

1. A path the user passed as an argument.
2. Issue references in the commit messages (`#123`, `Closes #45`, etc.) — fetch via `gh issue view <n> --json title,body,comments`. If the commits reference several distinct issues, fetch all of them and name each in the Spec prompt.
3. A PRD/spec file under `docs/` (including `docs/plans/`) or `specs/` matching the branch name or feature.
4. If nothing is found, ask the user where the spec is. If they say there isn't one, the **Spec** sub-agent will skip and report "no spec available".

### 3. Identify the standards sources

The stack rules file in `.agents/rules/` and `AGENTS.md`, plus anything else the repo documents, such as `CONTRIBUTING.md`.

On top of whatever the repo documents, the Standards axis always carries the **smell baseline** below — a fixed set of Fowler code smells (_Refactoring_, ch.3) that applies even when a repo documents nothing. Two rules bind it:

- **The repo overrides.** A documented repo standard always wins; where it endorses something the baseline would flag, suppress the smell.
- **Always a judgement call.** Each smell is a labelled heuristic ("possible Feature Envy"), never a hard violation — and, like any standard here, skip anything tooling already enforces.

Each smell reads _what it is_ → _how to fix_; match it against the diff:

- **Mysterious Name** — an unrevealing function/variable/type name. → rename it; murky design if no honest name comes.
- **Duplicated Code** — the same logic shape in more than one hunk or file. → extract the shared shape, call it from both.
- **Feature Envy** — a method reaching into another object's data more than its own. → move the method onto the data it envies.
- **Data Clumps** — the same few fields or params keep travelling together. → bundle them into one type, pass that.
- **Primitive Obsession** — a primitive standing in for a domain concept. → give the concept its own small type.
- **Repeated Switches** — the same `switch`/`if`-cascade on the same type recurs. → replace with polymorphism, or one shared map.
- **Shotgun Surgery** — one logical change forces scattered edits across many files. → gather what changes together into one module.
- **Divergent Change** — one file or module is edited for several unrelated reasons. → split so each module changes for one reason.
- **Speculative Generality** — abstraction or hooks added for needs the spec doesn't have. → delete it; inline back until a real need shows.
- **Message Chains** — long `a.b().c().d()` navigation the caller shouldn't depend on. → hide the walk behind one method.
- **Middle Man** — a class or function that mostly just delegates onward. → cut it, call the real target direct.
- **Refused Bequest** — a subclass that ignores or overrides most of what it inherits. → drop the inheritance, use composition.
- **Complex Logic** — excessive cyclomatic complexity. → break into smaller functions; use guard clauses; name the conditions.
- **Missing Test Coverage** — new/modified logic without corresponding test updates. → add or update unit tests, cover edge and failure paths.

### 4. Spawn both sub-agents in parallel

Send a single message with two `Agent` tool calls. Use the `general-purpose` subagent for both. Other harnesses: see `.agents/README.md` § Sub-agents across harnesses.

**Standards sub-agent prompt** — include: the full diff command and commit list; the standards-source files found in step 3 **plus the smell baseline pasted in full** (the sub-agent has no other access to it); and the brief: "Report — per file/hunk where relevant — (a) every place the diff violates a documented standard: cite the standard (file + rule); and (b) any baseline smell you spot: name it and quote the hunk. Distinguish hard violations from judgement calls — documented-standard breaches can be hard, but baseline smells are always judgement calls, and a documented repo standard overrides the baseline. Skip anything tooling enforces. Under 400 words."

**Spec sub-agent prompt** — include: the diff command and commit list; the path or fetched contents of the spec; and the brief: "Report: (a) requirements the spec asked for that are missing or partial; (b) behaviour in the diff that wasn't asked for (scope creep); (c) requirements that look implemented but where the implementation looks wrong. Quote the spec line for each finding. Under 400 words."

If the spec is missing, skip the Spec sub-agent and note this in the final report.

### 5. Aggregate

Present the two reports under `## Standards` and `## Spec` headings, verbatim or lightly cleaned. Do **not** merge or rerank findings — the two axes are deliberately separate.

End with a one-line summary: total findings per axis, and the worst issue _within each axis_ (if any). Don't pick a single winner across axes.

## Why two axes

A change can pass one axis and fail the other: code that follows every standard but implements the wrong thing is **Standards pass, Spec fail**; code that does exactly what the issue asked but breaks the project's conventions is **Spec pass, Standards fail**. Reporting them separately stops one axis from masking the other.
