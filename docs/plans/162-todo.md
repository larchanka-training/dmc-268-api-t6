# Tasks: rerun 409/422 contract alignment (#162)

Specification: [#162](https://github.com/larchanka-training/dmc-268-api-t6/issues/162).
Fixed base: `e904b1eea9ef731c06bfc947f361721b1609d29f`.
Plan approval: supplied by the user in advance.

## Task 1: Cover the rerun 422 wire contract

**Description:** Extend the existing contract fixture with a NOT_CONFIGURED rerun
outcome, verify the actual API's string-detail 422 and invalid-UUID validation
422, and correct the operation-local response schema using the established
`anyOf` pattern. No runtime behavior changes.

**Acceptance criteria:**

- [x] Actual RerunNotConfigured response is exactly
  `{"detail": "the repository has no active rule or prompt version"}` with 422,
  validated through rerunRun's declared response schema.
- [x] Regression fails against the original HTTPValidationError-only schema;
  after correction, both string-detail and invalid-UUID array-detail 422 validate.
- [x] Only rerunRun's 422 declaration changes; ErrorResponse remains closed and
  no conflict reason field or dependency is added.

**Verification:**

- [x] Baseline: `uv run pytest tests/test_openapi_contract.py` — coordinator
  recorded 116 passed, 2 existing deprecation warnings, 23.73 s.
- [x] Red: `uv run pytest tests/test_openapi_contract.py -k 'rerun_not_configured or rerun_invalid_uuid'`
  before the schema edit — 1 failed, 1 passed, 116 deselected, 2 existing warnings,
  3.07 s. The literal body/status assertions passed; schema validation failed with
  `'the repository has no active rule or prompt version' is not of type 'array'`.
- [x] Green: `uv run pytest tests/test_openapi_contract.py` — 118 passed,
  2 existing deprecation warnings, 8.84 s. Both actual 422 variants validate through
  rerunRun's operation-local schema.
- [x] Manual: inspected the operation-local schema and literal response assertions;
  shared ValidationFailed and closed ErrorResponse definitions are unchanged.
- [x] Independent Task 1 review: coordinator reported Standards — 0 findings,
  Spec — 0 findings, against the fixed base. Review preceded Task 2 edits.

**Dependencies:** None; overlap notices must precede file edits.

**Files likely touched:**

- `tests/test_openapi_contract.py`
- `contracts/openapi.yaml`

**Estimated scope:** Small (2 files).

## Task 2: Align rerun documentation

**Description:** Establish one canonical account of the existing check precedence
in PIPELINE_SPEC §6 and link to it from the sequence diagram's surrounding text,
test strategy and T3 decision entry. Explain four shared-detail 409 causes and
both 422 bodies, preserving the historical decision record.

**Acceptance criteria:**

- [x] All three docs name active Run, closed PR, repository pause and unavailable
  App as 409 causes with one common detail and no reason code; obsolete UI text
  and the claim of a cause-specific detail are removed.
- [x] PIPELINE_SPEC §6 alone explains precedence: closed PR/active Run → 409,
  missing candidate → 422, pause/unavailability → 409; other docs link there.
  The paused-repository-without-active-configuration example yields 422.
- [x] SYSTEM_DESIGN rerun diagram contains 409 and 422 outcomes; T3 decision
  provenance is supplemented with #132/#149 rather than rewritten as history.

**Verification:**

- [x] Manual: compared descriptions and diagram with `rerun_store.py`, candidate
  construction in `webhook_runs.py`, and the exception mapping in `app/main.py`.
  PIPELINE_SPEC §6.1 is the canonical precedence description; the diagram lists
  outcomes and links there. Existing unconditional pause→409 summaries now link
  to it as well, so they do not contradict the missing-configuration case.
- [x] Checked new local link destinations and heading anchors with a local
  `uv run python` check; all passed. The original SYSTEM_DESIGN label/CI and
  branch-filter paragraph is unchanged byte-for-byte.
- [x] Searched all three touched documents: the obsolete two-cause UI assertion
  and `соответствующим detail` wording are absent. Manually checked the T3 decision
  row preserves the original default and adds #132/#149 provenance.
- [x] `git diff --check` passes after documentation edits.
- [x] Final gates confirmed by the coordinator: `uv run ruff check .` — all
  checks passed; `uv run ruff format --check .` — 457 files already formatted;
  `uv run mypy .` — no issues in 454 source files; `uv run pytest` — 3104 passed,
  688 skipped, 2 existing deprecation warnings in 646.61 s (exit 0).
  Full-suite log: `/private/tmp/dmc-268-162-pytest.log`. Integration checks needing
  unconfigured infrastructure were skipped; this is not evidence of a live-stack
  integration run. No duplicate full-suite run was started.

**Dependencies:** Task 1.

**Files likely touched:**

- `docs/PIPELINE_SPEC.md`
- `docs/SYSTEM_DESIGN.md`
- `docs/TEST_PLAN.md`

**Estimated scope:** Medium (3 files).

## Checkpoint: Ready for review

- [x] Red/green regression evidence recorded, all gates green.
- [x] Issue AC reviewed and both tasks checked off only after verification.
- [x] No runtime, frontend or unrelated file changes. Publisher fetched origin/main:
  it remains the reviewed base `e904b1eea9ef731c06bfc947f361721b1609d29f` and is
  an ancestor of HEAD; no rebase was necessary.
- [x] Independent final standards/spec review against the fixed base passed:
  Standards — 0 findings; Spec — 0 findings (as did the Task 1 checkpoint).
- [x] Russian PR description prepared with What / Why / How to verify / Refs;
  no closing keyword or Development-panel linkage.
- [x] Publisher verified issue #162 is OPEN, assigned to `ndovnar`, and remains
  In Progress on course board 12 (`dmc-268-t6`), appropriate for a draft PR.
  The tech lead retains issue acceptance/closure ownership; human approval and
  merge are not claimed.
