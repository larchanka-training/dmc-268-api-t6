# Final verification after policy/context integration — PR #132 / #113

## Source and verification boundary

Integration base: `c9966270abb093207c4ace43b0fc4b7422921013` (merged #136).
Rebased checkpoint: `e4c343e2298c01cb16aec9781183b11ba45b8f57`, plus the preserved
four-P3 test/documentation delta and subsequent integration corrections.
The corrected complete source is frozen. A cache-miss checkpoint now runs
immediately before token acquisition after the Redis await. Fresh static gates
and both independent reviews pass. The full corrected-source gate passed as
recorded below; no earlier result substitutes for it.

The [historical unpublished round-seven proof](113-review-round7-final.md) records
3196 passes on main `2251b59` plus the then-unpublished P3 delta on `075004e`.
Its GitLab-origin and waiting-receipt tests are preserved; those historical counts
do not verify #136's new policy/context source.

## Migration and inherited integration evidence

The [migration/integration proof](113-review-round8-integration.md) records an
initial two-head ambiguity: five failures in 1.58s. The corrected graph and migration
selection passed 19 tests in 12.89s with zero skips, including upgrades from deployed
main and revision 0028, immutable-origin backfill and data-bearing round trips.

The preserved single chain is:
`20261007_0028` → `20261011_0029` → `20261012_0030` →
`20261010_0029` → `20261010_0030`.
The unmerged reinstall identity migration follows merged main's policy-key migration;
merged migrations and public reinstall revision IDs remain unchanged.

The inherited unit selection initially produced six failures and 130 passes in
1.48s because a VCS fake omitted the mandatory checkpoint. The corrected selection
passed 136 tests in 1.03s. These focused results are separate from full repository
gates on the complete integrated source.

## Context and policy verification

The initial focused results are distinct from the corrected-source cache-miss
rechecks and complete repository gates.

The [context checkpoint proof](113-review-round8-context.md) records Run-bound
policy/revision/tree/blob/PR/merge-base request checks, cancellation/deadline
propagation and optional-fallback controls: 71 focused unit passes, 21 PostgreSQL
passes with zero skips in 47.00s, and the three inherited worker/broker regressions
passing with zero skips in 15.21s (13 deselected). Independent NOWAIT lock probes
at provider boundaries verify that network I/O occurs outside DB transactions.

Additional cache-miss regressions first produced two unit failures and two
PostgreSQL failures with one active control passing. After the checkpoint fix,
the revision-source suite passed 4 tests and the context-origin PostgreSQL suite
passed 24 tests with zero skips in 29.13s. Complete/incomplete cache controls and
literal zero token/HTTP calls after cancellation or deadline are retained.

The [policy persistence proof](113-review-round8-policy.md) records atomic
immutable-origin/running-state guards for documents and snapshots: 39 PostgreSQL
passes with zero skips and 2 existing warnings in 24.48s. Real lock contention,
identity-map refresh, rollback and preserved raw bytes/snapshot diagnostics are
covered. Historic reads, append-only actions and paid usage remain intact.

Inherited #136 dependencies are markdown-it-py, redis, tree-sitter and its Go,
JavaScript, Python and TypeScript parsers. This PR adds no dependency of its own.
Provider calls remain outside database transaction boundaries.

## Full repository gates and review

- `uv run ruff check .`: All checks passed.
- `uv run ruff format --check .`: 433 files already formatted.
- `uv run mypy .`: Success: no issues found in 430 source files.
- `uv run pytest -rs` with PostgreSQL/RabbitMQ configured: 3425 passed,
  2 existing dependency warnings in 714.52s (0:11:54), zero failures/skips.

Integration collection selected 613 / 3425 tests, with 2812 deselected in 1.35s.
All 613 integration cases ran in the full suite above; there was no separate local
integration execution.
The interrupted preliminary run is excluded from passing-gate evidence.

Fresh Standards and Spec reviews of the actual combined-source delta reported
zero actionable findings on each axis, including the cache-miss correction.

The corrected tested snapshot contains 454 Python/runtime/configuration files,
including the two new PostgreSQL modules. All SHA-256 fingerprints match the
frozen source. Canonical sorted JSON SHA-256: `5790c830006be32f306da99682eda04885e0059331e1063730957ac38a51e6cc`.

## Publication and merge prerequisites

`git diff --check` and configured staged-file checks passed: trailing whitespace,
end of file, merge conflicts, Ruff check, Ruff format and mypy. YAML/TOML checks
had no applicable staged files. No hook changed a file; all 454 tested fingerprints
matched after the checks.

Publication, the general response to the four P3 findings and required-CI results
are recorded in the [PR #132 body and checks](https://github.com/larchanka-training/dmc-268-api-t6/pull/132)
on the exact published head, separately from local verification.

Squash only: intermediate commits have failing checks. Recheck main before merge,
obtain current-head approval at the agreed time with @axyi and confirm reviewer-owned
thread resolution. Stop every old Run writer before merge/main auto-deploy and keep
writers stopped through migration/bootstrap. If main advances, rebase and rerun
gates on the new exact head. Origin/account/policy downgrade losses and #126's
revision admission/fix-forward recovery remain documented in CICD.
