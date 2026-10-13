# PR #132 round-three final proof (#113)

This proof supplements the [original plan](113-plan.md) and [todo](113-todo.md),
[review plan](113-review-plan.md) and [todo](113-review-todo.md), and
[round-three plan](113-review-round3-plan.md) and [todo](113-review-round3-todo.md).

## Verified scope and focused evidence

Round three changes three existing integration-test files and portable contract/evidence
documentation. Application, migration and dependency files remain unchanged from
published head `83a36290192369197f6f72f5b84a65f1add6d1d1`. Full pytest ran on
the round-three source snapshot based on `834dd24c554e62d725060ecbaf19901dc709542e`.
The publication branch was subsequently rebased onto #127 main
`488475e4a3d48732d1d15860ee5718dfb83c10e5`.

[A proof](113-review-round3-a.md): 31 passed, 2 warnings in 19.26s, zero skips;
six isolated implementation mutations failed as expected before exact byte restoration.
[B proof](113-review-round3-b.md): 82 passed, 2 warnings in 40.52s, zero skips;
five isolated mutations failed before exact restoration. [C proof](113-review-round3-c.md)
records the receipt limitation, PG-only deferral, before-merge writer shutdown,
portable historical references and the #108/#134/#138 integration evidence.

Fresh independent Standards and Spec review of the frozen base-to-working-tree delta
reported zero actionable findings on each axis. Reviewers made no changes or test runs.

## Historical verification checkpoint

All temporary mutation windows ended before the coordinator's full repository gates.
The coordinator fingerprinted 389 tracked Python files for subsequent hook/commit
integrity comparison. Commands ran in `<worktree>` with disposable PostgreSQL 17
`<dev-db>` and RabbitMQ 4 `<dev-broker>`:

- `uv run ruff check .`: All checks passed.
- `uv run ruff format --check .`: 385 files already formatted.
- `uv run mypy .`: Success: no issues found in 382 source files.
- `TEST_DATABASE_URL='<dev-db>' TEST_RABBITMQ_URL='<dev-broker>' uv run pytest -rs`:
  **2886 passed, 2 existing deprecation warnings in 534.49s (8m54s), zero failures and zero skips.**

`git diff --check` passed. Main advanced to `488475e` through #127 before
final publication; the resulting documentation rebase is described below.
All 389 tracked Python hashes matched the pre-gate snapshot. Aggregate
SHA-256 (canonical sorted path-to-hash JSON):
`dacbccbbde6ff191881e7000c339c3fbbbb1fece19976734530dcdb6de10b2fe`. The tested source snapshot precedes publication metadata.
Configured staged-file hooks passed (text checks, Ruff lint/format and mypy);
all 389 tracked Python hashes still matched after hooks. No hook modified a file.
The resulting publication and review replies are recorded in the existing
[PR #132 timeline](https://github.com/larchanka-training/dmc-268-api-t6/pull/132).
The rebase changed only `.agents/rules/backend.md`, SYSTEM_DESIGN and the new
`115-plan.md` / `115-todo.md` inherited from #127. Conflict resolution preserves
main's broader AuthScope description and this PR's paused/App-unavailable rerun 409
contract. All 389 Python files and the complete application, migration, test,
dependency and runtime-configuration trees remain byte-identical to the tested
snapshot. Ruff check passed again after rebase, format reported 385 files and mypy
reported 382 source files. Full pytest was not rerun after this docs-only rebase;
the 2886-pass evidence above applies to its unchanged source/runtime tree.
This round adds no production, migration or dependency change.

## External prerequisites at the round-three checkpoint

#127 had merged and was included through main `488475e`. #125/#126/#121 were
still open; their integration remained required before #132. Rebase #132 last after
those companions, preserve #121
`RunTriggerOutcome` and mandatory keyword-only `expected_installation_external_id`,
#127 fail-closed `AuthScope` and #125/#126 deployment contracts, verify the waiting
old CI receipt outcome, then rerun combined-head gates. Current-head human approval,
reviewer-resolved threads and required CI remain necessary. Stop every old Run writer
before merge and coordinate timing with axyi because main deployment is automatic.
No companion merge, thread resolution, service stop, deployment or issue closure
is part of this proof.

## Subsequent frontend follow-up

Rerun 409 wording is tracked in [UI #105](https://github.com/larchanka-training/dmc-268-ui-t6/issues/105),
with the related [API #106 reference](https://github.com/larchanka-training/dmc-268-api-t6/issues/106#issuecomment-6131037958).
This follow-up does not change the historical round-three test evidence above.
