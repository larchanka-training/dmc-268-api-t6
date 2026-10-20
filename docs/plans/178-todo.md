# #178 — implementation checklist

Status: user approved the plan in chat with "ок" on 2026-10-20. Task 1 complete: README and SD §15 audit, all four gates green, independent review Standards 0 / Spec 0. Task 2 complete: immutable v3/golden/examples and seed expectations, all gates green, fresh independent review Standards 0 / Spec 0. Task 3 complete: default v3/eval CLI provenance, all gates green, fresh Standards 0 / Spec 0. Task 4 complete: real PostgreSQL upgrade/persisted pins, all gates green, fresh Standards 0 / Spec 0, temporary DB/private files removed. Candidate publication remains Task 5; paid capture is unauthorized. No paid run, commit, push or issue edit has been performed; the coordinator posted only the four explicitly authorized coordination comments below. Plan approval does not authorize a paid run or external comments.

**Gate rule for every completed implementation slice:** perform focused RED/GREEN checks, then run `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy .`, and `uv run pytest` before marking that slice complete or handing it to independent review. Apply this to each docs/asset/default/test/import slice below; checkpoint checks do not permit knowingly broken intermediate handoffs. Report integration skips explicitly.

## Task 1: Record the raw baseline audit

**Description:** Preserve the 17-case/rule matrix and current-production distinction from `178-plan.md`; define raw-first validity before all normalization.

**Acceptance criteria:**

- [x] Corpus README contains all 17 case rows, per-rule totals (33 prefix omissions), 22 equality ranges and two ordering cases.
- [x] README and SD §15 explain prompt-only choice, current normalization, raw-first denominator 24, and no demonstrated repair-cost saving.
- [x] Historical 2026-10-15 metrics/bytes remain intact.

**Verification:** Re-run offline replay; compare 7/24, 2/2/17, 2/5, 5/24 and expected pre-refresh warning; inspect audit counts against raw JSON. No provider calls.

**Task 1 evidence (2026-10-20):** User plan approval was "ок"; this is a docs-only
slice, so no new tests or RED/GREEN behavior change is required. Offline replay at
HEAD `2326273e6ab7fcea89d1cf3b94b88a4be66ca5e9` reproduces strict validity 7/24,
TP/FP/FN 2/2/17, critical recall 2/5, verdict 5/24 and only the expected
`Static input path set changed; refresh the complete baseline` warning. A separate
raw-JSON audit confirmed all case/rule counts, zero reversed ranges, gateway
normalized acceptance 24/24, and mechanical counterfactual validity 14/24, 22/24,
24/24. All 25 response/manifest files, v2 prompt and its existing golden
`tests/fixtures/prompt_builder/sample.system.txt` match HEAD bytes exactly; v2 SHA
remains `9177e358c23d1a7cd8987cbf667cf8b705c2afb27eaf8b2237480e4531431d75`. Temporary evidence files: `/private/tmp/dmc-268-178-task1-replay.json` and
`/private/tmp/dmc-268-178-task1-audit.json` (audit script alongside them).

**Ownership coordination:** The user explicitly authorized four coordination
comments, posted by the coordinator before touching owned files: SD §15
[#228](https://github.com/larchanka-training/dmc-268-api-t6/pull/228#issuecomment-6291806047)
and [#220](https://github.com/larchanka-training/dmc-268-api-t6/pull/220#issuecomment-6291806510),
future seed-test changes
[#230](https://github.com/larchanka-training/dmc-268-api-t6/pull/230#issuecomment-6291804806),
and future TEST_PLAN changes
[#227](https://github.com/larchanka-training/dmc-268-api-t6/pull/227#issuecomment-6291805386).
The README has no open-PR owner. Task 1 was independently reviewed before the
coordinator dispatched Task 2. No further external comments or paid run are authorized.

**Required gates (all exit 0, 2026-10-20):**

- `uv run ruff check .` — all checks passed.
- `uv run ruff format --check .` — 518 files already formatted.
- `uv run mypy .` — no issues in 515 source files.
- `uv run pytest` — 4025 passed, 863 skipped, 2 dependency deprecation warnings,
  512.82 seconds. `TEST_DATABASE_URL` was unset: PostgreSQL integration, including
  the two prompt-seeding integration tests, was skipped in this run; this docs-only
  slice does not claim executed database seeding. Optional integration skips remain
  visible in the full test result.

Commands ran in the managed worktree via the original checkout's existing virtual
environment, `UV_PROJECT_ENVIRONMENT`, with
`UV_CACHE_DIR=/private/tmp/dmc-178-uv-cache` and `uv run --offline --no-sync` to
avoid dependency/environment changes. No lock/dependency changes. `git diff --check`
also passed. Independent Task 1 review reported **Standards 0, Spec 0** and
separately reconfirmed the raw matrix, strict/normalized distinction and 25
immutable baseline files. Task 1 completed before the coordinator dispatched Task 2.

**Later integration infrastructure:** An authorized dedicated ephemeral
PostgreSQL 17 container `dmc-268-178-seed-test` was initially ready on
`127.0.0.1:64909`; after Docker restart it was recreated on `127.0.0.1:52586`
for subsequent seed tests. It uses tmpfs data, automatic container removal and private
temporary connection configuration; no shared database was changed. After those
tests, stop the container and remove its private temporary config/env files.


**Dependencies:** user plan approval received ("ок"); SD ownership comments posted before edit.

**Files likely touched:** `test-prs-dataset/README.md`, `docs/SYSTEM_DESIGN.md`.

**Estimated scope:** Small: 2 files.

## Task 2: Specify and add immutable v3 examples

**Description:** With the authorized #230 seed-test coordination comment posted, write failing strict example/golden tests first, then add the new prompt, golden, and necessary exact seeder expectations atomically within this slice. Do not edit v2 or hand off an asset-only state that breaks seed tests.

**Acceptance criteria:**

- [x] V3 includes worked custom attribution and ordinary findings, null single-line/valid multi-line anchors, mixed severity and confidence ordering, and an empty answer.
- [x] Every example passes independent strict `validate_review_output` and gateway parsing; negative missing/wrong prefix, equal/reversed range and wrong-order variants fail strict validation as expected.
- [x] Golden fixture is exact full v3 content; v2 SHA stays `9177e358c23d1a7cd8987cbf667cf8b705c2afb27eaf8b2237480e4531431d75` and its golden remains unchanged. In the same completed slice, seed discovery/count expectations include six assets and first-seed active system v3, so the repository remains green.

**Verification:** RED: new v3 example/fixture tests fail before asset; GREEN: `uv run pytest tests/test_review_prompt_v3.py tests/test_review_prompt_examples.py tests/test_prompt_seeding.py tests/test_deploy_prompt_seed.py`; inspect prompt for a concrete construction sequence and scope boundary. Run all four required gates before this slice completes; execute configured PostgreSQL seed integration or identify its skip.

**Task 2 evidence (2026-10-20, complete; all gates, focused DB integration and independent review green):** Pre-edit check
confirmed remote main still `2326273e6ab7fcea89d1cf3b94b88a4be66ca5e9`, #130
unchanged/open and no open PR claiming the v3 asset/golden/test. The authorized
#230 comment covers the existing seed-test overlap. No additional comments sent.

- RED 1: the new full-asset/golden contract failed with missing v3 before asset
  creation (1 failed). GREEN 1: add v3 and exact golden, include six seed assets
  and active system v3 expectations (5 passed, 2 PostgreSQL tests skipped).
- RED 2: concrete custom-rule/mixed-order examples were absent (9 failed,
  1 passed). GREEN 2: focused v3/examples/seeding/deploy suite gives 21 passed,
  2 skipped. All five complete JSON fences (output shape + four answers) pass the
  independent strict validator and gateway parser without normalized changes.
  Six negative variants reject missing/wrong prefix, equal/reversed ranges and
  severity/confidence order. Anchors/suggestions match the shown invented files.
- V3/golden SHA-256:
  `eb851e7fc2329eb7bdff015ec0fda945c46111ec5c884934ed6f6d6803883452`.
  V2 SHA/golden, 25 raw response/manifest files, bootstrap default and snapshot
  checksum source remain byte-identical to HEAD (29 files checked). No schema,
  validator, normalizer, default or dependency change.
- Prompt size: v2 15,164 bytes / 15,062 characters; v3 21,076 bytes / 20,972
  characters (+5,912 bytes, about 39%). Character/4 heuristic grows from about
  3,766 to 5,243 tokens (+1,478); this is not measured provider token use or cost.
  The later default/eval selection must fit the actual v3 input before capture.
- The initial focused PostgreSQL attempt had two setup connection-refused
  errors because Docker stopped. The coordinator restored Docker Desktop; the
  lost temporary container was recreated independently on `127.0.0.1:52586`.
  Retried `tests/test_prompt_seeding.py tests/test_deploy_prompt_seed.py` with
  the dedicated DB URL: **8 passed in 2.69s, zero skips**. Disposable migrated
  schemas exercise first-seed six-asset insertion and active system v3, plus the
  existing v1-to-v2 unique-active regression. Extra v2-to-v3/idempotence/legacy-pin
  coverage remains Task 4. Connection values were redacted. The global pytest
  run keeps `TEST_DATABASE_URL` unset as directed.
- Ruff lint/format pass (519 files formatted), mypy passes (516 source files);
  global `uv run pytest` (DB URL unset) passes **4036 passed, 863 skipped,
  2 dependency deprecation warnings in 695.80s (11:35)**. PostgreSQL integration
  remains skipped in that global run; the focused seed/deploy DB run above
  actually executed all 8 tests without skips. Fresh independent review reports
  **Standards 0, Spec 0**. `git diff --check` passes. README/SD now describe v3
  without stale "not added" wording; default selection remains Task 3.

Task 2 is complete. No commit, push, model call or workflow dispatch occurred;
Task 2 was handed off before Task 3 dispatch. At that handoff the dedicated DB
was available for Task 4; its completed teardown is recorded under Task 4 below.

**Dependencies:** Task 1; **#230 must merge or an explicitly authorized coordination comment must exist before this slice edits `tests/test_prompt_seeding.py`**. The authorized #230 coordination comment linked in Task 1 is posted; recheck current file ownership before editing. Check #130/open branches have not allocated v3 before writing. On collision stop and report.

**Files likely touched:** `tests/test_review_prompt_v3.py`, `review/prompts/review.system.v3.md`, `tests/fixtures/prompt_builder/sample.system.v3.txt`, `tests/test_prompt_seeding.py`.

**Estimated scope:** Medium: 4 files.

## Checkpoint after Tasks 1–2

- [x] Review immutable-v2/golden evidence and strict examples (do not rely on production parse alone).
- [x] Focused tests pass; no schema, validator or normalizer relaxation.
- [x] Agent-loop independent review reports no standards/spec findings.

## Task 3: Select v3 for the existing eval path

**Description:** Test-first switch `DEFAULT_SYSTEM_PROMPT`; keep v1/v2 historical fixtures and explicit legacy tests intact.

**Acceptance criteria:**

- [x] Default bootstrap and mocked `eval_live` use v3, with matching manifest prompt path/version/hash.
- [x] Explicitly selected v2 remains supported; all active source pins include the actual selected prompt.
- [x] No provider calls or workflow mutation is needed to verify selection.

**Verification:** RED: default/manifest assertions expect v3 before changing default; GREEN: `uv run pytest tests/test_review_prompt_v3.py tests/test_eval_live.py tests/test_eval_live_workflow.py tests/test_eval_commit_provenance.py`. Run other affected bootstrap tests discovered by references. Re-run unchanged strict replay to confirm historical metrics remain unchanged.

**Task 3 evidence (2026-10-20, complete; all gates and fresh review green):** Pre-edit open-PR
file check found no owner for bootstrap/default eval/CLI test files; remote main
remained `2326273e6ab7fcea89d1cf3b94b88a4be66ca5e9`. No external comment sent.

- Meaningful RED at the real eval CLI without `--system`: manifest captured v2
  instead of expected v3 (1 failed, 1 explicit-v2 case passed). Only then switched
  `app/bootstrap/llm_gateway.py::DEFAULT_SYSTEM_PROMPT` to v3.
- Final focused GREEN: v3/eval/workflow/commit-provenance/bootstrap-CLI suite
  **172 passed in 34.44s**, using only fake/MockTransport boundaries. New CLI
  cases assert the actual full golden request content, literal path/version/SHA,
  only the selected prompt in static pins, exact raw answer and clean replay.
  Explicit `--system v2` remains supported; the existing `record()` helper and
  historical v2 provenance assertions remain unchanged.
- Initial expanded focus found two size-related fixture failures: the engine
  settings test's artificial fast 8k input budget left 7k/6.5k prompt space, and
  the HTTP-400 test's 16k fallback inherited an 8k output reservation. Scaled only
  artificial engine capacities (fast 10k, fallback window 12k) and explicitly
  matched fallback output cap to primary 4k, with comments. Exact derived budget,
  engine timeout/cost and primary+fallback 400 assertions remain. No production
  limits, model profiles or transport behavior changed.
- Historical offline replay reproduced strict validity 7/24, TP/FP/FN 2/2/17,
  critical recall 2/5 and verdict agreement 5/24, with the sole expected static
  path-set warning. Temporary report:
  `/private/tmp/dmc-268-178-task3-replay.json`. No raw bytes changed.
- Byte checks against HEAD cover 32 baseline/v2/schema/normalizer/settings/snapshot
  files. V3/golden remain exact at SHA
  `eb851e7fc2329eb7bdff015ec0fda945c46111ec5c884934ed6f6d6803883452`.
- Required Ruff lint and format checks pass (519 formatted), mypy passes
  (516 source files). Fresh independent review reports **Standards 0, Spec 0**;
  actual default CLI provenance and fixture capacities were checked. Global
  `uv run pytest` (DB URL unset): **4038 passed, 863 skipped, 2 dependency
  deprecation warnings in 440.78s (7:20)**. PostgreSQL integration remains skipped
  in that global run; this slice changes no seeding behavior. `git diff --check`
  passes. Task 3 completed before Task 4 dispatch. No provider call, workflow
  dispatch, commit or push occurred.

**Dependencies:** Task 2; inspect latest file ownership before edit.

**Files likely touched:** `app/bootstrap/llm_gateway.py`, `tests/test_eval_live.py`; extend `tests/test_review_prompt_v3.py` if needed. If assertions span more files, split the task rather than broad rewriting historical literals.

**Estimated scope:** Medium: 2–3 files.

## Task 4: Verify seed activation and legacy pins

**Description:** Extend the already green Task 2 seed coverage with v3 activation/idempotence and pinned-v2 regression tests. Necessary asset/count/active-version expectation updates are part of Task 2, not deferred here.

**Acceptance criteria:**

- [x] Discovery sees six assets, first seed activates system v3, repeat seed inserts zero and preserves immutability.
- [x] Existing persisted v2 content/checksum remains unchanged and previously pinned v2 run/snapshot remains v2; do not repoint snapshot checksum defaults as part of this legacy task.
- [x] Actual PostgreSQL seeding test verifies unique-active transition when TEST_DATABASE_URL is configured; skipped tests are identified explicitly if it is not.

**Verification:** Add extra activation/idempotence/legacy-pin regression first, record RED; GREEN: `uv run pytest tests/test_prompt_seeding.py tests/test_deploy_prompt_seed.py tests/test_policy_snapshot.py tests/test_policy_snapshot_postgres.py`. Use configured disposable integration schema only.

**Task 4 evidence (2026-10-20, complete; all gates and fresh review green):** Pre-edit check
confirmed #230 remains the only seed-test owner, covered by the authorized comment;
no open PR owned the new persisted-pin regression file. Remote main remains
`2326273e6ab7fcea89d1cf3b94b88a4be66ca5e9`.

- Added regression coverage before any implementation change. Existing behavior
  passed immediately: **no RED was manufactured and no application fix was needed**.
  Focused real PostgreSQL seed/deploy/policy/pinned-run suite: **26 passed,
  zero skips, 2 dependency warnings in 10.15s** (7 actual PostgreSQL integration
  cases plus 19 unit/static checks). All schemas were disposable and
  migrated on the dedicated container; no shared database or helper was modified.
- Upgrade coverage seeds five legacy assets, adds only v3 (one insert), then
  repeats the full six-asset seed (zero inserts). Persisted v2 retains its id,
  exact golden content/checksum and inactive status; exactly one system prompt
  is active at v3. A changed v2 checksum is rejected without overwriting old
  content or changing active v3.
- The new `test_review_prompt_pins_postgres.py` deliberately replaces the portal
  fixture's fake v1 with coherent actual-v2 setup before seeding real assets.
  Through `SqlAlchemyReviewPromptRepository.get_review_prompt_input`, the old
  persisted run resolves exact v2 content after v3 activates. Public
  `SqlAlchemyPolicySnapshotStore.get_pins` retains v2 id/version/checksum and the
  stored snapshot round-trips unchanged through its public read seam.
  Snapshot defaults/checksum pins remain v2; no v3 shortcut was added.
- Only two functional test files changed in this slice. Required Ruff lint/format
  pass (520 formatted); mypy passes (517 source files). Single global
  `uv run pytest` (DB URL unset): **4038 passed, 866 skipped, 2 dependency
  deprecation warnings in 341.27s (5:41)**. The new three integration regressions
  contribute three additional skips in this global run; they all executed in
  the focused real-DB run above. `git diff --check` passes. Fresh review is green below.
- Fresh independent review reports **Standards 0, Spec 0**, including coherent
  actual-v2 fixture and public persisted run/snapshot seams. No DB reruns requested.
- Teardown complete: dedicated `dmc-268-178-seed-test` stopped/automatically removed;
  private temporary connection config and env files deleted. Unrelated containers
  were untouched. The global pytest run retains no DB URL.
- Coordinator independently confirmed selected v3 SHA above, 46 active pins,
  static digest `sha256-v1:387834e02e34318527ee2c48e25e2fb63743b65a6b33a5739c25c7b877d8ca3a`,
  no verifier pins and unchanged remote main. New #231 is test-only with no
  overlap in these files/pins. Capture approval/source verification remains Task 5.

**Dependencies:** Tasks 2–3; **#230 must merge or an explicitly authorized coordination comment must exist before editing its owned `tests/test_prompt_seeding.py`**. The authorized #230 coordination comment linked in Task 1 is posted; recheck current ownership and reconcile merged work before editing.

**Files likely touched:** `tests/test_prompt_seeding.py`; if a dedicated pinned-run regression belongs elsewhere, split it into a separate ≤3-file slice.

**Estimated scope:** Small: 1–2 files.

## Checkpoint after Tasks 3–4

- [x] New eval selects v3; old pinned runs preserve v2.
- [x] Focused tests pass; integration execution/skips are stated accurately.
- [x] Agent-loop independent review reports no standards/spec findings.

## Task 5: Verify and publish the capture candidate before paid approval

**Description:** Rebase after relevant overlaps land, verify prompt/version/pins/runtime path, complete free gates and independent review, then delegate candidate branch creation/commit/push to the publisher. Verify and pin the remote commit before preparing a concrete paid-run request. No paid execution or automatic merge belongs to this task.

**Acceptance criteria:**

- [ ] Recheck #130/#217 status and open PR file lists (#230/#229/#228/#227/#220), #168 pinned changes and #206 warning behavior. Confirm legacy path, v3 version allocation, and #184/#133 merged ancestry; record exact source SHA and static path set.
- [ ] All four repository gates pass on capture candidate; independent review has zero findings. Delegate branch creation and candidate commit/push to the agent-loop publisher using the publishing skill (existing worktree branch may be reused if suitable). Verify the pushed remote source SHA contains v3, the golden/seeder updates and the v3 default; record its exact SHA and clean working-tree state. If eval now uses verifier, stop and revise scope/version/pins before spending; do not mechanically assume 46 or add unused verifier pins.
- [ ] Only after the candidate is pushed and remotely verified, present a separate tech-lead request naming its branch and exact remote SHA, exactly one complete 24-case Actions run, current primary/fallback profiles, raw export, proposed window and budget; approval is recorded before dispatch. Default to no early PR; Task 7 opens the final PR after the artifact import. If an early PR is explicitly needed, it must be draft and Task 7 updates that same PR.

**Verification:** `uv run ruff check .`; `uv run ruff format --check .`; `uv run mypy .`; `uv run pytest`; `uv run python review/scripts/validate_dataset.py --final`. Offline replay must still honestly display the old baseline warning before refresh. Check 24 active cases, SEC-05 excluded, no corpus/truth/model change. Run gate commands separately without dropping required checks. Verify the remote branch resolves to the reviewed candidate SHA and inspect its v3/default file contents; a local commit or uncommitted files cannot be the Actions capture source.

**Task 5 pre-capture free-check evidence (2026-10-20):** At coordinator request,
ran `uv run python review/scripts/validate_dataset.py --final --report-json
/private/tmp/dmc-268-178-task5-dataset-report.json` offline with the same existing
virtual environment and UV flags as above. Exit **0**; exact final summary
`cases: 24; valid: 24`, with every active case OK. SEC-05 remains excluded.
No source/corpus/truth mutation or model/provider call. This check plus unchanged
final source/gates permits candidate publication preparation; it does not authorize
paid capture. Remote candidate SHA/publisher steps and remaining freeze checks
are still pending Task 5.

**Dependencies:** Tasks 1–4; relevant merges/ordering stable. User can approve implementation without paid approval. No external comment may be sent without explicit authorization.

**Files likely touched:** `docs/plans/178-todo.md` for evidence; optional README/SD final freeze note. No implementation changes to files owned by open PRs.

**Estimated scope:** Small: 1–3 documentation files plus delegated candidate branch/commit/push; no paid call, no automatic merge.

## Task 6a: Execute the authorized complete capture and audit its artifact

**Description:** Dispatch exactly one approved run and verify the artifact before touching committed baseline data.

**Acceptance criteria:**

- [ ] Explicit tech-lead authorization covers Task 5's verified remote SHA, this exact window/budget and one run; recheck the branch still resolves to that SHA before dispatch. Actions `eval-live.yml` runs with `model=mistral-small-4`, `export_raw_responses=true`, and its recorded source SHA equals the approved remote candidate. If the ref moved, stop for source revalidation/authorization rather than capture different inputs.
- [ ] Exactly 24 raw response files plus manifest from one run/attempt; safe/raw manifests match, every response SHA/size matches safe metadata; baseline is publishable and all metadata/secret constraints pass.
- [ ] No automatic retry or partial/mixed import; failure stops further paid execution pending fresh approval.

**Verification:** Actions source SHA/run/attempt, artifact listing, byte/hash comparison; inspect effective primary/fallback/settings and 24-case mapping. Do not rewrite response bytes, digest or newline endings.

**Dependencies:** Task 5 and separate explicit paid-run approval. **Pending; not authorized by plan approval.**

**Files likely touched:** none during dispatch/download/audit; evidence in todo only. Download to temporary staging.

**Estimated scope:** Small: capture operation.

## Task 6b: Import one complete artifact and record measured results

**Description:** Preserve artifact bytes exactly and publish the full comparison honestly.

**Acceptance criteria:**

- [ ] Replace the complete `responses/` set with the audited 24 raw files and manifest byte-for-byte from Task 6a; leave historical bytes linked at their old revision.
- [ ] Double offline replay is deterministic and `warnings=[]`; offline metrics agree with live safe report after removing expected diagnostics. Compare validity, TP/FP/FN, critical recall, verdict against 7/24, 2/2/17, 2/5, 5/24 in README and TEST_PLAN.
- [ ] Capture SHA/run/attempt, prompt/version/hash, pins/digests, model/configuration and approval evidence are recorded; no unmeasured semantic or repair savings claim. A regression is reported for tech-lead disposition, never hidden by another capture.

**Verification:** `uv run python review/scripts/validate_dataset.py --final`; replay twice into separate temporary JSONs and compare; compare all imported bytes/hashes against the untouched artifact; re-run repository gates for final tree. Ensure hooks preserve exact response bytes.

**Dependencies:** Task 6a; #227 must merge or authorized coordination comment exist before editing its owned `docs/TEST_PLAN.md`; the authorized #227 comment linked in Task 1 is already posted. No pinned source changes after capture.

**Files likely touched:** Mechanical artifact import is one indivisible cohort of 25 files (`test-prs-dataset/responses/*.json`); split docs into the following separate 2–3-file slice: `test-prs-dataset/README.md`, `docs/TEST_PLAN.md`, `docs/plans/178-todo.md`. The 25-file exception is required by AC3's complete atomic capture, not independent feature work.

**Estimated scope:** Small mechanical import + separate medium documentation slice.

## Task 7: Publish and obtain post-merge main replay evidence

**Description:** After import/docs and final independent review/gates, the publisher commits and pushes the exact imported bytes/docs to the existing candidate branch, then creates the final PR. If an early draft was explicitly created in Task 5, update that same PR and make it ready only after final validation. Keep final main replay distinct from pre-merge evidence; no automatic merge is authorized.

**Acceptance criteria:**

- [ ] Independent Standards/Spec review is clean, all four gates pass on the final tree; publisher commits/pushes the imported cohort/docs and opens the final PR (or updates the same early draft), preserving the recorded capture source SHA as provenance. PR follows What/Why/How to verify/Refs with the case/rule audit and no auto-closing keyword/Development link; current-head approve and resolved threads precede merge.
- [ ] Branch remains up to date with main without introducing pinned drift. After merge the **required CI replay on main** succeeds with 24 cases, `warnings=[]`, including no `Static input path set changed`; record exact job/run/source SHA.
- [ ] Handoff identifies all AC evidence and board status. The tech lead closes #178 after checking; absent main replay evidence means AC3 remains incomplete even if the PR was published.

**Verification:** Inspect current-head review/thread checks, final main Actions required replay report and imported artifact hashes; confirm static/corpus digests match. If a new pin change intervened, report drift and obtain an explicit decision; never spend again silently.

**Dependencies:** Tasks 1–6b, successful publisher skill checks, merge by authorized process. No automatic merge is implied here.

**Files likely touched:** none required after merge; retain final evidence in authorized PR/issue handoff and update this checklist if appropriate without changing pinned inputs. External comments require explicit authorization unless the invoked publishing workflow specifically covers them.

**Estimated scope:** Small: publishing and evidence handoff.

## Final checkpoint

- [ ] All four AC rows in `178-plan.md` have concrete evidence.
- [ ] All four required gates pass; integration skips are disclosed.
- [ ] Complete artifact retains exact bytes; historical 2026-10-15 remains accessible.
- [ ] Main's required replay is clean; no verifier quality claim is made from reviewer raw-first evaluation.
- [ ] Tech-lead issue acceptance/closure remains separate from implementation completion.
