# PR #132 integration with policy and AST context (#113)

## Base and contracts

Integrate main `c9966270abb093207c4ace43b0fc4b7422921013` (merged #136)
with PR checkpoint `075004e996db907b19787c91322ff8b62e43825e` and the
publication cancellation and installation-specific receipt retry corrections.
Preserve immutable origins, per-request GitHub
checkpoints, typed receipt retries, GitLab publication, pipeline events and rollback
revision admission. Network operations remain outside database transactions.

## Implementation

1. Integrate policy/context behavior with the existing publication cancellation
   and receipt retry coverage, preserving both context and origin safeguards.
2. Prove one Alembic head and an upgrade from deployed main and revision 0028.
   Linearize the unmerged reinstall migrations after main's conventions-policy head;
   retain public reinstall revision IDs and leave merged migrations unchanged.
3. Run focused migration, publication, origin and worker regressions before minimal
   corrections. Apply immutable-origin safeguards to the new context/network
   seams and verify them through focused behavior regressions. Clarify full migration
   revision IDs in current rollout documentation.
4. Record actual current-base proof; preserve historical counts as historical.

## Completion conditions

Focused network/context and policy-persistence regressions pass, together with
Ruff, format (433 files) and mypy (430 source files). The cache-miss checkpoint
correction passed its 4-unit and 24-PostgreSQL rechecks. Fresh Standards and Spec
reviews report zero actionable findings. Full pytest passed 3425 tests including
613 integration cases, with zero failures/skips; the [final proof](113-review-round8-final.md)
records the exact local results. Exact-head publication and required CI
are recorded in the PR body and checks separately.
Latest-main verification, current-head human approval and writer shutdown before
merge remain merge prerequisites. Main's inherited dependency changes require
explicit identification in the PR body.
