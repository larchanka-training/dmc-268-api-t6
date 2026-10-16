# Implementation Plan: rerun 409/422 contract alignment (#162)

## Overview

Align the existing rerun HTTP contract and its documentation with runtime behavior;
do not change application behavior. Specification:
[#162](https://github.com/larchanka-training/dmc-268-api-t6/issues/162).
Fixed review base: `e904b1eea9ef731c06bfc947f361721b1609d29f` (`origin/main` at kickoff).
Branch: `docs/162-rerun-conflict-contract`. The user approved the implementation
plan in advance and requested a Russian PR description.

## Architecture decisions

- Keep `app/main.py` and the rerun use case/store unchanged. The store already
  checks closed PR/active Run, then candidate availability, then effective
  repository availability. Missing candidate returns 422 before paused/unavailable
  repository checks; paused repository without active configuration therefore
  returns 422. An unavailable GitHub installation and a missing installation are
  distinct conditions; document existing behavior without broadening it.
- Make PIPELINE_SPEC §6 the single canonical explanation of check precedence.
  SYSTEM_DESIGN §6.7, TEST_PLAN §2.5 and the decision-table entry link there;
  the sequence diagram shows all outcomes without duplicating precedence prose.
- All four conflict causes share the literal string `detail` returned by the
  route; there is no machine-readable reason code. Preserve the closed
  `ErrorResponse` shape and remove the obsolete claim about the UI's text.
- Change only rerunRun's 422 response to the existing `anyOf` pattern used by
  listRuns: `HTTPValidationError` for invalid UUID and `ErrorResponse` for missing
  active rule/prompt configuration. Do not widen shared `ValidationFailed`.
- Exercise actual TestClient responses with the existing fake unit of work and
  `_validator_for`; a new NOT_CONFIGURED fixture outcome must fail against the
  current schema before the YAML correction. Include invalid-UUID coverage so
  both 422 variants remain supported.

## Tasks and checkpoints

1. Contract regression coverage and schema correction (tests + OpenAPI only).
   Run the contract suite before edits, demonstrate the new string-detail
   regression fails against the old schema, then correct the rerun-only schema
   and prove both 422 responses validate. See [task list](162-todo.md).
2. Documentation alignment (PIPELINE_SPEC, SYSTEM_DESIGN, TEST_PLAN only).
   Preserve historical T3 decision provenance and add #132/#149 references;
   document four 409 causes, common detail, absent reason code and the canonical
   ordering, with links from other documents. Retain existing branch-filter and
   label/CI behavior statements.

Checkpoint after task 1: focused contract suite green and recorded red/green evidence.
Checkpoint after task 2: all four API gates green; complete AC review against #162;
review standards/spec against the fixed base; ready for publication through PR.

## Scope boundaries

No runtime edits, new reason field, new dependencies, migration, frontend change,
or unrelated documentation cleanup. Do not close #162 or add Development-panel
links. PR sections remain What / Why / How to verify / Refs, with Russian prose
and a non-closing issue reference. Rebase on main before review.

## Risks and mitigations

| Risk | Impact | Mitigation |
| --- | --- | --- |
| Open PRs own overlapping contract/test/docs files | Medium | Coordinator has notified PRs #161, #159, #155, #148, #133 and #124; inspect latest main during rebase. |
| Tests accept a schema but never exercise the string response | Medium | Obtain actual NOT_CONFIGURED response, assert its literal body, validate at rerunRun's 422 pointer and record initial failure. |
| Widening the global validation response changes unrelated endpoints | Medium | Use operation-local `anyOf`; keep shared components unchanged. |
| Precedence or UI assertions drift again | Medium | Keep ordering canonical in PIPELINE_SPEC §6, link other docs there, remove current-UI wording. |
| Missing installation confused with unavailable installation | Medium | Describe the store's candidate-before-availability behavior; do not change implementation to match a simplified narrative. |

## Open questions

None. The issue fixes documentation/schema fidelity and explicitly excludes a
machine-readable conflict reason and runtime changes.
