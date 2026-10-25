# #216 / PR #303 review-round checklist

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

## R1.6a — Portable mutation driver/catalog

- [x] Prepare the safe driver and data catalog as a separate three-path slice (driver, catalog and driver safety tests), with all 103 available current mappings plus new feedback probes; no private artifact dependencies.
- [x] Guard stale fingerprints and changed snapshot targets, exact replacement count, meaningful assertion kills and byte restoration in `finally`; report setup/collection failures separately.
- [x] Dry-run catalog and real representative source/schema/prompt/new-feedback probes pass GREEN/RED/restored GREEN; any historical reproduction limits are explicit.
- [x] Fresh Standards = 0; Spec = 0.

## R1.6b — Compact portable evidence documents

- [x] All four #216 documents contain durable summaries, numerical results, source SHAs and reproducible instructions linked to the committed driver/catalog.
- [x] No personal paths, inaccessible artifacts, loopback URLs, machine-specific existing resources or quoted chat replies remain.
- [x] Keep M01–M15 current killers/dispositions and explicitly correct the missing step-3 survivor claim; actual catalog coverage and historical limits are accurate.
- [x] Privacy/portability grep, links and reproduction instructions pass.
- [x] Fresh Standards = 0; Spec = 0.

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
- [ ] Independent final-document delta reviews: Standards = 0; Spec = 0.
- [ ] Publisher uses focused conventional commits and explicit staging; updates existing non-draft PR303, requests no reviewer, records current base/results/all dispositions.
- [ ] Reply to all four P2 threads with new SHA, summarize every P3 disposition; leave thread resolution to reviewers.
- [ ] Attach PR, verify current-head CI, remove only owned disposable resources. No merge, issue closure, self-approval or Development-panel link.
