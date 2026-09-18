---
name: planning-and-task-breakdown
description: This skill should be used when there is a spec or clear requirements and the work needs to be broken into implementable tasks. Use when a task feels too large to start, when scope needs estimating, or when parallel work is possible.
metadata:
  version: 1.0.0
  source: instructor-pack
  adapted-for: any
---

# Planning and Task Breakdown

<!-- SYNC: mirrored in dmc-268-{ui,api}-t6/.agents/skills/planning-and-task-breakdown/SKILL.md -->

## Overview

Decompose work into small, verifiable tasks with explicit acceptance
criteria. Good task breakdown is the difference between an agent that
completes work reliably and one that produces a tangled mess. Every task
should be small enough to implement, test, and verify in a single focused
session.

## When to Use

- There is a spec and it needs breaking into implementable units
- A task feels too large or vague to start
- Work needs to be parallelized across multiple agents or sessions
- Scope needs to be communicated to a human
- The implementation order isn't obvious

**When NOT to use:** Single-file changes with obvious scope, or when the
spec already contains well-defined tasks.

## The Planning Process

### Step 1: Enter Plan Mode

Before writing any code, operate in read-only mode:

- Read the spec and relevant codebase sections
- Identify existing patterns and conventions
- Map dependencies between components
- Note risks and unknowns

**Do NOT write code during planning.** The output is a plan document saved
to `docs/plans/<issue>-plan.md` and a task list saved to
`docs/plans/<issue>-todo.md`, not implementation.

### Step 2: Identify the Dependency Graph

Map what depends on what:

```
Data model / schema
    │
    ├── API models/types
    │       │
    │       ├── API endpoints
    │       │       │
    │       │       └── Client API layer
    │       │               │
    │       │               └── UI components
    │       │
    │       └── Validation logic
    │
    └── Seed data / migrations
```

Implementation order follows the dependency graph bottom-up: build
foundations first.

### Step 3: Slice Vertically

Instead of building all the data layer, then all the API, then all the UI —
build one complete feature path at a time:

**Bad (horizontal slicing):**

```
Task 1: Build the entire data model
Task 2: Build all API endpoints
Task 3: Build all UI components
Task 4: Connect everything
```

**Good (vertical slicing):**

```
Task 1: User can create a repository entry (model + endpoint + UI)
Task 2: User can trigger a run (schema + endpoint + UI)
Task 3: User can view a run's findings (query + endpoint + UI)
Task 4: User can comment inline on a diff line (schema + endpoint + UI)
```

Each vertical slice delivers working, testable functionality.

### Step 4: Write Tasks

Each task follows this structure:

```markdown
## Task [N]: [Short descriptive title]

**Description:** One paragraph explaining what this task accomplishes.

**Acceptance criteria:**

- [ ] [Specific, testable condition]
- [ ] [Specific, testable condition]

**Verification:**

- [ ] Tests pass, build succeeds, lint/typecheck clean — the test/build
      commands from the stack rules file in `.agents/rules/`
- [ ] Manual check: [description of what to verify]

**Dependencies:** [Task numbers this depends on, or "None"]

**Files likely touched:**

- `path/to/file`
- `path/to/test`

**Estimated scope:** [Small: 1-2 files | Medium: 3-5 files | Large: 5+ files]
```

### Step 5: Order and Checkpoint

Arrange tasks so that:

1. Dependencies are satisfied (build foundation first)
2. Each task leaves the system in a working state
3. Verification checkpoints occur after every 2-3 tasks
4. High-risk tasks are early (fail fast)

Add explicit checkpoints:

```markdown
## Checkpoint: After Tasks 1-3

- [ ] All tests pass
- [ ] Build succeeds without errors
- [ ] Core user flow works end-to-end
- [ ] Review with human before proceeding
```

## Task Sizing Guidelines

| Size | Files | Scope                            | Example                      |
| ---- | ----- | -------------------------------- | ---------------------------- |
| XS   | 1     | Single function or config change | Add a validation rule        |
| S    | 1-2   | One component or endpoint        | Add a new API endpoint       |
| M    | 3-5   | One feature slice                | Repository registration flow |
| L    | 5-8   | Multi-component feature          | Run list with filtering      |
| XL   | 8+    | **Too large — break it down**    | —                            |

If a task is L or larger, it should be broken into smaller tasks. An agent
performs best on S and M tasks.

**When to break a task down further:**

- It would take more than one focused session (roughly 2+ hours of agent
  work)
- The acceptance criteria can't be described in 3 or fewer bullet points
- It touches two or more independent subsystems (e.g., ingestion and review)
- The task title has "and" in it (a sign it is two tasks)

## Output Files

- **Plan document:** save the implementation plan to
  `docs/plans/<issue>-plan.md`.
- **Task list:** save the checklist-style task list to
  `docs/plans/<issue>-todo.md`.

Create the `docs/plans/` directory if it does not exist. These are the paths
the dev-loop agent and other downstream tooling expect.

## Plan Document Template

```markdown
# Implementation Plan: [Feature/Project Name]

## Overview

[One paragraph summary of what we're building]

## Architecture Decisions

- [Key decision 1 and rationale]
- [Key decision 2 and rationale]

## Task List

### Phase 1: Foundation

- [ ] Task 1: ...
- [ ] Task 2: ...

### Checkpoint: Foundation

- [ ] Tests pass, builds clean

### Phase 2: Core Features

- [ ] Task 3: ...
- [ ] Task 4: ...

### Checkpoint: Core Features

- [ ] End-to-end flow works

### Phase 3: Polish

- [ ] Task 5: ...
- [ ] Task 6: ...

### Checkpoint: Complete

- [ ] All acceptance criteria met
- [ ] Ready for review

## Risks and Mitigations

| Risk   | Impact         | Mitigation |
| ------ | -------------- | ---------- |
| [Risk] | [High/Med/Low] | [Strategy] |

## Open Questions

- [Question needing human input]
```

## Parallelization Opportunities

When multiple agents or sessions are available:

- **Safe to parallelize:** Independent feature slices, tests for
  already-implemented features, documentation
- **Must be sequential:** Schema/data-model migrations, shared state
  changes, dependency chains
- **Needs coordination:** Features that share an API contract (define the
  contract first, then parallelize)

## Common Rationalizations

| Rationalization                | Reality                                                                                      |
| ------------------------------ | -------------------------------------------------------------------------------------------- |
| "I'll figure it out as I go"   | That's how you end up with a tangled mess and rework. 10 minutes of planning saves hours.    |
| "The tasks are obvious"        | Write them down anyway. Explicit tasks surface hidden dependencies and forgotten edge cases. |
| "Planning is overhead"         | Planning is the task. Implementation without a plan is just typing.                          |
| "I can hold it all in my head" | Context windows are finite. Written plans survive session boundaries and compaction.         |

## Red Flags

- Starting implementation without a written task list
- Tasks that say "implement the feature" without acceptance criteria
- No verification steps in the plan
- All tasks are XL-sized
- No checkpoints between tasks
- Dependency order isn't considered

## Verification

Before starting implementation, confirm:

- [ ] Every task has acceptance criteria
- [ ] Every task has a verification step
- [ ] Task dependencies are identified and ordered correctly
- [ ] No task touches more than ~5 files
- [ ] Checkpoints exist between major phases
- [ ] The human has reviewed and approved the plan

## Definition of Done

Acceptance criteria are per-task and answer "did we build the right thing?".
They sit on top of the project-wide Definition of Done: the gates in the
stack rules file in `.agents/rules/` — every one green before a task counts
as done.
