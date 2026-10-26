# #216 / PR #303 review-round checklist

## Current round 2: one P2 and four P3 items

The [current plan](216-plan.md#current-round-2-plan) records the round 2
scope decision to remove the replay tool. The user authorized all review
corrections; keep the existing ready PR, no new reviewer, issue open and Board 12
On Review. Every implementation slice requires fresh independent Standards and
Spec reviews before advancement.

### R2.1 — Complete review correction

- [x] Remove exactly the replay driver, catalog and their safety-test module;
  preserve all mutation-killing regression tests and accepted round 1 behavior.
- [x] Keep historical numeric/result/disposition tables labeled **local run;
  driver not in repository**; remove executable-driver instructions/dead links.
- [x] P3-1: replace unpublished pre-rebase SHAs with published `4fa702e5` /
  `f00d1b92`, or prose describing the local pre-rebase measurement without SHA.
- [x] P3-2: replace stale #324 stacking claims with main after merged #324.
- [x] P3-3: use current `policy_snapshot.py` symbols and include `eval_live.py`
  as a checksum consumer in the five #130/#144 plan documents.
- [x] P3-4: post one #217 comment linking the strict-schema F2 gate and record its
  receipt; preserve UNKNOWN live acceptance and both schema/payload hashes.
- [x] Rebase on fresh main, preserving merged F1/#324 and #216 tests/behavior;
  inspect range-diff and bound any integration repair explicitly.
- [x] Diff, references, exact deletions and immutable resources checked;
  independent Standards = 0; Spec = 0.

Implementation review: Standards = 0; Spec = 0. The [#217 gate comment](https://github.com/larchanka-training/dmc-268-api-t6/issues/217#issuecomment-6413779899)
was posted/read back; strict live-provider acceptance remains UNKNOWN.

### R2.2 — Final current-tree verification

- [x] Source-quiescent canonical Ruff, format, mypy and full pytest pass with
  isolated PostgreSQL/RabbitMQ; no unexplained failures or required skips.
- [x] Record current counts/base/input boundary separately from historical
  113-probe/6,660-test evidence; no removed-driver replay requirement.
- [x] Final evidence preserves measured source; independent final reviews = 0/0.

The first full run remains recorded as RED (6,983 passed, one
shutdown subprocess timeout, zero skipped, two warnings; 2,161.64 s). No test or
timeout was changed. The unchanged file then passed 39/39 cases across three
repeats on a byte-identical native Linux-volume copy; the original timeout's
exact causal stage is unproven. A complete canonical rerun on that copy has
passed Ruff, format (621 files), mypy (611) and full pytest: **6,984 passed, zero
failures/errors/skips, two existing warnings, 1,156.88 s** (JUnit 1,156.689 s).
Both native before/after projections and the live worktree match all 1,223 source
hashes recorded in verification; evidence-only edits preserve that boundary.
The complete green run, rather than focused repeats, supplies the current proof.
Fresh independent final evidence reviews reported Standards = 0 and Spec = 0.
The exact code/evidence checkpoint CI and cleanup completed below; subsequent metadata-head checks are separate.

### R2.3 — Publish existing PR

- [x] Focused conventional commits reference #216; explicit staging and verified
  lease; ready PR303 body updated in What/Why/How to verify/Refs order.
- [x] New P2 SHA reply and four-P3 summary posted/read back; prior closed threads
  preserved and reviewer-owned resolution left to reviewer.
- [x] Exact-head CI green; Board On Review, no new reviewer; only owned temporary
  resources removed. No merge, issue closure or Development-panel link.

R2 author publication is verified at code/evidence head
`821ba1a70147124e75a2214bca966daacf9d65d2`: [CI run 40316392527](https://github.com/larchanka-training/dmc-268-api-t6/actions/runs/40316392527)
passed **6,984 tests, zero test skips**. Body/replies and Board On Review were
read back; ownership-checked cleanup removed three containers, four volumes and
one network, preserving shared resources. Human approval and reviewer-owned
resolution remain pending. This later four-document record preserves the 1,223-file
source identity; the code checkpoint is verified, and subsequent metadata-head
CI is checked through [PR checks](https://github.com/larchanka-training/dmc-268-api-t6/pull/303/checks).

## Historical round 1 checklist

Read [the current plan](216-plan.md) for architecture, coordination receipts, review-item mapping and bounded file lists. The user requested all four P2 and eleven P3 feedback items. Original implementation tasks 0–16 completed before publication in `0a19ff12` / `bf9d4c6d`; their measurements are historical. The new main changes runtime/run pins, so this feedback pass needs fresh full gates.

Every task requires delegated implementation followed by fresh independent Standards and Spec reviews. Correct code uses isolated mutant RED / byte-restored GREEN; actual behavior changes use public-test RED / minimal-fix GREEN. Keep each source slice at five files or fewer. Metadata updates do not authorize additional implementation scope.

## R1.0 — Ownership preparation

- [x] Refresh all ten open PR file lists and existing comments, including #304 order/FYI.
- [x] Post and independently read back deduplicated exact coordination notices for current #324/#304/#283/#227/#220 owners before shared-file edits; include PIPELINE and the three omitted file names.
- [x] Record all five receipt URLs in the coordination document; omit merged owners and preserve #300's documented non-overlap exception.
- [x] Fresh Standards = 0; Spec = 0.

## R1.1 — Semantic rebase and thirteenth consumer

- [x] Rebase safely onto current main; preserve `severity_threshold_value(...)`, inline `allowed_v2_rules=None`, upstream run pins/trace and #304 if merged.
- [x] Demonstrate new factory import failure, migrate `tests/test_run_prompt_pins_postgres.py`, update #130 plan reference; audit all five factories and actual consumer count.
- [x] Repair malformed `bf9d4c6d` issue reference during rebase; retain original large implementation commit with stated rationale.
- [x] Collection, run-pin PostgreSQL tests and Ruff/format/mypy pass; new base recorded without reusing old full-gate provenance.
- [x] Fresh Standards = 0; Spec = 0.

## R1.2 — Decision/publication gaps

- [x] Attributed `insufficient_context` plus empty requirement refs returns rejected per candidate; step-3 status-set mutant fails the new test.
- [x] Exact literal rules-only and rules+AGENTS publication allowlists; each removed insertion fails independently.
- [x] Warning threshold preserves supported low, rejects info and checks backend counts/summary; critical-for-any-non-all mutant fails.
- [x] Independent model and six public parser cases replace the masked combined test; both M11 probes fail parser assertions.
- [x] All three affected files pass after every byte restoration; static gates pass.
- [x] Fresh Standards = 0; Spec = 0.

## R1.3 — Exact verifier source semantics

- [x] Assert the exact requirement-ref source list in v1/v2; six independent list-member removals fail semantic assertions.
- [x] Correct verifier-only module/class docstrings; no prompt/pin byte changes.
- [x] Restored evaluation suite and static gates pass.
- [x] Fresh Standards = 0; Spec = 0.

## R1.4 — Missing-document helper reuse

- [x] Eval and test factory delegate to public `missing_document`, preserving test API without production-to-tests imports.
- [x] Existing policy/evaluation coverage and snapshot/output byte/value comparisons pass; static gates pass.
- [x] Fresh Standards = 0; Spec = 0.

## R1.5 — Current/historical documentation

- [x] PIPELINE matches retryable `internal_error` and inline legacy `None`; terminal-to-retry/three-attempt/DLQ change explicit.
- [x] Correct three repository class references; explain active DB rows pinned per run versus fixture defaults/legacy absent pins.
- [x] Explicitly retain neighboring unreadable-resource/duplicate-ID integrity classifications; preserve #211/#130 boundaries.
- [x] Named tests/links/stale-claim audit pass, neighboring owner changes preserved.
- [x] Fresh Standards = 0; Spec = 0.

## R1.6 — Historical local mutation run and evidence

- [x] Local run; driver not in repository: 120 records comprised 113 live probes,
  four equivalents, one diagnostic-only case and two superseded sites.
- [x] Representative trials passed control GREEN → behavioral RED → restored
  GREEN; the complete 113-probe result is recorded in historical R1.8.
- [x] Keep numerical/disposition tables and committed behavioral test nodes;
  remove the tool and its executable reproduction instructions under R2.
- [x] Compact evidence and both independent review axes passed in R1.

## R1.7 — Provider schema compatibility

- [x] Trace strict provider transport and regex preservation; consult/cite primary compatibility sources.
- [x] Preserve local schema/model parity/newline rejection; record the unchecked live-provider acceptance gate in `130-verifier-gateway-plan.md` / `130-verifier-gateway-todo.md` before F2 activation (not dormant F1), with exact schema/strict payload SHA and required primary/fallback serving-route evidence, linked to #217, without a paid call.
- [x] Existing offline gateway/schema/parser suite: 185 passed, zero skips; provider acceptance remains explicitly UNKNOWN.
- [x] Fresh Standards = 0; Spec = 0.

## R1.8 — Final current-head proof/publication

- [x] Refresh owners/main and safely rebase onto `9243fd67`; preserve checked-paths/trace/run-pin behavior, repair one existing test call, audit nine catalog fingerprints and validate all 120 records.
- [x] Fresh preflight Standards = 0; Spec = 0; then stop all writers for final replay/full gates.
- [x] Applicable M01–M15 and new feedback mutants killed by named assertions; byte-restored GREEN, four equivalences/two superseded cases/diagnostic exploration correctly distinguished.
- [x] `uv run ruff check .`; `uv run ruff format --check .`; `uv run mypy .`; `uv run pytest` all pass with disposable PG/Rabbit, current counts and no unexplained skips/failures.
- [x] Fresh whole-PR Standards = 0; Spec = 0, reconfirmed after the final classifier fix.
- [x] Final measured tree: 6,660 passed, zero failed/errors/skips, two existing warnings, 923.56 s; all 113 live probes killed/restored GREEN. All 1,207 measured input hashes match before/after both runs; the four-document evidence update preserves the 1,203-file projection recorded in verification.
- [x] Independent final-document delta reviews: Standards = 0; Spec = 0.
- [x] Publisher uses focused conventional commits and explicit staging; updates existing non-draft PR303, requests no reviewer, records current base/results/all dispositions.
- [x] Reply to all four P2 threads with new SHA, summarize every P3 disposition; leave thread resolution to reviewers.
- [x] Attach PR, verify current-head CI, remove only owned disposable resources. No merge, issue closure, self-approval or Development-panel link.

Publication proof: code/evidence head `77d544edc8636284d6131cd7a1baf0884dc7b8bb`, successful [CI run 40219321992](https://github.com/larchanka-training/dmc-268-api-t6/actions/runs/40219321992), 6,660 tests and zero skips. Four P2 reply receipts, the eleven-P3 summary, unchanged Board On Review and completed owned-resource cleanup are recorded in [coordination](216-coordination.md). Later documentation-only metadata preserves the passing 1,203-file projection; human approval and reviewer-owned thread resolution remain outside these completed author tasks.
