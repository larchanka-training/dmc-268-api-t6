# Task list: overview body budget, run.step fields, post-#97 doc drift (#170)

See [170-plan.md](170-plan.md) for design decisions D-1–D-7. Baseline `origin/main` `1d05fbe`;
branch `fix/170-overview-budget-and-docs`. Each implementation task is one TDD slice unless noted.

**Common verification** (every task that touches code):

```sh
uv run ruff check .
uv run ruff format --check .
uv run mypy .
uv run pytest
```

Use `TEST_DATABASE_URL` for integration tests when the task touches DB-backed paths.

---

## Task 1: RED — long-path overview regression test (Ч.1)

**Description:** Replace the second half of
`test_character_cap_drops_only_additional_findings` (`tests/test_findings_post_processor.py:286-301`)
so it fails against current code and encodes #170 AC: with ~50 long paths plus a body-only finding,
the body is ≤ 4000 chars, the finding is listed **or** an overflow line counts it with a placeholder
URL, and Additional findings are not silently dropped when `prefix >= 4000`.

**Acceptance criteria:**

- [ ] Test fails on unfixed `findings_post_processor.py`.
- [ ] Test documents expected checked-file truncation behaviour (not full 50-line list).
- [ ] Test expects `Spill note` visible or explicit overflow count.

**Verification:** `uv run pytest tests/test_findings_post_processor.py::test_character_cap_drops_only_additional_findings -q` fails RED.

**Dependencies:** None.

**Files likely touched:** `tests/test_findings_post_processor.py`.

**Estimated scope:** Small.

---

## Task 2: Reviewable paths for Checked files (Ч.1)

**Description:** Pass a separate `checked_paths` (or filter) into `FindingsPostProcessor.process` /
`_overview` so **Checked files** lists only snapshots with non-null `review_patch`, while
`hunk_lines` for inline/body bucketing stays unchanged.

**Acceptance criteria:**

- [ ] Ignored/omitted/unsupported paths (null `review_patch`) do not appear under Checked files.
- [ ] Task 1 test still RED on truncation/budget (not yet fixed).

**Verification:** Unit test with mixed snapshots; `tests/test_findings_post_processor.py`.

**Dependencies:** Task 1.

**Files likely touched:**

- `app/modules/reviews/infrastructure/run_repository.py` (input assembly)
- `app/modules/reviews/application/findings_post_processor.py`
- `tests/test_findings_post_processor.py`

**Estimated scope:** Small.

---

## Task 3: Checked-files cap in overview (Ч.1)

**Description:** Implement D-1 step 2 — cap listed paths with a final summary line
(`… и ещё N файлов`) before Additional findings consume budget.

**Acceptance criteria:**

- [ ] Long path lists truncate deterministically (sorted order preserved for shown paths).
- [ ] Overview stays readable for 50×~58-char paths within budget after Task 4.

**Verification:** Task 1 scenario progresses; dedicated unit test for cap edge cases (0, 1, N).

**Dependencies:** Task 2.

**Files likely touched:** `findings_post_processor.py`, tests.

**Estimated scope:** Small.

---

## Task 4: Body budget packer and overflow footer metadata (Ч.1)

**Description:** Implement D-1 packing (≤ 4000, no mid-finding cut, no silent drop when
`prefix >= 4000`). Extend `ProcessedReviewOutput` with metadata for omitted body-only count.
Append `**Ещё N находок:** {url}` in the store/publish path when count > 0 and URL known.

**Acceptance criteria:**

- [ ] Task 1 test GREEN.
- [ ] `test_character_cap_drops_only_additional_findings` first scenario (overflow notes) still passes.
- [ ] `len(review_body) <= 4000` for all post-processor tests.
- [ ] `findings_hash` unchanged for same finding sets (existing publication tests green).

**Verification:** `tests/test_findings_post_processor.py`, `tests/test_publish_run_review.py` (if fixtures touch body).

**Dependencies:** Task 3.

**Files likely touched:**

- `app/modules/reviews/application/findings_post_processor.py`
- `app/modules/reviews/application/review_output.py` (if DTO extended)
- `app/modules/reviews/infrastructure/run_repository.py` or `store_review_output.py` (footer URL)
- `tests/test_findings_post_processor.py`

**Estimated scope:** Medium.

---

## Task 5: Align check-run `body_count` with body (Ч.1)

**Description:** Implement D-3 so `CheckRunReport.body_count` matches visible body-only findings plus
overflow count, not merely `len(findings) - inline`.

**Acceptance criteria:**

- [ ] Succeeded check-run summary `в теле ревью: N` matches body content for regression scenario.
- [ ] `test_severity_threshold_postgres.py` remains green (#149).
- [ ] `test_check_run_view_sends_the_run_link_as_details_url_in_every_state` still passes (#172).

**Verification:** Postgres/integration tests above; common verification block.

**Dependencies:** Task 4.

**Files likely touched:**

- `app/modules/reviews/infrastructure/run_lifecycle_store.py`
- Possibly `runs.review_body` metadata column or derived parse — prefer explicit stored count from Task 4

**Estimated scope:** Medium.

---

## Task 6: Spec sync for overview budget (Ч.1 + ч.3 partial)

**Description:** Update `docs/plans/97-todo.md` Task 1 AC (4000 / path list), `docs/plans/97-plan.md`
overview bullet, and PIPELINE_SPEC / SD only if checked-file definition is documented explicitly.

**Acceptance criteria:**

- [ ] 97-todo status reflects Tasks 7–8 completion from #97 and points body-budget fix to #170.
- [ ] No contradiction with PIPELINE_SPEC §7 check-run text (#172).

**Verification:** Doc review; `uv run ruff format --check .` on touched files.

**Dependencies:** Task 5.

**Files likely touched:** `docs/plans/97-todo.md`, `docs/plans/97-plan.md`, optional `docs/PIPELINE_SPEC.md`.

**Estimated scope:** Small.

---

## Checkpoint: After Tasks 1–6

- [ ] Ч.1 AC satisfied.
- [ ] Focused pytest + full gates green.
- [ ] Optional: open PR A or continue on same branch.

---

## Task 7: RED — OpenAPI and stream tests for new step fields (Ч.2)

**Description:** Extend `RunStepEvent` in `contracts/openapi.yaml` with required `attempt` and
optional `llmOperation` enum for `llm.call`. Update
`tests/test_openapi_contract.py` and `tests/test_run_stream.py` expected JSON (still no `id:` on steps).

**Acceptance criteria:**

- [ ] Contract tests fail until runtime/schema code updated.
- [ ] `additionalProperties: false` preserved with explicit new properties.

**Verification:** `uv run pytest tests/test_openapi_contract.py tests/test_run_stream.py -q` RED.

**Dependencies:** Checkpoint 1–6 (may run in parallel if branch split — no file overlap).

**Files likely touched:** `contracts/openapi.yaml`, `tests/test_openapi_contract.py`, `tests/test_run_stream.py`.

**Estimated scope:** Small.

---

## Task 8: NOTIFY payload and `RunStep` model (Ч.2)

**Description:** Add `attempt` and optional `llm_operation` to NOTIFY JSON in
`run_action_records.py`, parse in `run_step_listener.py`, extend `RunStep` in `run_events.py`.

**Acceptance criteria:**

- [ ] `insert_run_action` sets `attempt` from locked `Run.attempt`.
- [ ] Malformed payloads still dropped safely.

**Verification:** `tests/test_run_step_postgres.py`; Task 7 still RED on OpenAPI/app frame.

**Dependencies:** Task 7.

**Files likely touched:**

- `app/modules/reviews/infrastructure/run_action_records.py`
- `app/bootstrap/run_step_listener.py`
- `app/modules/reviews/application/run_events.py`
- `tests/test_run_step_postgres.py`

**Estimated scope:** Medium.

---

## Task 9: Persist `operation` on `llm.call` request (Ч.2)

**Description:** Extend `LlmCallRecord.request_json()` / gateway record path so each `llm.call`
stores `operation: conventions|review` from `StructuredTask.operation`.

**Acceptance criteria:**

- [ ] Conventions and review calls distinguishable in `run_actions.request` JSON.
- [ ] Existing llm gateway tests updated if they assert exact request shape.

**Verification:** `tests/test_llm_gateway.py` or postgres llm call tests.

**Dependencies:** Task 8.

**Files likely touched:**

- `app/modules/reviews/application/llm.py`
- `app/modules/reviews/infrastructure/llm/gateway.py`
- `tests/test_llm_gateway.py` / `tests/test_run_llm_origin_postgres.py`

**Estimated scope:** Small.

---

## Task 10: SSE frame and FastAPI OpenAPI mirror (Ч.2)

**Description:** Update `_run_step_frame` in `main.py` and any duplicate schema builders; wire
`llm_operation` from request when emitting `llm.call` steps.

**Acceptance criteria:**

- [ ] Task 7 tests GREEN.
- [ ] Step frames still have no `id:` line.

**Verification:** Task 7 pytest; `uv run pytest tests/test_run_stream.py -q`.

**Dependencies:** Tasks 8–9.

**Files likely touched:** `app/main.py`, possibly `InMemoryRunStepHub` test helpers.

**Estimated scope:** Small.

---

## Task 11: PIPELINE_SPEC step payload + ui#84 comment + UI Zod PR (Ч.2)

**Description:** Update PIPELINE_SPEC §1 bullet on `run_step` NOTIFY/SSE fields. After the API PR
exists (or in the same delivery window per plan decision B): open a **small** UI PR in
`dmc-268-ui-t6` that adds Zod `RunStepEvent` (`attempt` required min 1, optional `llmOperation`)
and a contract test — **do not** switch the progress bar off `/actions` polling. Comment on
https://github.com/larchanka-training/dmc-268-ui-t6/issues/84 with both PR links and the field list.

**Acceptance criteria:**

- [ ] OpenAPI description for `/api/stream` mentions both event types (feeds Task 12).
- [ ] UI Zod + contract test PR opened (or ready) in the same window; progress bar still poll-based.
- [ ] ui#84 comment posted after API PR URL exists.

**Verification:** Doc diff review; UI `pnpm test` for the new schema test.

**Dependencies:** Task 10.

**Files likely touched:** `docs/PIPELINE_SPEC.md`, `contracts/openapi.yaml` (description only if not done in Task 7); UI: run stream / run entity schemas + tests.

**Estimated scope:** Small (api docs) + Small (ui PR).

---

## Checkpoint: After Tasks 7–11

- [ ] Ч.2 AC satisfied.
- [ ] Contract + stream tests green.

---

## Task 12: OpenAPI stream summary and example (Ч.3)

**Description:** Fix `contracts/openapi.yaml` `/api/stream` summary and SSE example text per issue
(both `run.updated` and `run.step`; keep replay rules).

**Acceptance criteria:**

- [ ] `test_stream_declares_run_step_beside_the_status_event` still passes.
- [ ] Summary reflects status **and** step events.

**Dependencies:** Task 11 (or merge OpenAPI yaml once).

**Files likely touched:** `contracts/openapi.yaml`.

**Estimated scope:** XS.

---

## Task 13: SECRETS and `.env.example` GitLab base URL (Ч.3)

**Description:** SECRETS.md — `GITLAB_BASE_URL` is GitLab **instance root** (code adds `/api/v4`).
Reorder `.env.example` so GitLab comments sit with worker/portal block per issue.

**Acceptance criteria:**

- [ ] Wording matches `gitlab_review_publication.py` URL construction.

**Dependencies:** None (doc).

**Files likely touched:** `docs/SECRETS.md`, `.env.example`.

**Estimated scope:** XS.

---

## Task 14: docker-compose worker GitLab env (Ч.3)

**Description:** Add `GITLAB_TOKEN` and `GITLAB_BASE_URL` to `worker.environment` in
`docker-compose.yml` (mirror staging pattern if present).

**Acceptance criteria:**

- [ ] Local stack can pass GitLab vars into worker without editing compose by hand.

**Dependencies:** Task 13.

**Files likely touched:** `docker-compose.yml`, optionally `deploy/compose/staging.yml` if drift exists.

**Estimated scope:** XS.

---

## Task 15: PIPELINE_SPEC GitLab / T14–T15 / §5.2 (Ч.3)

**Description:** Align phase table checkpoint wording for GitLab with code (**doc-only**, decision A:
do not implement GitHub-style HTTP checkpoints in the GitLab gateway). Annotate T14/T15 that
GitLab has no check-run; add GitLab cross-refs in §5.2 or pointer to SD §8.4. Update NOTIFY/SSE
field list if not fully done in Task 11.

**Acceptance criteria:**

- [ ] Phase table no longer claims GitLab HTTP checkpoints that the gateway does not run.
- [ ] T16 target vs implemented codes called out where mixed.

**Dependencies:** Tasks 11–12.

**Files likely touched:** `docs/PIPELINE_SPEC.md`.

**Estimated scope:** Medium.

---

## Task 16: BACKEND_ARCHITECTURE worker and SSE (Ч.3)

**Description:** Update worker consumer table (T14–T16 including GitLab path), transaction bullet
to mention `llm.review_output`, add `run_step` NOTIFY → listener → hub → `/api/stream` paragraph.

**Acceptance criteria:**

- [ ] Text search finds `gitlab.publish_review` and run step bridge.
- [ ] Does not duplicate #194 full catalog work.

**Dependencies:** None.

**Files likely touched:** `docs/BACKEND_ARCHITECTURE.md`.

**Estimated scope:** Small.

---

## Task 17: workspace_id SELECT and visibility filter note (Ч.3)

**Description:** Fix PIPELINE_SPEC §1 SSE bullet: portal-api may SELECT for visibility filtering
(`run_events.py`); `workspace_id` in NOTIFY remains for future fan-out. Document step visibility
filter if missing.

**Acceptance criteria:**

- [ ] Doc matches `iter_visible_run_events` / `_visible_updated_at` behaviour.

**Dependencies:** None.

**Files likely touched:** `docs/PIPELINE_SPEC.md`.

**Estimated scope:** XS.

---

## Task 18: CONTEXT_AND_VERIFICATION_SPEC and TEST_PLAN GitLab pointers (Ч.3)

**Description:** Add explicit pointers to #97 GitLab adapter / gateway where docs say GitLab is
“only specified” without implementation anchor.

**Acceptance criteria:**

- [ ] At least issue bullets at :27, :1400, :1507 (CONTEXT) and :102, :114, :408 (TEST_PLAN) addressed or cross-linked.

**Dependencies:** None.

**Files likely touched:** `docs/CONTEXT_AND_VERIFICATION_SPEC.md`, `docs/TEST_PLAN.md`.

**Estimated scope:** Small.

---

## Task 19: PIPELINE_SPEC §6 error code target vs implemented (Ч.3)

**Description:** Mark `publication_outcome_unknown`, `publication_payload_too_large`,
`legacy_publication_unresolved` and related rows as target vs implemented per issue T16 note.

**Acceptance criteria:**

- [ ] Reader can tell which codes `main` emits today.

**Dependencies:** Task 15.

**Files likely touched:** `docs/PIPELINE_SPEC.md`.

**Estimated scope:** Small.

---

## Task 20: Final gates and PR (Ч.1–Ч.3)

**Description:** Full backend gates, PR body with What/Why/How to verify/Refs, no new deps. If ui
contract pin required, note follow-up ui PR separately.

**Acceptance criteria:**

- [ ] All issue AC checkboxes satisfied.
- [ ] `docs/plans/170-plan.md` and this file committed on the branch.

**Verification:** Full gate block; `uv run pytest tests/test_findings_post_processor.py tests/test_run_stream.py tests/test_openapi_contract.py -q`.

**Dependencies:** Tasks 6, 11, 12–19.

**Files likely touched:** PR only.

**Estimated scope:** Small.

---

## Checkpoint: Complete

- [ ] All tasks 1–20 done or explicitly deferred with issue comment.
- [ ] Tech lead acceptance per `docs/CONTRIBUTING.md`.
