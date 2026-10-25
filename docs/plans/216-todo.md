# Task checklist: Reflection regressions and docs (#216)

Baseline: `e6d3077cfcb5584f8af0524ce4a0c8a658b2c5fb`.
Read `216-plan.md` first; its mutation IDs, exact coordination comments and scope
boundaries are part of every task. The user approved the plan/public seams and
exact original external actions with `ок` on 2026-10-25, then separately approved
the new #291 comment with `Да, опубликовать`. Task 0 is complete; implementation
may proceed under the plan. Evidence: `216-coordination.md`. Nothing below is
marked done on the basis of inspection alone.

All new regression tests use the approved public seams and literal independent
expectations. Existing-correct behavior uses mutant RED -> restored-code GREEN;
new behavior uses failing-test RED -> minimum-change GREEN. Use uv, follow the
TDD skill, annotate tests and keep one small slice active at a time. A fresh
review agent checks Standards and Spec after each completed task; findings return
to development before selecting the next task. Any task that grows beyond about
five source/test files must be split further. Evidence/todo updates are metadata,
not permission to expand implementation scope.

## Task 0: Approval and shared-file coordination

**Dependencies:** None. **Scope:** No implementation files.

- [x] User approves plan and public test seams (`ок`, 2026-10-25).
- [x] Exact coordination comments explicitly authorized; refreshed all open PR
      file lists and checked existing comments before posting once on
      #290/#280/#229/#227/#283/#277/#220. #252 merged and was omitted. New #291
      overlap separately authorized (`Да, опубликовать`) and commented; current
      source and docs shared-file gates are cleared.
- [x] #133's exact two body replacements separately included in explicit `ок`
      authorization, applied and re-read; each original target occurred once,
      updated bytes equal only the two replacements and test additions total 6472.

Evidence and actual comment URLs: `docs/plans/216-coordination.md` (2026-10-25).

**Verify:** No implementation/mutation touches another PR's owned file before its
required comment. No comments/body edits are claimed without tool confirmation.

## Task 1: Pristine baseline and mutation inventory

**Dependencies:** 0 for shared-file mutations. **Files:**
`docs/plans/216-verification.md` (new). **Size:** S.

- [x] `uv sync --locked`; prepare disposable dmc216 PostgreSQL/RabbitMQ with free
      ports and isolated test216 vhost as specified in plan; exact commands are in
      `216-verification.md`. Foreign host-5432 PostgreSQL was not touched.
- [x] Run full pristine gates with TEST_DATABASE_URL and TEST_RABBITMQ_URL and
      save counts/SHA: Ruff/format/mypy passed; pytest was 2 failed, 5,684 passed,
      0 skipped in 2,795.29s. Three unchanged performance reruns and three unchanged
      origin reruns passed; this does not make the original full gate green.
      No blanket skip acceptance or reuse of host 5432.
- [x] Before new tests/refactors, manually probe M01–M15 one at a time wherever
      applicable; restore each patch. Recorded 97 probes / 210 executions, exact
      patches/commands/results, existing killers and prospective final-test owners.
      Latest: 15 already killed, 4 equivalent by domain proof, 7 checksum-only
      semantic gaps, 71 surviving the unit importer corpus. Mark absent sites superseded with source/history proof.
      Execution refinement: a named focused killer is conclusive; focused survivors
      require recomputed importer/resource-reader coverage before preliminary gap
      classification. Green importer results are not whole-suite survivors or
      equivalence proof; full pristine/final gates remain required.

**Verify:** Full pristine tests measured, not assumed; the old 47-file importer
set has been recomputed; survivor classification distinguishes equivalent,
superseded, blocked and genuinely uncovered. No mutant remains in git diff.

## Task 2: Decision references and budget/checksum boundaries

**Dependencies:** 1. **Files:** `tests/test_reflection_pass.py`,
`tests/test_policy_budget.py`. **Size:** S.

- [x] M01/M02 present-day stable/foreign/duplicate-ID invariants and M03 all-status
      reference validation are covered without restoring obsolete slug behavior.
- [x] M04 exact send budget versus +1 and system+user counting use actual public
      prompt/verify calls; distinguish early mandatory budget from final guard.
- [x] M05 empty checksum and M06 exact policy cap/+1 have focused killer tests and
      assert no model call on rejection plus literal error code/limit/actual.

**Verify:** `uv run pytest tests/test_reflection_pass.py tests/test_policy_budget.py`;
replay each corresponding isolated mutant and record the named killer.

Task 2 proof: the affected suites passed **62 tests**; Ruff lint/format and mypy
passed. Seventeen relevant isolated mutations each produced named RED failures
and restored-code GREEN passes; exact commands/counts/nodeids are in
`216-verification.md` task 2. Production and prompt bytes remain unchanged.
New cases cover canonical E2/e2, rejection before provider for duplicate IDs,
supplied refs for both non-supported statuses, real final-send 152/151 and early
mandatory 954/953 limits, blank checksum, and default policy 12,000/12,001 cap.
Existing killers were reused for the already-covered reference/system cases.
The exploratory builder mandatory-envelope equality probe changes diagnostics
only because zero code budget rejects next; its wording is intentionally
unconstrained, outside the original final-guard mutation. Its measured baseline
outcome is preserved, not called strict equivalence. Independent task 2 Standards
and Spec reviews both returned zero findings, confirmed by the parent. The
final full service-backed gate remains in task 16.

## Task 3: Prompt escaping, long policies and manifests

**Dependencies:** 1; can precede 2 for unowned files. **Files:**
`tests/test_prompt_builder_markdown.py`, `tests/test_policy_snapshot.py`,
`tests/test_reflection_evaluation.py`. **Size:** M.

- [x] M07 service-default name and check text independently resist quote/tag
      breakout in reviewer/verifier prompts; name probe reaches escaping rather
      than failing unknown-rule validation first.
- [x] M08 >200-line document asserts actual lines 200/201/final in both rendered
      prompts and late requirement validation, so manifest line_count alone is
      insufficient to pass.
- [x] M12 noncontiguous ranges assert sum; no snapshot -> run_id=None; missing
      stage/blocks rejected. Mutate each field/count behavior separately.

**Verify:** Focused three-file pytest: 74 passed; expanded task 2/3 boundary
suites: 136 passed. All seven isolated M07/M08/M12 probes were RED and restored
GREEN after final formatting. Ruff check, format check and mypy pass. Exact
commands, final killers and counts are in `216-verification.md`, task 3.
Implementation, focused proof and fresh task 3 independent review are complete;
Standards and Spec both returned zero findings (confirmed by parent).

## Checkpoint A: Boundary proof

- [x] Fresh independent task 3 Standards and Spec reviews both returned zero
      findings, confirmed by the parent before synchronization.
- [x] Current mutation inventory is complete through M08/M12; no hidden survivor.
- [x] After synchronization onto `74b42a7134fb9f272d93c913344e5b826b03b523`,
      Ruff check, format check (566 files), mypy (556 source files) and the five
      affected boundary suites (136 passed) all pass. Fresh independent task 3
      Standards and Spec reviews both returned zero; all five test-file bytes
      match those reviewed before synchronization. The final complete pytest
      gate with both services remains mandatory in task 16.

Checkpoint A is complete: focused boundary checks on synchronized main and
both independent task 3 review axes are green. Task 16 still owns the final
complete pytest gate with both services. No full-suite success is claimed here.

## Task 4: Publication boundary and threshold interaction

**Dependencies:** 2, 3. **Files:** `tests/test_execute_review.py`. **Size:** S.

- [x] M09 real ReflectionPass with snapshot proves insufficient_context cannot
      reach publisher while supported findings can; assert recorded drop reason.
- [x] M10 first-valid/later-invalid attribution drops only invalid candidate;
      AGENTS.md and pinned stable service-defaults positive paths preserve names.
- [x] M15 existing combined critical-threshold regression is mutation-sensitive;
      strengthen only missing observable assertions or add a distinct retained
      high-severity case if needed. Assert exact summary, findings/drop reasons.

**Verify:** `uv run pytest tests/test_execute_review.py`; M09/M10/M15 replay proves
publisher observations change under each regression, with no live network.

Task 4 proof: 49 affected-file cases pass; six isolated final replay variants
are RED at publication assertions and restored GREEN. Ruff/format/mypy pass
(566 formatted files / 556 typed sources). See task 4 verification ledger.
Both fresh independent review axes reported zero findings (Standards and Spec).

## Task 5: Schema/model contract parity

**Dependencies:** 1. **Files:** `tests/test_schemas_v2.py`,
`tests/test_review_output_v2.py`, `review/schemas/review-output.schema.v2.json`;
`review/schemas/verifier-output.schema.v1.json` only if a demonstrated parity bug
requires it. **Size:** M.

- [x] M13 shared public parse examples detect maxLength, object closure and array
      bounds in both models and schemas, including widening and narrowing probes.
- [x] rule_name 128/129 and candidate_id 64/65 use independently specified
      boundary strings; retain explicit runtime-only cross-field rules.
- [x] Add intentional schema pattern fix for current lexical rule_name domain;
      allow null/E2/FSD1 and reject Security, empty ID and trailing newline. M11
      model/parser newline rejection is proved independently of schema validation.

**Verify:** `uv run pytest tests/test_schemas_v2.py tests/test_review_output_v2.py`;
Draft202012 schema validation and Pydantic examples agree on encoded constraints;
replay M11/M13 including mutated JSON files, restoring every patch.

Task 5 proof: 80 affected-file cases pass. The initial schema defect was RED
(7 invalid lexical cases accepted) before the one-pattern fix. The isolated
mutation replay kills all 49 applicable M11/M13 baseline variants plus the new
schema dollar-anchor variant; each restoration returns 80 passing cases. Two
minItems:0-removal equivalents retain their original domain proof. Independent
Standards and Spec reviews both returned zero findings (confirmed by parent).
See task 5 verification ledger.

## Task 6: Verifier prompt semantic regression checks

**Dependencies:** 1. **Files:** `tests/test_reflection_evaluation.py`. **Size:** S.

- [x] M14 checks trust rules, statuses, 1:1 decisions, check_id sources and JSON
      example in explicitly selected immutable v1 and current v2 resources.
- [x] Remove each semantic clause/example in a temporary mutant: meaningful test
      fails without relying only on the existing checksum failure.
- [x] No prompt bytes/checksum pins or paid eval inputs change in final diff.

**Verify:** `uv run pytest tests/test_reflection_evaluation.py`; record semantic
killer assertions separately from checksum-only killers.

Task 6 proof: 55 affected-file cases pass (14 semantic cases replace one shallow
historical trust case, net +13). All 14 baseline M14 deletions plus four focused
code-evidence/v2 unsupported-attribution deletions fail at semantic assertions,
with no checksum-only failures, and restore GREEN. Both published verifier
resources match HEAD byte-for-byte; prompt pins and the approved task 5 schema
correction are preserved. The eight task 2–6 acceptance suites pass 278 cases;
Ruff check, format check (566 files), and mypy (556 sources) pass. Exact patches,
named assertions and outcomes are in the task 6 verification ledger. Fresh
independent task 6 Standards review returned zero findings (confirmed by parent);
Spec also returned zero findings after final metadata review (confirmed by parent).

## Checkpoint B: Acceptance tests

- [x] All live M01–M15 targets are killed or already-covered; vanished historic
      sites are clearly superseded. Genuine equivalence requires documented proof.
      Task 12 closes the separate M11 postprocessor/public-validator target with
      independent old-site and shared-validator RED/GREEN proofs at both public
      boundaries; final replay/full gates remain in task 16.
- [x] Affected task 2–6 acceptance regressions pass (278 cases), plus Ruff check,
      format check (566 files) and mypy (556 sources). Mutation evidence is
      preserved before hygiene changes; the final full four-gate proof remains
      in task 16 with both services.
- [x] Fresh independent task 6 Standards/Spec review has no unresolved findings.

## Task 7: Public prompt rendering surface

**Dependencies:** 2, 3, 6. **Files:**
`app/modules/reviews/application/prompt_builder.py`, `prompt_budget.py`,
`reflection_pass.py`, `tests/test_reflection_pass.py`, `tests/test_prompt_builder.py`.
**Size:** M (five files).

- [x] All external callers use named public render APIs rather than private _render_*,
      including four changed-file renderer calls in test_reflection_pass.py and one
      in test_prompt_builder.py. Do not preserve private aliases just for tests.
- [x] Reflection reuses existing public xml_text/xml_attribute; common policy
      rendering removes rules/agents duplication with identical prompt bytes.
- [x] Existing envelope, checksum, injection and budget tests remain green.

**Verify:** Focused builder/budget/reflection/evaluation suites, byte-preservation
assertions placed in test_prompt_builder.py, and `rg` for all remaining external
private render/escaping references. Task 7 does not add another markdown-test edit.

Task 7 proof: five public renderer methods replace every external private renderer
call; the shared policy-document helper preserves missing, empty and present
bytes. Reflection uses the existing public XML escaping functions. Five new
literal cases plus the migrated changed-file case were RED before each public
API promotion and GREEN after it. The seven affected suites pass 216 cases;
Ruff check, format check (566 files), and mypy (556 sources) pass. Six paired
reviewer/verifier contexts retain identical system/user byte hashes before and
after consolidation. No production test factory consumer was added. Independent
task 7 Standards/Spec review completed with zero unresolved findings on both axes; M11's public-validator seam remains
task 12 and the final full four-gate run remains task 16.

## Task 8: Typed policy budget defaults

**Dependencies:** 2, 7. **Files:** `app/modules/reviews/application/prompt_budget.py`,
`policy_snapshot.py`, `tests/test_policy_budget.py`. **Size:** M.

- [x] Remove dead ExecutionProfile dict/get fallback; typed field is authoritative.
- [x] Share named unchanged default policy cap without overriding a run's cap.
- [x] Boundary tests and fast/deep profiles preserve configured behavior.

**Verify:** Policy budget/snapshot tests and M06 replay after the refactor.

Task 8 proof: `DEFAULT_MAX_POLICY_TOKENS` is the unchanged literal 12,000 and
supplies all three current default declarations, including the temporary
complete-profile factory. The pre-send guard reads the typed run field directly.
Four public fast/deep cases accept exactly-at-cap and reject cap+1 for both 200
and 12,001; two literal default assertions preserve both profile defaults. The
original default 12,000/12,001 regression remains unchanged. Current M06 and
hardcoded/default-value mutants fail the intended assertions and restore GREEN.
Eight affected suites pass 237 cases; Ruff check, format check (566 files), and
mypy (556 sources) pass. Fresh independent task 8 Standards/Spec reviews both
completed with zero unresolved findings; the final full four-gate run remains task 16.

## Task 9: Test factory helper and first migration batch

**Dependencies:** 8. **Files:** `tests/policy_snapshot_fakes.py` (new),
`tests/test_policy_snapshot.py`, `tests/test_prompt_builder_markdown.py`,
`tests/test_policy_budget.py`. **Size:** M.

- [x] Copy five make_* factories into tests helper; preserve exact fixture values.
- [x] Migrate these three consumers; production definitions remain temporarily
      for unmigrated tests. Add no production import of tests.
- [x] Checksum registry, PolicyDocument/Bindings/Profile contracts stay in app.

**Verify:** Focused migrated suites, import/typecheck, no production behavior diff.

Task 9 proof: a consumer import first failed with ModuleNotFoundError before the
helper existed. All five copied factory ASTs and signatures match the retained
production definitions exactly; 29 temporary output-equivalence cases cover
normalization, counts, checksums, IDs, overrides and fast/deep profiles. All three
consumers, including the local markdown-test import, now use the tests helper.
The three migrated suites pass 60 cases. Ruff check, format check (567 files),
and mypy (557 sources) pass. Production bytes remain unchanged from task 8, and
there is no application import of tests. Fresh independent task 9 Standards/Spec reviews both completed with zero
unresolved findings (confirmed by the coordinator); the final full four-gate
run remains task 16.

## Task 10: Reflection factory migration batch

**Dependencies:** 9. **Files:** `tests/test_reflection_pass.py`,
`tests/test_reflection_evaluation.py`, `tests/test_execute_review.py`. **Size:** M.

- [x] Switch fixture factory imports to tests helper with no fixture drift.
- [x] Preserve new regression names and mutant killers across import changes.
- [x] No #130 interface adaptation is required on the unchanged approved base;
      any later merged-interface adaptation still requires rebase and overlap checks.

**Verify:** Three focused suites plus typecheck; compare fixture outputs unchanged.

Task 10 proof: all three consumers import the same four factories from the tests
helper; non-import ASTs match the captured task 9 state. No test behavior, fixture
arguments, regression names, assertion or application bytes changed. In a fresh
process, removing all five retained production factory attributes in memory
produced three collection errors before migration, then 144 passing cases after.
The ordinary three suites pass 144 cases; Ruff check, format check (567 files),
and mypy (557 sources) pass. Task 9's five-factory equivalence proof still applies
because both factory implementations are unchanged. Fresh independent task 10
Standards/Spec reviews both completed with zero unresolved findings (confirmed
by the coordinator); the final full four-gate run remains task 16.

## Task 11: Remaining factories and production cleanup

**Dependencies:** 10. **Files:** `tests/test_repo_conventions.py`,
`tests/test_llm_gateway.py`, `tests/test_rules_md_injection.py`,
`app/modules/reviews/application/policy_snapshot.py`, and newly discovered
`review/scripts/eval_live.py`. **Size:** M (five paths).

- [x] Migrate remaining three known consumers; re-scan whole tree for new users.
- [x] Remove five factory definitions and now-dead fixture-only constants/imports
      from app; retain registry/contracts used by production.
- [x] Whole-tree typing exposed one additional evaluation consumer outside
      app/tests. The coordinator approved a five-path refinement after checking
      that none of the nine current open PRs owns it. It now constructs public
      dataclasses locally with no tests dependency; no compatibility app re-export.

**Verify:** Migrated suites, `rg` import audit, ruff/format/mypy and independent
review; the final complete pytest gate remains in task 16.

Task 11 proof: all nine migrated test suites pass **459 cases**, including the
PostgreSQL conventions test with the dedicated service environment. Five factory
ASTs/signatures and 29 outputs were rechecked before deletion. Only those five
functions, four fixture constants and the dead hashlib import were removed from
production; all seven contract classes, the checksum registry, defaults and
remaining production AST match the captured task-start state. The test helper is
byte-identical to task 9. Whole tracked Python import audit finds no remaining
production factory import. For the extra evaluation consumer, 12 complete
snapshots and eight fake-transport request envelopes are identical across policy
arms and fast/deep profiles. Ruff/format (567 files)/mypy (557 sources) pass.
The offline eval suites yield **331 passed, 2 failed**: both failures also occur
in exact pristine-base workflow/test copies because /bin/bash 3.2 rejects an
empty array under nounset; a scratch-only Bash 5 diagnostic passes both cases.
The canonical result remains red, and neither workflow nor its tests was edited.
Fresh independent task 11 Standards/Spec reviews both returned zero unresolved
findings (confirmed by the coordinator). The inherited macOS Bash workflow
failures remain accurately red pending the final Linux environment proof in task 16.

## Task 12: Public strict service-defaults ID validation

**Dependencies:** 5. **Files:** `app/modules/reviews/application/review_output.py`,
`findings_post_processor.py`, `tests/test_review_output.py`,
`tests/test_findings_post_processor.py`. **Size:** M.

- [x] Replace cross-module private regex import with coherent public validator.
- [x] Preserve lexical/full-string semantics; stable ID membership stays in
      manifest/default-version validation, not generic parser/post-processor.
- [x] M11 trailing newline is rejected through parser and public post-processor
      output; probe both former regex sites independently.

**Verify:** Review-output v1/v2 and findings post-processor suites; M11 replay.

Task 12 proof: `is_valid_service_defaults_id` preserves the original private
regex's nonempty, full-string ASCII contract and is shared by the v2 model and
attribution repair. Eleven public postprocessor cases prove positive stable and
unknown lexical IDs remain verbatim, while malformed attribution is cleared
without dropping or otherwise changing the finding. The Task 5 model/parser
newline test is reused unchanged. Scratch-only direct dict/string/bytes parser
assertions separately verify calls beyond that test's first model assertion.
Independent old-parser relaxation gives **8 failed / restored 8 passed**; old
repair relaxation gives **2 failed, 9 passed / restored 11 passed**. The final
shared-helper relaxation gives **10 failed, 9 passed / restored 19 passed**, with
named model, parser and attribution failures. Review-output v1/v2 plus
postprocessor suites pass **82 cases**; Ruff, format (567 files), mypy (557
sources) and diff-check pass. Only three of the four allowed source/test paths
changed; test_review_output.py needed no duplicate coverage. Fresh independent
task 12 Standards and Spec reviews both returned zero unresolved findings
(confirmed by the coordinator before task 13). No full-suite success is claimed.

## Task 13: Missing reflection is a server assembly error

**Dependencies:** 4. **Files:** `app/modules/reviews/application/execute_review.py`,
`tests/test_execute_review.py`; `tests/test_review_run_worker.py` or
`tests/test_check_runs.py` only if existing public classification evidence is
insufficient. **Size:** M (<=4).

- [x] Missing reflection with snapshot reports existing internal_error;
      no review/verifier/publisher call occurs.
- [x] Incomplete/corrupt snapshot still reports SNAPSHOT_INTEGRITY_ERROR.
- [x] Existing failure classification/check-run author text correctly describes
      server fault without inventing a new public error code.

**Verify:** Execute/failure/check-run focused tests and new behavior RED/GREEN.

Task 13 proof: the existing snapshot-without-reflection public execution test
first fails on the obsolete policy-snapshot code, while the incomplete-snapshot
companion remains green. The minimal guard now raises existing
`RunFailure("internal_error")`. Both tests pass after the fix and exercise public
`classify_failure` / `failure_reason` with distinct literal author-facing texts.
No draft or publication occurs; absent reflection has no verifier by
construction, and the incomplete-snapshot fake verifier remains uncalled.
Existing unknown-counter corruption still raises snapshot integrity. Complete
execute/worker suites pass **177 cases**; Ruff, format (567 files), mypy (557
sources) and diff-check pass. Task-only before bytes, patch and RED/GREEN logs
are in `/tmp/dmc216-task13`. Fresh independent task 13 Standards and Spec
reviews both returned zero unresolved findings, confirmed by the coordinator
before task 14. Final full-suite proof remains task 16.

## Task 14: Remove unused allowed_v2 local

**Dependencies:** 4, 12. **Files:**
`app/modules/reviews/infrastructure/run_repository.py`,
`tests/test_sqlalchemy_run_repository.py` only if a new behavior assertion is
needed. **Size:** S.

- [x] Inline None, retain fail-closed explanation and all legacy output behavior.
- [x] Snapshot-row presence alone never enables v2 attribution.
- [x] Do not change transactions, persistence or #130 wiring in this slice.

**Verify:** Existing repository/post-processing tests; real PG if marked
integration. Do not create a test that merely asserts the local variable vanished.

Task 14 proof: the only source edit inlines `allowed_v2_rules=None` and removes
its constant local. The existing explanatory comment and DiffMap behavior are
unchanged. Existing repository/postprocessor suites pass **56 cases before and
56 after**; the named legacy fake-session test asserts `None` and strips all
three v2 attribution forms. It does not create a real snapshot row, and neither
selected file is marked integration or requires service URLs. Ruff, format
(567 files), mypy (557 sources) and diff-check pass. Task-only before bytes,
patch and logs are in `/tmp/dmc216-task14`. Fresh independent task 14 Standards
and Spec reviews both returned zero unresolved findings, confirmed by the
coordinator before synchronization. Task 16 retains final real
PostgreSQL/RabbitMQ full-suite proof.

## Checkpoint C: Hygiene complete

- [x] P3 9/10/11/16/17/20 fixes retain their historical proof; compatibility
      after merged #290/#130 is restored by integration A and B. Both have
      fresh independent Standards and Spec reviews at zero findings, confirmed
      by the coordinator. Integration B records 661 affected cases green.
- [x] P3 19 explicit rejection and residual disk I/O recorded for PR body;
      Task 15 documentation and both fresh review axes are complete.
- [x] Affected hygiene regressions, focused repository DB coverage where relevant,
      ruff/format/mypy and independent review pass before docs claim final behavior.
      Task 16 retains the full quiescent pytest gate with both services.

## Integration A: Preserve merged #290 verifier interfaces

**Dependencies:** 14 and synchronization onto
`4faa03dd7accfa161048f584b834b314fa1ab5b6`. **Scope:**
`tests/test_reflection_pass.py`, `tests/test_reflection_evaluation.py`,
`tests/test_execute_review.py`,
`app/modules/reviews/infrastructure/run_repository.py` (at most four paths).

- [x] Resolve the one Reflection-test conflict with all original #216 test
      functions and all upstream-added protocol/failure helpers and tests intact.
- [x] Adapt the added publication fake to typed `manifest`/`context`; the other
      two suites already accept upstream protocol arguments. Preserve
      `verify_recorded`, `VerificationRecord`, `VerifierReply` and trace fields.
- [x] Replace obsolete Until-#130 comment with current legacy-adapter fail-closed
      rationale; preserve inline `None` and new trace persistence.
- [x] Three affected suites pass 157 cases; Ruff and format (591 files) pass;
      scoped mypy over app and those suites passes (261 sources). Whole-tree
      mypy remains red with nine missing-factory errors in the three B files.
- [x] Fresh independent Standards and Spec reviews have zero unresolved findings,
      confirmed by the coordinator before integration B.

Task A evidence is appended to `216-verification.md`; prior `74b42a7` proof is
historical and remains unchanged. No full-suite or global-typecheck success is
claimed before integration B.

## Integration B: Migrate newly merged factory consumers

**Dependencies:** A and its independent review. **Scope:**
`tests/test_gateway_answer_validation.py`, `tests/test_gateway_verifier_model.py`,
`tests/test_llm_verify_trace.py` (three paths).

- [x] Migrate new test-only factory imports to `tests.policy_snapshot_fakes`;
      retain all upstream gateway/trace test behavior and production contracts.
      The coordinator approved a same-file fixture adaptation in the raw-index
      semantic regression: the approved rule-name schema now rejects its old
      input before model validation, so use schema-valid `line=30/start_line=31`
      and retain the exact model-error assertion at raw finding index 0.
- [x] Rescan all 594 tracked Python files: no removed-factory imports or
      definitions remain in application code, no app-to-tests dependency; all
      twelve test consumers import the helper. Nineteen affected suites pass
      661 cases with no skips, including PostgreSQL trace rollback. Global Ruff,
      format (591 files), mypy (581 sources) and diff whitespace checks pass.
- [x] Fresh independent Standards and Spec reviews both have zero findings,
      confirmed by the coordinator. Checkpoint C compatibility is green; task 16
      retains the complete quiescent pytest gate with both dedicated services.

## Task 15: Documentation and historical correction proposal

**Dependencies:** 4–14 (or final recorded dispositions). **Files:**
`review/README.md`, `docs/TEST_PLAN.md`, `docs/plans/120-plan.md`,
`docs/plans/120-todo.md`. **Size:** M.

- [x] All issue part-2 local corrections are applied using current main behavior:
      current verifier v2, qualified evidence contract, legacy evidence stripping,
      correct injection-test attribution/[INST] wording and actual log/persistence.
- [x] Correct historical review IDs 5557688806/5568780909 and obsolete slug-test
      claim; preserve historical narrative without attributing #230 work to #133.
- [x] Apply only separately authorized #133 exact body replacements or document
      their external deferral in our PR; include 6472-addition provenance.

**Verify:** Read docs beside actual code and named tests; links resolve; #211/#277
neighboring text is preserved; all claimed tests/evidence exist. No speculative
current-state edits to PS/CVS/RFS/SD or unrelated historical plans.

**Measured result:** Four approved documentation paths updated on base `4faa03d`;
70 source/reference checks and `git diff --check` pass. Both review URLs were
GET-verified. Historical #133 body corrections were already published and
verified separately: any rejection (including partial) and 6,472 added test
lines across thirteen files. P3 19 is explicitly rejected with the approved
resolver-boundary rationale and residual runtime package I/O, now documented
in README. Fresh independent Task 15 Standards and Spec reviews both returned
zero unresolved findings, confirmed by the coordinator. Final complete
service-backed gates, final review and publishing remain Task 16. No runtime
files changed.

## Task 16: Final proof, independent review and publishing handoff

**Dependencies:** 0–15. **Files:** `216-verification.md` and this checklist;
implementation files only for separately reviewed fixes. **Size:** S.

- [x] Full four gates measured on `4faa03d` with TEST_DATABASE_URL and
      TEST_RABBITMQ_URL: 6,521 passed, 0 failed/skipped, two warnings in
      1,680.47s; 594 runtime/test/resource/config files identical before/after.
- [x] Refresh origin/main and all open PR files; adopt documentation-only main
      `f8b13103f908b26e3e210c3f78b125f21bfbdaba` with all 35 intended paths,
      index and cumulative patch preserved. Runtime/tests/resources/config/lock
      remain identical, so the measured full result carries forward; no full
      pytest rerun at f8b1310 is claimed. Post-sync statics pass (591/581).
- [x] Fresh whole-change Standards and Spec reviews: zero findings on each axis;
      fresh documentation-sync Standards and Spec reviews also zero each.
- [x] Replay all 103 applicable final behavioral mutations, verify restoration,
      and account for four true equivalents, two historical superseded sites
      outside the original 97 and the diagnostic-only exploratory M04 probe.
- [x] Publisher follows pull-request skill: intended files only, issue-referenced
      conventional commit, push, PR with template and evidence/dispositions,
      attach PR to chat and report URL. Honor required user readiness/reviewer
      choice; update board 12 status only for this issue, no merge/closure.

**Verify:** PR body What/Why/How to verify/Refs, <=72-character conventional title,
no closing keyword/Development link, no mutants/scratch/.env, no --no-verify.
Approving human review on current head and resolved threads remain merge gates.
Clean up only dmc216's disposable services/resources; preserve foreign services.

## Issue acceptance coverage

| #216 acceptance criterion | Tasks / completion evidence |
| --- | --- |
| Every part-1 mutation kills a test; equivalent reasons in PR | 1–6 and final 16 complete: 103 named behavioral RED/restored GREEN probes; four equivalents, diagnostic-only M04 and two historical superseded sites kept distinct |
| Snapshot insufficient_context publication and Reflection + severity | 4; existing threshold test plus recorded killer, new insufficient boundary test |
| README/TP/plans describe current main; legacy evidence loss documented | 15; refresh after #130/#211 merges; verified historical links |
| Every hygiene item fixed or rejected with reason in PR | 7–14 fixes; P3 19 deliberate retained-loader reason in 15/16 |
| Gates with TEST_DATABASE_URL; PR/review/board DoD | Final complete gates green with own PostgreSQL/RabbitMQ; whole-change/sync reviews 0/0; PR #303 published and attached, issue #216 board status verified On Review; human merge gates remain |


### Task 16 measured gates and synchronization status

The complete quiescent Linux run on `4faa03d` is green. The later f8b1310
synchronization changes 44 upstream documentation paths with zero overlap with
our 35 paths; runtime/test/resource identity carries the measured result to the
current tree. This is a preservation argument, not a second full-suite run.
All 1,168 tracked/nonignored working-tree paths stayed unchanged during the
post-sync static gates. The original e6d3077 full run's two failures remain
historical evidence. Final 103-probe replay is now complete: 103 unique named
behavioral failures, 103 restored GREEN checks and exact restoration of all 610
scratch source/resource fingerprints, with live source still matching. All proof
work is complete. Publication is complete: [PR #303](https://github.com/larchanka-training/dmc-268-api-t6/pull/303)
is open and ready for review with no reviewer requested, as chosen by the user.
The PR is attached to this chat and exact issue #216's board 12 status was
GET-verified as `On Review` on 2026-10-25. Human approval on the final head and
resolved review threads remain required before merge; the issue remains open.
