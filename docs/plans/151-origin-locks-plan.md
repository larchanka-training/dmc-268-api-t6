# Independent origin-lock refactor (#151 P3-5)

## Authorization and baseline

The user approved the combined #151 plan on 2026-10-13 under agent-loop.
This separate branch implements P3-5 only, from refreshed origin/main
`c1d8f59d9ff3f332a3595857dbe612115ad559e8` on `chore/151-origin-locks`.
PR #152 implements the other findings independently; no files or commits from
that branch are copied or used as this branch's base.

F1 and F2 are implemented and verified: 256 focused and 3425 full tests pass
with zero skips; static gates are green. Fresh Standards/Spec review has zero
findings. Published independently as PR #153:
https://github.com/larchanka-training/dmc-268-api-t6/pull/153.
CI/Ready status is tracked by the coordinator; no reviewers are assigned.

## Design and contracts

Extract `lock_run_origin_row` in reviews infrastructure. Accept the caller's
typed SQLAlchemy Select and return its nullable typed Row. Acquire exactly three
queries: immutable origin installation FOR SHARE, Repository FOR SHARE, then the
caller select FOR UPDATE OF runs. Missing prefix rows do not independently fail.
The helper does not own transactions, flush, commit, check authority, or call a
provider. Callers keep their joins, predicates, refresh options and guards.

Eight sites: policy guard (two public writes), diff snapshot writer, lifecycle
claim/finish/requeue/complete_publication/retire_stale_origin, and trace context
save. Ordinary lifecycle selects keep the current-installation inner join; policy
and diff keep the immutable-origin outer join. Keep claim's workspace budget
column, stale/worker/state/revision predicates, nested finish lock reacquisition,
exception codes and payload immutability. Type the existing `_joined` select
without changing its provider variant. Four production files only; no dependency,
migration, public contract, worker wiring or network/DB boundary change.

## Work and validation

F1: establish the exact three-file PostgreSQL baseline with zero skips, then
extract and migrate all eight sites. Existing public store/use-case PostgreSQL
regressions are a before/after safety net for correct behavior; a naturally red
new test is not claimed for this behavior-preserving refactor. A temporary Run-
first mutation may prove existing NOWAIT regressions detect the unsafe order;
restore it before acceptance. No helper-mirroring tests are planned.

F2: run eight-file real PostgreSQL acceptance and all four required gates. Use
disposable PostgreSQL 17 schemas and RabbitMQ, with one broker suite at a time.
Optionally execute PR #152's composition regression externally against this app
without copying it into the branch. Record separate heads and import provenance.
Fresh Standards/Spec review and publication belong to the coordinator/publisher.

## Ownership and publication

Read-only open-PR file check on 2026-10-13 found no overlap with #152, #148, #133
or #124 for the four production files and these separate plan documents. No
coordination comment is needed. Commit/push/PR creation are publisher tasks.
Refs #151; no closing keyword or Development link. No merge in this task.
