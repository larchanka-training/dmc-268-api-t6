# PR #132 policy/context integration checklist (#113)

- [x] Preserve existing publication cancellation and receipt retry regression coverage.
- [x] Confirm integration base `c996627`.
- [x] Combine typed merge-base failures with mandatory provider checkpoints.
- [x] Prove a single migration head and deployed-main/0028 upgrades.
- [x] Run inherited regressions; correct the VCS fake and record the three context failures.
- [x] Resolve policy/context failures: 71 unit, 21 PostgreSQL and 3 worker passes; atomic policy persistence: 39 PostgreSQL passes, all with zero skips.
- [x] Clarify origin migration rollout and rollback revision IDs.
- [x] Record focused migration and inherited regression evidence.
- [x] Ruff check, format (433 files) and mypy (430 source files) pass.
- [x] Correct Redis cache-miss checkpoint gap: 4 revision unit and 24 context PostgreSQL passes; active controls retained.
- [x] Fresh independent Standards/Spec reviews: zero actionable findings on each axis.
- [x] Complete corrected-source full pytest: 3425 passed including 613 integration cases, zero failures/skips; see [final proof](113-review-round8-final.md).

Publication, the general review response and required-CI results are recorded in
the [PR #132 body and checks](https://github.com/larchanka-training/dmc-268-api-t6/pull/132)
on the exact published head.

Approval on current head, latest-main verification and writer shutdown coordination
remain external merge prerequisites.
