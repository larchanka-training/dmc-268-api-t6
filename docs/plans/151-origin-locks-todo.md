# Origin-lock refactor todo (#151 P3-5)

- [x] Read approved combined plan, detailed brief, AGENTS and backend/git rules.
- [x] Verify independent main base and open-PR ownership.
- [x] F1: record zero-skip baseline, extract typed lock helper and migrate eight sites.
  Before source: 140 passed, zero skipped, 180.94s. Restored-source static gates pass.
  Run-first mutation fails all five NOWAIT authority-lock regressions as expected.
- [x] F2: record zero-skip eight-file acceptance and four required gates.
  Acceptance: 256 passed, zero skipped, 372.57s.
  Full gate: 3425 passed, zero skipped, 753.16s; all three static gates pass.
- [x] Fresh independent Standards/Spec review (coordinator): zero findings in both axes
  against exact base c1d8f59 and the stable four-file working diff.
- [x] External first-PR public composition test passes against this app with import provenance checked.
- [x] Publish independent PR (publisher): [#153](https://github.com/larchanka-training/dmc-268-api-t6/pull/153), base main.
  Source/proof commit `84641aa`. On 2026-10-14, PR #153 was Ready and all applicable
  CI checks were green on historical head `205f1a6ab8ed405e0fe56df7bf63cb7be40519b9`.
  This status does not establish follow-up-head CI. Human approval on the current
  head, merge and issue acceptance remain external.

## PR #153 review follow-up (2026-10-14)

- [x] Plan the four authorized P3 review follow-ups in the existing documents.
- [x] R1: document helper preconditions (same `Run.id`, no existing lock clause)
  and replacement with `FOR UPDATE OF runs`; keep SQL/call sites unchanged.
- [x] R2: retain original proof and add clearly attributed reviewer evidence:
  Repository-before-installation mutation caused claim/diff `40P01` deadlocks
  and two watcher timeouts. Optional developer mutation rerun omitted; no source
  mutation was made in this round.
- [x] R2: record scoped #106 deferral for optional finish/requeue/publication/
  context concurrency cases, with exact gaps tracked here rather than an edit to
  #106. Correct verification.md's stale Draft/future
  Ready tracking using the exact historical head above.
- [x] Checkpoint: docstring-only production diff, preserved proof provenance,
  restored source, consistent plan/todo/verification tracking.
- [x] R3: Ruff lint/format check and mypy pass; full `uv run pytest` with dedicated
  PostgreSQL 17/RabbitMQ: 3425 passed, zero skips, 2 existing warnings, 728.41s.
- [x] R3: fresh independent Standards/Spec review has no unresolved findings.
  Both axes report zero findings on 2026-10-14 against base `c1d8f59`, including
  the original PR diff and stable docstring/documentation correction.
- [x] R4: publisher updates existing PR #153, observes final-head CI, and posts
  one PR comment replying to all four P3 remarks; no threads exist to resolve.
- [x] Checkpoint: every remark addressed, final-head status stated accurately;
  human approval on the current head, merge and issue acceptance remain external.
  The actual publication SHA and observed CI state are recorded in the PR body
  and author reply, avoiding a self-referential publication-evidence commit.
