# Implementation Plan: #178 — first-answer format and one complete baseline

## Overview and approval boundary

Improve first raw reviewer answers for the existing legacy ReviewOutput contract with a new immutable `review.system.v3` prompt, preserve strict raw-first measurement, and replace the complete 24-case baseline once the inputs are stable. Do not introduce normalization or relax validators. The user approved this implementation plan in chat with "ок" on 2026-10-20 under the invoked agent-loop skill. A paid run requires a separate explicit tech-lead approval specifying one full run, its window and budget. Plan approval does not authorize that run or any external coordination comment.

Recommended order: deliver legacy v3 before the #130 snapshot prompt migration; reserve the later next version for #130 after checking current assets and open work again. Do not activate a snapshot/evidence_refs prompt against the legacy envelope. A version collision or decision to land #130 first is a pre-capture stop condition requiring a revised ordering decision, not an invitation to edit immutable v2 or quietly recapture twice.

## Baseline and investigation evidence

Planning worktree HEAD and origin/main both equal `2326273e6ab7fcea89d1cf3b94b88a4be66ca5e9`, the latest remote main confirmed by the coordinator at kickoff. No implementation changes were made. Baseline is run `38701845865`, 2026-10-15, primary `mistral-small-4`, engine `fast`, prompt v2 SHA-256 `9177e358c23d1a7cd8987cbf667cf8b705c2afb27eaf8b2237480e4531431d75`.

Offline `uv run python review/scripts/eval_replay.py --report-json <private-output>/replay.json` reproduced validity 7/24, micro TP/FP/FN 2/2/17, critical recall 2/5, verdict agreement 5/24. Warning: `Static input path set changed; refresh the complete baseline`. This is expected current drift, not a successful post-refresh replay. Replay used `UV_CACHE_DIR=<task-private-cache>`, the original checkout's existing virtual environment through `UV_PROJECT_ENVIRONMENT=<existing-virtualenv>`, and `uv run --offline --no-sync`; neither dependencies nor original checkout files were changed. No provider calls or workflow dispatch occurred.

### All 17 invalid cases, by rule and defect

Rule abbreviations: NC = Naming Consistency; CA = Clean Architecture Boundaries; MC = Meaningful Comments for Complex Logic; CF = Complex Function Improvement Suggestions; EH = Error Handling Standards; FSD = FSD Layer Boundaries. Counts in the attribution column count findings lacking the required prefix. Equal ranges count `start_line == line`; there are zero reversed ranges.

| Case | Attribution omissions by rule | Equal ranges | Wrong order |
| --- | --- | ---: | --- |
| CLEAN-01 | NC ×2 | 0 | no |
| CLEAN-04 | NC ×2 | 2 | no |
| LOG-01 | NC ×1 | 0 | no |
| LOG-03 | NC ×2 | 0 | no |
| LOG-04 | none | 1 | no |
| LOG-05 | NC ×1, MC ×1 | 0 | no |
| RES-01 | CA ×1 | 2 | no |
| RES-02 | NC ×1 | 0 | no |
| RES-03 | NC ×1, CA ×2, CF ×1 | 3 | yes |
| RES-04 | none | 1 | no |
| RES-05 | NC ×1 | 0 | yes |
| SEC-02 | NC ×2, EH ×1 | 0 | no |
| SEC-04 | NC ×1 | 1 | no |
| SYN-02 | NC ×1, FSD ×1 | 2 | no |
| SYN-03 | NC ×1, MC ×1 | 2 | no |
| SYN-04 | NC ×8 | 8 | no |
| SYN-05 | NC ×1 | 0 | no |

Totals: 15 cases / 33 attribution omissions; 9 cases / 22 equal ranges; 2 cases with ordering defects. Disjoint groups: attribution only 7; attribution + range 6; attribution + range + order 1 (RES-03); attribution + order 1 (RES-05); range only 2. Rule totals: NC 25, CA 3, MC 2, CF 1, EH 1, FSD 1. Names occur in the checked-in default backend/frontend rule sets; valid names do not establish semantic applicability or a true violation. Range defects by `rule_name`: NC 13, CA 3, null 3, CF 1, FSD 1, MC 1.

The seven valid cases are CLEAN-02, CLEAN-03, CLEAN-05, LOG-02, SEC-01, SEC-03 and SYN-01. There are no empty/missing answers or JSON parse failures. Counterfactual mechanical fixes are not model measurements: prefix-only would yield 14/24 valid, prefix + equal-range would yield 22/24, and also ordering would yield 24/24. Do not replace recorded bytes or label those counterfactuals as an improved baseline.

### Current production differs from the issue's historical premise

`llm/answers.py::_normalize_review_answer` already maps equal ranges to null and sorts severity/confidence before typed validation. `ReviewFinding` enforces ranges and titles, while `ReviewOutput` enforces order; neither enforces the legacy attribution prefix. The independent offline `review/scripts/validate_findings.py` checks attribution, ranges and order on untouched raw JSON. An offline audit through current `validate_review_answer` accepts all 24 baseline responses. Downstream `FindingsPostProcessor._repair_attribution` clears invalid attribution instead of making a paid repair call. Consequently this change must not claim demonstrated current repair-call, latency, or cost savings. The measurable objective is improved strict raw-first format; explicit model attribution also avoids losing valid rule attribution downstream. Production repair statistics, if ever measured, are a separate metric.

## Architecture decisions

1. Prompt-only change for legacy reviewer: v2 remains byte-identical, including its existing golden fixture and checksum pins. Existing range/order normalization stays as it is. Do not synthesize a paraphrase of a rule in backend normalization; it can invent provenance and would not improve raw model adherence.
2. V3 makes construction concrete: first decide whether a shown applicable custom rule is the source; if so copy its exact name and construct the complete prefix plus one-clause check paraphrase before the finding; otherwise set `rule_name: null` and use an ordinary body. Preserve scope matching and standard/custom boundary. Add worked custom-rule and ordinary findings instead of another isolated reminder.
3. Default to `start_line: null` for a single-line finding; use a non-null start only for a genuine multi-line span with `1 <= start_line < line`. Explicitly show equal-line invalid versus null valid, retain a valid multi-line example, and cover both equal and reversed bad ranges in tests.
4. Construct final findings in severity order `critical, high, medium, low, info`, then confidence descending, retaining stable order for exact ties. Include a mixed-severity/same-severity example and a final silent construction checklist. No extra prose or output keys.
5. Golden output examples must pass the independent strict raw validator AND the production gateway parse path; production parse alone hides equality/order mistakes and does not check attribution. Include no-findings, standard single-line, custom single-line, valid multi-line, and mixed severity/confidence coverage. Keep expectations literal, independent of runtime sorting.
6. Eval must actually select v3. Update `DEFAULT_SYSTEM_PROMPT` in `app/bootstrap/llm_gateway.py`; `eval_live.py` already uses that default, so no new workflow inputs are needed. Test the selected default explicitly. Change only focused tests that assert the current default; historical v1/v2 fixtures remain historical.
7. New prompt discovery automatically activates the newest version during deploy seeding. Update asset list/count/active-version expectations (5 → 6 assets, active system v2 → v3). Existing snapshot defaults and `PINNED_PROMPT_CHECKSUMS` intentionally remain v2 until #130 wires a coherent snapshot contract; don't repoint them merely to silence tests. Verify already pinned v2 runs still load their immutable content and new legacy runs use the new active version. If activation proves incompatible, stop to fix that integration before seeding or capture.
8. Raw-first validity remains **before** all normalization, repair, fallback, lint filtering, anchoring and verifier; all 24 cases remain the denominator. Continue to score `review/scripts/validate_findings.py` on exact first provider bytes. Additive diagnostic normalized-acceptance observations must be named separately and must not replace the baseline.

## Dependencies, file ownership and capture freeze

#177 part 2 merged via #184 (`0b5f76a`), and #133 merged (`3ff0fb6a`); these ordering prerequisites are satisfied. Current pins are 46; baseline manifest still records 49. Added `commit_prefix_limits.py` and `llm/memory.py`; removed `github_vcs.py`, `github_run_source.py`, `vcs_failures.py`, `vcs_diff.py`, and `review/rules/schema.json`.

#130 remains open and its latest scope includes a snapshot/evidence_refs reviewer contract **without** the old prefix, v2 gateway validation, verifier adapter and post-fit manifest. #217 is open, has no implementation comments in the retrieved issue, and depends on #130's real verifier adapter/profile. Current gateway eval uses the legacy schema and does not execute the verifier. Therefore this plan does not add inactive verifier files to pins or claim verifier quality. Before capture re-check this condition. If eval has switched, include `review/prompts/review.verifier.v1.md`, `review/schemas/review-output.schema.v2.json`, `review/schemas/verifier-output.schema.v1.json`, `app/modules/reviews/application/reflection_pass.py`, plus other active shaping inputs identified by that integration, before the single paid run. Such a switch changes scope and version allocation; revise the plan before paying.

Coordinator inspected currently open PR file lists:

| Open PR | Ownership relevant here | Consequence |
| --- | --- | --- |
| #230 reflection guard/budget | `prompt_builder.py`, `prompt_budget.py`, `review_output.py`, `tests/test_prompt_seeding.py` | authorized seed-test coordination comment posted; freeze pinned modules before capture |
| #229 DiffMap | `review_output.py` and context integration | no edits planned; pinned-file changes must land before capture |
| #228 RAG triggers | `docs/SYSTEM_DESIGN.md`, independent RAG work | authorized SD §15 coordination comment posted; inspect diff/pins again before capture |
| #227 overview budget/docs | `gateway.py`, `review_output.py`, `tests/test_llm_gateway.py`, `docs/TEST_PLAN.md` | authorized TEST_PLAN coordination comment posted; no gateway/runtime changes planned |
| #220 GitLab intake | `docs/SYSTEM_DESIGN.md`, integration work | authorized SD §15 coordination comment posted; inspect final file list/pins before capture |

On 2026-10-20 the user explicitly authorized four coordination comments, posted
before owned-file edits: [#230 seed tests](https://github.com/larchanka-training/dmc-268-api-t6/pull/230#issuecomment-6291804806),
[#227 TEST_PLAN](https://github.com/larchanka-training/dmc-268-api-t6/pull/227#issuecomment-6291805386),
[#228 SD §15](https://github.com/larchanka-training/dmc-268-api-t6/pull/228#issuecomment-6291806047),
and [#220 SD §15](https://github.com/larchanka-training/dmc-268-api-t6/pull/220#issuecomment-6291806510).
These clear the documented ownership requirement for those exact edits; they do
not authorize further external comments, paid calls or pinned-input drift. Read file lists again before every edit and immediately before capture; do not assume this snapshot remains current. #168 changes and #206 warning behavior are additional freeze checks from the issue. No intentionally incompatible pinned change may land between capture and post-merge replay without an explicit tech-lead ordering decision.

## Ordered implementation tasks

Detailed executable checklists, file boundaries and verification are in `178-todo.md`. Each completed implementation slice must leave a green, reviewable state: perform focused RED/GREEN checks, then all four required gates (`uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy .`, `uv run pytest`) before marking the slice complete or sending it to review. Temporary RED states are internal to a slice; no knowingly broken slice is handed off.

1. Preserve the offline audit in corpus README and SD §15; clarify current production behavior and raw-first definition (two files).
2. With the #230 seed-test coordination comment now posted, add failing v3 contract/example tests and implement immutable v3, its golden fixture, and exact seeder asset/count/active-version expectation updates together (four files). Run focused tests and all four gates before this slice completes; adding the asset without updating seed expectations is not a valid intermediate handoff.
3. Switch the eval/default bootstrap prompt and update focused default assertions (two to three files), then checkpoint mocked eval provenance selects v3.
4. Add extra v3 activation/idempotence and legacy-v2-pin regression coverage (one to two test files; reuse seeder); basic seeder expectations are already green from Task 2. Run PostgreSQL integration when configured, report skips honestly.
5. Freeze merged inputs; document current pins, version order and expected costs; run all four gates and independent review. Then delegate branch creation plus candidate commit/push to the publisher using the invoked publishing workflow. Verify the remote candidate commit contains v3, its golden/seed changes and the new default; pin that remote source SHA before presenting the paid-run request. Actions cannot capture uncommitted local files. Default to no early PR: final PR publication follows the artifact import; if an early draft is needed, create it explicitly as draft and update that same PR later.
6. After separate explicit tech-lead authorization for the verified pushed candidate SHA, dispatch exactly one full 24-case `eval-live.yml` on that frozen remote branch/SHA with `model=mistral-small-4`, `export_raw_responses=true`. No subset or retry without fresh authorization. Audit and import one complete artifact byte-for-byte, then publish measured comparisons (capture import and docs are separate small slices).
7. After artifact/docs import and final checks/review, delegate the final commit/push and PR creation to the publisher (or update the same early draft if one was explicitly created). No automatic merge is authorized. After merge verify the **required CI replay on main**, all 24 cases and `warnings=[]`. Retain exact run/job evidence in PR/issue handoff. Local or PR replay cannot substitute for main's required job. The tech lead closes the issue.

## Verification and acceptance map

| Issue acceptance criterion | Tasks / evidence |
| --- | --- |
| AC1: analyze 17 cases and rules in PR | Task 1 audit matrix above, README/SD evidence; include matrix or direct file link in PR body |
| AC2: new version + golden, immutable v2; normalized metric defined if used | Tasks 2–4, golden byte equality and v2 hash `9177…1d75`, strict example tests; no added normalization; Task 1 defines pre-normalization raw metric explicitly |
| AC3: complete 24 response bytes + manifest, required main replay no drift, metrics vs 7/24, 2/2/17, 2/5, 5/24 | Tasks 5–7: authorization record, exact capture source/run/attempt, byte/hash audit against safe artifact, 24-entry manifest, publishability check, double deterministic replay, live/offline comparison, final main required CI link with `warnings=[]` |
| AC4: capture after #177 part 2 | Task 5 verifies #184 ancestor before dispatch; Task 6 records source SHA and current pin set (expected 46 for unchanged legacy path) |
| DoD: ruff lint/format, mypy, pytest | Every completed implementation slice and final changed-tree review checkpoint; required integration skips explicitly reported |
| DoD: approve current head, resolved threads, up-to-date branch, Refs-only PR and board handoff | Task 7; publisher uses What/Why/How to verify/Refs; no closing keywords or Development link; tech lead closes |

Quality is measured, not pre-guaranteed: there is no numeric improvement threshold in #178. Report a regression honestly and seek tech-lead disposition if the single capture worsens; never cherry-pick answers or pay for another run under the original authorization. No new dependencies are proposed.

## Risks and mitigations

| Risk | Impact | Mitigation |
| --- | --- | --- |
| Better formatting does not improve semantic quality | High | keep truths/corpus/model fixed; report validity, TP/FP/FN, critical recall and verdict separately; no causal claim |
| Prompt content/version conflicts with #130 | High | check assets/branch ownership before v3 addition and pre-capture; recommend legacy v3 first, snapshot successor later; stop on collision |
| Open PR changes pinned input after capture | High | merge/order affected work first, rebase, freeze exact source; required replay must be clean on main |
| Seeder silently activates highest version | High | add activation regression and immutable v2 loading test; keep snapshot defaults v2 pending #130 |
| Production/benchmark validators measure different layers | Medium | document 24/24 gateway-accepted audit and 7/24 strict raw validity; test examples through both paths |
| Capture fails or artifact is incomplete/nonpublishable | High | do not import mixed/partial baseline; retain evidence; a further paid attempt requires fresh authorization |
| Database not configured | Medium | run available unit gates; use TEST_DATABASE_URL for actual seed integration before activation and report skipped integration explicitly |

## Decisions for the coordinator/user

The user approved the recommended narrow legacy-v3 implementation plan with "ок" and later explicitly authorized the four coordination comments linked above. Stable-pin/version-order checks remain required before capture. No further external comments are authorized. Later, once implementation and free checks are complete, request the tech lead's explicit authorization for **one** full paid 24-case run with a stated window/budget. Neither planning approval nor implementation approval authorizes spending.

## Capture/import evidence update — 2026-10-20

The candidate was published and remotely verified at
`47f9add7c4df2b7c32db0091d39296cdd2e4810c` on `feat/178-first-answer-format`.
User chat authorization "сделай 1 прогон" followed the explicit USD 12 nominal /
24-case / primary `mistral-small-4` + fallback `mistral-small-3.2-24b` / raw-export
proposal. Exactly one workflow ran:
[39443263258, attempt 1](https://github.com/larchanka-training/dmc-268-api-t6/actions/runs/39443263258).
Source contains #184 and #133; main was `2326273e6ab7fcea89d1cf3b94b88a4be66ca5e9`.
The user chose capture now despite pending pinned changes in #230/#229/#227;
subsequent drift requires a separate decision and does not authorize another run.
The legacy gateway path remains selected, with no verifier execution/pins.

The complete publishable 25-file artifact passed raw/safe hash/size, 24-ID,
source/prompt/46-pin/static/corpus/settings audits and was imported byte-for-byte.
Double offline replay is byte-identical with `warnings=[]`; after removing only
validator diagnostics it is byte-identical to the live safe report. Dataset final
validation passes 24/24. Strict validity is 10/24, TP/FP/FN 3/5/16, critical recall
2/5 and verdict agreement 4/24; precision is 37.5%, recall 15.8%. Mixed outcomes
and the full 14-invalid-case diagnostics are recorded in README and TEST_PLAN.
No response selection, source retuning or second capture occurred. Historical
15 October bytes remain at the pre-import candidate revision. Actual cost and
production repair savings are not measured.

Final import/docs gates pass: Ruff lint and format (520 files), mypy (517 source
files), pytest 4038 passed / 866 skipped / 2 dependency warnings in 309.26s.
Focused eval tests pass 274/274, zero skips. Fresh independent review reports
Standards 0 / Spec 0; exact evidence and integration skip disclosure are in todo.
Task 7 publication and required post-merge main replay remain outstanding.
Local/PR replay does not complete AC3; the tech lead retains acceptance/closure.
