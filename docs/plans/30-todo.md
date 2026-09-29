# Issue #30 task list — both repositories

T01–T04, C01–C04 and C06–C25 are complete; the corpus checkpoint has user signoff; C05/SEC-05 is omitted after two automatic safety-filter stops; other unchecked items remain pending. API base: `3cdb417` (`origin/main`, 29.09.2026). `30-plan.md` records architecture, ownership and blockers. A task is complete only with its stated evidence and applicable repository gates. In case paths below, `<id>` means one directory under `test-prs-dataset/cases/`; each active case is a separate small task touching only its own metadata, patch and pre-image (plus its recorded response when baseline is generated). Do not use `.py` for case source files in the repository root.

## Foundation

### T01 — Freeze TEST_PLAN methodology (api; 2–4 files)

- [x] **Acceptance:** Move current UI TEST_PLAN content to api `docs/TEST_PLAN.md` after incorporating UI PR #58; retain Р-1…Р-9 and correct §1–2 against current SD/PIPELINE_SPEC (auth-api, worker, PG idempotency, `synchronize`, publisher `side`/`commit_id`/≤10 inline, sandbox phase 3, Р-9/10/14/15, L0–L4).
- [x] **Acceptance:** Add test levels, local/CI/staging/live environments and commands, data/case template, D4 server auth (including `workspaces: []`), webhook label rule, T3 rerun, UI auth, and oracle for hallucinations.
- [x] **Acceptance:** §3 defines curator-class mapping, matching key/tolerance/duplicate handling, raw-response stage, validity denominator, micro Precision/Recall, Critical Recall, per-category/zero-denominator rules, verdict agreement; explicitly records manual semantic adjudication and that quality thresholds report only.
- [x] **Verify:** Human compare against issue criteria and SD; markdown links/section references resolve. **Depends:** None. **Likely files:** api `docs/TEST_PLAN.md`; UI `docs/TEST_PLAN.md` read-only until PR #58 merges. **Ownership:** Do not edit UI PR #58 file while open without a comment.

### T02 — Patch and secret-scan safeguards (api; 2 files)

- [x] **Acceptance:** `.pre-commit-config.yaml` excludes dataset `.patch` bytes from trailing-whitespace/end-of-file hooks before first case commit; no blanket bypass for other files.
- [x] **Acceptance:** Synthetic credentials are unmistakably fake; if a fixture still triggers Gitleaks, add only the narrowly documented placeholder allowlist to `.gitleaks.toml` before case commit.
- [x] **Verify:** Hook run leaves a sample patch byte-identical and `git apply --check` passes; Gitleaks remains green. **Depends:** None. **Likely files:** api `.pre-commit-config.yaml`, `.gitleaks.toml`. **Ownership:** Recheck open PR file lists.

### T03 — Case schema and single-case validator (api; 3–5 files)

- [x] **Acceptance:** JSON Schema requires ID/class/language/source/license (real cases), `expected_verdict`, Finding-field ground truth and base/patch paths; rejects malformed enums/anchors and missing required fields. Prompt/response provenance belongs to a separate manifest in T05/T07-R so a case validates before model #33 and baseline exist.
- [x] **Acceptance:** Validator applies every patch with `git apply --check` against a temporary copy of its committed pre-image, checks truth paths/lines against new-side changed lines and derived verdict, and returns nonzero with case-specific diagnostics.
- [x] **Verify:** Valid fixture passes; invalid schema, malformed patch, missing base, out-of-diff anchor and wrong verdict fail. **Depends:** T01,T02. **Likely files:** api `test-prs-dataset/schema/case.schema.json`, `review/scripts/validate_dataset.py`, `tests/test_validate_dataset.py`, `test-prs-dataset/README.md`.

## Dataset: one focused case per row

**Common acceptance for each active Cxx:** one valid `git diff` applies to committed pre-image; case validates against T03 schema; truth uses `path/start_line/line/severity/category` and anchor D6; no answer-hint comments; `expected_verdict` matches truth; language and class are recorded. For real slots, a stable PR/commit URL, source revision, license name/URL, attribution and manually verified ground truth are mandatory. Each row's verification is `uv run python review/scripts/validate_dataset.py --case <id>` plus the recorded human oracle check. Candidate real PRs are selected during sourcing, never inferred from a title. C01/C02 contribute two critical truths; at least three additional distinct, evidence-backed critical truths must come from resource/logic cases. Never inflate severity only to meet the count.

| Task | ID | Curator class and intended case | Language/source | Depends |
| --- | --- | --- | --- | --- |
| [x] C01 | SEC-01 | hardcoded fake credential; critical | Python, repaired TC-02 idea | T03 |
| [x] C02 | SEC-02 | SQL injection; critical | Python, repaired TC-03 idea | T03 |
| [x] C03 | SEC-03 | unsafe path/input handling | TypeScript, synthetic | T03 |
| [x] C04 | SEC-04 | auth/permission regression | Python, licensed real PR | T03 + source/license verification |
| [x] C06 | RES-01 | unclosed file descriptor | Python, repaired TC-05 idea | T03 |
| [x] C07 | RES-02 | leaked subscription/timer | React, synthetic | T03 |
| [x] C08 | RES-03 | CookieJar memory growth across URL paths | Python, licensed real PR | T03 + source/license verification |
| [x] C09 | RES-04 | retained listener | TypeScript, synthetic | T03 |
| [x] C10 | RES-05 | memory growth in repeated operation | Python, synthetic | T03 |
| [x] C11 | LOG-01 | None/null dereference; repaired pre-image | Python, repaired TC-04 idea | T03 |
| [x] C12 | LOG-02 | wrong conditional branch; critical if justified | TypeScript, synthetic | T03 |
| [x] C13 | LOG-03 | boundary/off-by-one defect | Python, licensed real PR | T03 + source/license verification |
| [x] C14 | LOG-04 | state-transition regression | React, synthetic | T03 |
| [x] C15 | LOG-05 | incorrect result/return path | Python, synthetic | T03 |
| [x] C16 | SYN-01 | redundant syntax without behavior change | Python, synthetic | T03 |
| [x] C17 | SYN-02 | repetitive JSX/TS expression | React, synthetic | T03 |
| [x] C18 | SYN-03 | avoidable control-flow boilerplate | Python, synthetic | T03 |
| [x] C19 | SYN-04 | duplicated declaration/import overhead | TypeScript, licensed real PR | T03 + source/license verification |
| [x] C20 | SYN-05 | needless complexity with stable behavior | Python, synthetic | T03 |
| [x] C21 | CLEAN-01 | behavior-preserving refactor (`round` regression removed) | Python, repaired TC-01 idea | T03 |
| [x] C22 | CLEAN-02 | safe base-class call with full context | Python, repaired TC-06 idea | T03 |
| [x] C23 | CLEAN-03 | harmless UI refactor | React, licensed real PR | T03 + source/license verification |
| [x] C24 | CLEAN-04 | clean type/format improvement | TypeScript, synthetic | T03 |
| [x] C25 | CLEAN-05 | clean change with realistic surrounding code | Python, synthetic | T03 |

**Omitted slot:** C05/SEC-05 was stopped twice by the automatic safety filter (the cross-tenant access example, then a static cookie-protection regression). It is excluded from active tasks, dataset and replay/live denominators; no further security-case attempt is planned. This leaves four completed security cases and does not block the checklist.

- [x] **Corpus checkpoint:** 24 active cases (within 20–30): security 4, each of the other four classes 5, ≥5 licensed real PRs with at least one per class, Python and TS/React, ≥5 distinct critical truths (at least three from resource/logic beyond C01/C02), 100% schema/apply checks. Real-slot substitution is allowed only if all aggregate criteria remain true; C05 is not a substitute slot. **Depends:** C01–C04 and C06–C25. **Verify:** Validator emits machine-readable counts and human review signs off source/license/ground truth and critical severity justification.
  - [x] Automated evidence: `--final --report-json` passes with 24/24 valid, five distinct licensed real PRs and five distinct critical anchors; independent case reviews found no outstanding findings.
  - [x] Human signoff on real source/license/truth and critical severity justification: user confirmed in this chat on 29.09.2026.
- [ ] **Disposition:** TC-07 becomes `is_generated` unit coverage; TC-08 becomes T10 webhook integration coverage; TC-09 is recorded as phase-3 sandbox deferral. Remove all nine old UI fixtures only in T17. **Depends:** T03/T10/T17.

## Eval and CI

### T04 — Deterministic one-to-one scorer (api; 2–4 files)

- [x] **Acceptance:** Score raw valid ReviewOutput against truth using TEST_PLAN §3; one prediction matches at most one truth, duplicates/clean-case findings count FP, unmatched truths FN, critical truths yield Critical Recall; per-category counts and verdict agreement follow PIPELINE_SPEC §11.
- [x] **Acceptance:** Invalid responses do not crash scoring; validity and detection denominators/undefined metrics follow documented rules.
- [x] **Verify:** Unit tests for duplicate, ±2 line boundary/miss, wrong path/category, clean FP, missing prediction, invalid output, zero denominators, critical and verdict. **Depends:** T01,T03. **Likely files:** api `review/scripts/eval_score.py`, `tests/test_eval_score.py`.

### T05 — Offline replay command and report (api; 2–4 files)

- [ ] **Acceptance:** For each case load committed raw response; invoke existing `validate_findings.py` and accept only exit 0 plus `OK ReviewOutput`; exit 1/2 and RepoConventionsDraft count invalid. Never require key/network.
- [ ] **Acceptance:** Console and JSON report contain valid %, micro Precision, Recall, Critical Recall, per-category breakdown, verdict agreement, counts, model ID and prompt digest/version.
- [ ] **Verify:** Two offline runs yield byte-identical JSON; tests cover validator exit cases. **Depends:** T04 and initial representative cases; final acceptance needs C01–C04 and C06–C25. **Likely files:** api `review/scripts/eval_replay.py`, `tests/test_eval_replay.py`, `test-prs-dataset/responses/manifest.json`.

  - [x] CLI/scorer integration verified with temporary recorded responses: strict validator kind/exit handling, counts, provenance and byte-identical JSON across two runs.
  - [ ] Full corpus replay awaits the recorded response manifest and baseline responses after the #33 model choice; the CLI currently reports their absence explicitly.

### T06 — API PR replay summary (api; 1–3 files)

- [ ] **Acceptance:** Every PR validates case schema and patch application, runs replay, and writes metrics + model/prompt provenance into `GITHUB_STEP_SUMMARY`; a changed `review/prompts/` file with stale response digest emits a visible warning and directs live refresh.
- [ ] **Acceptance:** Metric thresholds do not fail this sprint's required check; malformed dataset or broken replay command does fail. Job/check names remain stable.
- [ ] **Verify:** CI PR run shows report and a deliberate prompt-only change shows warning. **Depends:** T03,T05, corpus. **Likely files:** api `.github/workflows/eval-replay.yml`, `review/scripts/eval_replay.py`. **Ownership:** Recheck PR #10/#38 workflow diffs first.

### T07 — Live model adapter (api; 2–4 files)

- [ ] **Acceptance:** Live mode constructs case context through current PromptBuilder and `review/prompts/`, calls selected model (#33/OQ-2) with credentials outside repo, captures raw output, model ID, prompt digest and run metadata. The output layout is replay-compatible.
- [ ] **Verify:** Live smoke case produces a replay-compatible report without logging secrets. **Depends:** T05, one validated case, #33 model/adapter/credential decision. **Likely files:** api `review/scripts/eval_live.py`, `tests/test_eval_live.py`. **Blocker:** model/secret policy not yet settled.

### T07-R<id> — Record baseline response for one case (api; 1–3 files each, repeat C01–C04 and C06–C25)

- [ ] **Acceptance per case:** Record the raw model response plus model ID, prompt digest/version and run metadata for this case; review for secrets/unlicensed content. Invalid responses remain recorded rather than rewritten as valid.
- [ ] **Verify per case:** Replay reads the recorded response and reports its validator result. **Depends:** T07 and corresponding Cxx. **Likely files:** api `test-prs-dataset/responses/<id>.json`, response manifest entry.
- [ ] **Aggregate acceptance:** All 24 active response tasks done; C05 has no response entry; T04/T05 computes baseline metrics and dataset README records them. **Verify:** Replay reproduces baseline totals with denominator 24. **Likely file:** api `test-prs-dataset/README.md`.

### T08 — Manual live CI workflow (api; 1–2 files)

- [ ] **Acceptance:** Separate `workflow_dispatch` job runs live eval with documented secret handling and prints the same metric report; it is not required. It cannot leak credentials/raw private data in logs.
- [ ] **Verify:** Authorized manual dispatch completes and summary matches report schema. **Depends:** T07 and CI secret policy. **Likely files:** api `.github/workflows/eval-live.yml`, `review/scripts/eval_live.py`.

### Checkpoint 1 — dataset/replay

- [ ] T01–T06 and C01–C04 plus C06–C25 pass locally and in API PR CI; active dataset and report counts equal 24. Live tasks are tracked separately until their named blocker clears.

## API integration paths

### T09 — PostgreSQL Run invariants (api; 1–2 files)

- [x] **Acceptance:** Tests against migrated disposable PostgreSQL schema prove default `queued`, partial unique index permits only one active Run per CodeChange, `idempotency_key` uniqueness, and FK chain. Assert actual insert/constraint behavior, not just index existence.
- [x] **Verify:** `TEST_DATABASE_URL=... uv run pytest -m integration -rs`: 12 passed, 0 skipped; `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy .`, and `uv run pytest` passed (388 passed, 1 xfailed). **Depends:** Existing main schema. **Likely files:** api `tests/test_run_invariants_integration.py`.

### T10 — Webhook → queued Run (api; 1–3 new test files)

- [ ] **Acceptance:** With fake GitHub REST and real PG, test human `ai-review` label + green CI in either order, no label, red/pending CI then green, own suite excluded, `wait_for_ci` modes/no-CI sweep, `synchronize` cancellation and new SHA, manual unlabeled/closed/reopened, duplicate delivery/sha, valid/invalid HMAC and HTTP 202.
- [ ] **Acceptance:** Wait/poll for eventual Run after 202; assert no second Run, and no network call inside DB transaction. Test generated-file filtering from old TC-07 separately as unit coverage where #11 implements it.
- [ ] **Verify:** New tests run in required CI without skips. **Depends:** #11/PR #38 and #34 publisher/sweep contract. **Likely files:** api `tests/test_webhook_run_integration.py`, `tests/test_generated_diff_filter.py`. **Ownership:** Put tests in PR #38 with owner coordination or start after merge; do not edit PR-owned files without a comment. **Issue conflict:** use label, not obsolete review-request event.

### T11 — Server auth lifecycle (api; 1–3 new test files)

- [ ] **Acceptance:** With fake GitHub and real PG, callback yields access JWT+refresh cookie; refresh rotates; reuse revokes family; me reflects user/workspaces; logout makes refresh unusable; GitHub exchange failure creates no cookie/session.
- [ ] **Acceptance:** Missing/expired/foreign/malformed JWT or missing/bad Workspace claim yields 401; `workspaces: []` is valid and returns empty 200 lists; two-Workspace data isolation in GET runs/repos and cross-Workspace run detail 404.
- [ ] **Verify:** Auth integration suite runs in required CI without skips. **Depends:** #11/PR #38, #34 for GET repos. **Likely files:** api `tests/test_auth_lifecycle_integration.py`, `tests/test_workspace_authorization_integration.py`. **Ownership:** PR #38 tests need coordination/comment or post-merge PR.

### T12 — Rerun API T3 (api; 1–2 new test files)

- [ ] **Acceptance:** 202 creates `trigger=rerun` queued Run at current SHA without label/CI checks; active Run and closed PR return 409 without insert; missing auth 401; other Workspace 404.
- [ ] **Verify:** Real PG+RabbitMQ integration suite runs in required CI without skips. **Depends:** #34 rerun/queue and #11 auth. **Likely files:** api `tests/test_rerun_integration.py`.

### T13 — Required API integration services (api; 1–2 files)

- [ ] **Acceptance:** Existing required `Python lint / type / test` has PostgreSQL and RabbitMQ services, `TEST_DATABASE_URL` and RabbitMQ address, readiness checks, then uv gates; no integration-marked test skips.
- [ ] **Verify:** Required CI log identifies integration count and zero skips; intentionally unavailable service fails job rather than turning green with skips. **Depends:** T09; full claim needs T10–T12 and #34 worker tests. **Likely files:** api `.github/workflows/ci-cd.yml`. **Ownership:** PR #10/#38 diff check; do not edit owned file without comment.

### Checkpoint 2 — API integration

- [ ] T09–T13 green in required job with real services, zero integration skips, original check name, and no regression of four uv gates.

## UI and documentation retirement

### T14 — UI auth client tests (ui; 2–4 new or existing test files)

- [ ] **Acceptance:** Callback backend failure leaves no local session; OAuth `state` missing/mismatch prevents exchange; concurrent 401s share one refresh then each original request retries once; failed refresh logs out; startup refresh then `/api/auth/me` restores session.
- [ ] **Verify:** `pnpm test` runs tests under `src/**/*.test.{ts,tsx}`. **Depends:** ui#50/PR #55. **Likely files:** ui `src/features/auth/model/store.test.ts`, `src/pages/auth/CallbackPage.test.tsx`, `src/shared/api/client.test.ts` (actual merged paths to confirm). **Ownership:** PR #55 owns these now; coordinate in PR or wait for merge.

### T15 — UI Finding→ReviewComment contract (ui; 1–2 files)

- [ ] **Acceptance:** Tests validate single-line `newLine=line,endLine=null`, range `newLine=start_line,endLine=line`, enum parity for severity/category and Run statuses against backend contract fixture; no old `APPROVE`/`COMPLETED` vocabulary.
- [ ] **Verify:** `pnpm test`, `pnpm check-types` pass. **Depends:** current api D6 and UI schema; account for UI PR #58. **Likely files:** ui `src/entities/review/model/schemas.test.ts`, `src/entities/run/model/schemas.test.ts` (confirm after merge).

### T16 — UI PR quality job and required ruleset (ui; 1–2 files + GitHub setting)

- [ ] **Acceptance:** PR CI executes `pnpm lint`, `pnpm check-types`, `pnpm format:check`, `pnpm test`; resulting check is configured required in branch ruleset.
- [ ] **Verify:** A UI PR shows the green job and repository ruleset lists its exact check name; `pnpm build` also passes locally. **Depends:** ui#50/PR #55 workflow merge or comment. **Likely files:** ui `.github/workflows/ci-cd.yml`; ruleset. **Ownership:** PR #55 currently owns workflow.

### T17 — Canonical docs and old dataset retirement (api + ui; split PRs, 1–4 files each)

- [ ] **Acceptance:** API SD header/§13, `review/README.md`, api AGENTS and UI AGENTS point to api `docs/TEST_PLAN.md`; stale pending PR/reference and wrong section numbers fixed; UI `docs/TEST_PLAN.md` and nine `tests/golden_dataset/` directories removed only after T01 and corpus validation. Execute as separate small api-link, UI-link, and one-old-case-directory-at-a-time deletion slices.
- [ ] **Acceptance:** README documents class/category mapping, all real PR attributions/licenses, corpus counts, case replay/live commands and baseline metrics; TC-07/08 replacement and TC-09 deferral are stated. QA approval evidence for api PR #36 is linked/recorded.
- [ ] **Verify:** Link check, `rg` for obsolete paths/terms, both repo gates. **Depends:** T01,C01–C04,C06–C25,T07-R<id> baseline set; UI PR #58 merge or comment. **Likely files:** api `docs/SYSTEM_DESIGN.md`, `review/README.md`, `AGENTS.md`, `test-prs-dataset/README.md`; ui `AGENTS.md`, `docs/TEST_PLAN.md`, `tests/golden_dataset/**`. **Ownership:** Split into separate small commits/PRs; api PR #10 owns SD, UI PR #58 owns TEST_PLAN.

### T18 — Delivery and acceptance audit (both repos)

- [ ] **Acceptance:** Map every issue #30 checkbox to test/report/PR evidence, including 24 active cases (security 4, four other classes 5 each), ≥5 licensed real with at least one per class, ≥5 distinct critical (at least three from resource/logic), replay and live summaries, API/UI integration, zero skip, QA approval of #36, links and required checks. Record C05's safety-filter omission without scheduling another security attempt. Resolve the stale `review_requested` wording with issue owner under current label canon.
- [ ] **Acceptance:** API gates `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy .`, `uv run pytest`; UI gates `pnpm lint`, `pnpm check-types`, `pnpm format:check`, `pnpm test`, `pnpm build` all green; required checks green, approving reviews obtained, threads resolved, branches rebased on main.
- [ ] **Verify:** PR bodies in What / Why / How to verify / Refs order, conventional titles ≤72 chars; UI PR says `Refs larchanka-training/dmc-268-api-t6#30`; any dependency has a PR-body line and lockfile. Merge by 04.10.2026 and close #30 only after last PR. **Depends:** T01–T17. **Likely files:** PR descriptions and issue checklist; no implementation file.

### Checkpoint 3 — complete

- [ ] All issue acceptance and DoD items have objective evidence, cross-repo PRs are reviewed/merged, required checks are active, and issue #30 is closed after merge.
