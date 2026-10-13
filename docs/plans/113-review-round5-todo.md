# PR #132 round-five checklist (#113)

- [x] Confirm the two P3 observations and PR checkpoint `8a87fe9`.
- [x] Verify combined main `11ddfab`: #121/#126 merged (initial checkpoint was `b220dd8`).
- [x] Rebase on combined main `11ddfab`; preserve trigger outcome and installation fence at parent `b4fb4a2`.
- [x] Adapt the receipt regression and verify restart/redelivery, superseded and obsolete-head controls with real PostgreSQL: [333 focused passes](113-review-round5-integration.md), zero skips.
- [x] Correct old-image bootstrap failure, blocked services and fix-forward recovery.
- [x] Describe merged #126 revision admission before application-container changes.
- [x] Limit NULL-origin wording to independent old writers and new-worker cancellation.
- [x] Correct matching round-four plan/proofs and completed publication checkboxes.
- [x] Preserve historical verification and UI #105 / API #106 references.
- [x] Verify runbook against actual scripts, compose and migration behavior.
- [x] Pass diff/private-reference checks and all four repository gates: 3132 passed, zero skips; see [final proof](113-review-round5-final.md).
- [x] Independent Standards and Spec reviews report zero actionable findings on each axis.
- [x] Record portable current-base documentation, integration and final verification evidence.

Publication, review response and required-CI results are recorded in the
[PR #132 body and checks](https://github.com/larchanka-training/dmc-268-api-t6/pull/132)
on the exact published head.

External merge prerequisites remain separate: latest-main verification, combined-head
gates/CI, human approval and agreed writer shutdown/merge timing.
