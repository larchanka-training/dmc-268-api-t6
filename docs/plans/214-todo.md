# #214 — acceptance checklist

Issue: <https://github.com/larchanka-training/dmc-268-api-t6/issues/214>.
PR: <https://github.com/larchanka-training/dmc-268-api-t6/pull/302>.
Current remediation base: `d08e6c9625ac5eb45eae45c229925fa776d82b80`.

## Implementation

- [x] Approved plan; coordinate overlapping worker/operator/architecture files.
- [x] Two independent real-composition tests protect HandleReviewRun recovery and
      successful PublishRunReview; use literal URLs and structural summary checks.
- [x] Linked, unset and empty cases; realistic failed verdict; transaction
      observations asserted from the test outside caught provider exceptions.
- [x] Test-first startup validation, optional settings and accepted bases retained.
- [x] Reject zero ports and nonprintable characters after observing RED.
- [x] Protect parser exception privacy with the credential-bearing NFKC input.
- [x] Canonical SECRETS policy, CICD cross-link, architecture startup note; explain
      both rollback branches, effective variable precedence and CI bundle reset.
- [x] Keep generic credential-safe error and local fakes; no dependency change.
- [x] Remove duplicate PR body and local-session/self-assessment residue from plans.

## Verification

- [x] Rebase onto pinned current main while preserving upstream work.
- [x] Focused settings/composition and relevant regressions: 229 passed.
- [x] CI unmarked strict-marker selection collects all 62 portal tests.
- [x] M1/M2 independently fail on missing details_url; restore byte-exact.
- [x] M12 fails on leaked credential marker; restore byte-exact and GREEN.
- [x] Ruff, format and mypy pass on remediation source.
- [x] All four exact gates on the frozen remediation snapshot exit 0; unexcluded
      pytest with dedicated PostgreSQL/RabbitMQ: 6475 passed, zero skipped,
      two existing deprecation warnings in 807.34 s (13:27).
- [x] Fresh independent agent Standards/Spec checks of the review-round changes
      completed with no findings; this checkpoint is not GitHub human approval.

Evidence: [214-verification.md](214-verification.md). Prior 6437-test full success
is a historical checkpoint, not proof of this changed source. GitHub current-head
approval, reviewer thread resolution, merge and tech-lead issue closure remain
human workflow steps. No live staging or durability claim.
