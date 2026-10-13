# Round-four documentation verification (#113 / PR #132)

Scope: P3-1, P3-2 and P3-4 in the [round-four plan](113-review-round4-plan.md).
Documentation baseline: `e417c2ec5af2f7e5556614c38474743e31ebaa98`, rebased on
main `620c81d927cebf3deb0721e5dacb7b70b721ad7e` with #125 included.

## Portable historical evidence

- `113-review-plan.md` identifies the public PR review timeline and issue instead
  of private downloaded review files.
- `113-review-final.md` retains the tested `9aedfa8` base, reviewed `ed62aad` head,
  2,846-pass execution, 469 integration cases and recorded timings, without local
  gate-log paths. Companion states are explicitly historical.
- `113-review-round3-final.md` retains the 2,886-pass execution, 389-file SHA-256
  fingerprint, unchanged source/runtime trees after the #127 documentation rebase,
  focused counts and gate provenance. Publication sequencing and pending literal
  approval notes were removed. UI wording is tracked in [UI #105](https://github.com/larchanka-training/dmc-268-ui-t6/issues/105),
  with the [API #106 reference](https://github.com/larchanka-training/dmc-268-api-t6/issues/106#issuecomment-6131037958).
- The sweep covers every `docs/plans/113*.md`; existing portable placeholders and
  historical commands, SHAs, counts and durations remain.

## Rollout facts checked against source

`docs/CICD.md` keeps shutdown of every old Run writer before merge and bootstrap,
qualifies new-image startup to a successful rollout, and retains #125's successful
bootstrap/PostgreSQL API dependencies and worker LLM/`PORTAL_URL` env contract.

Stopped API cannot accept `/webhooks/github`. GitHub's primary
[failed-delivery policy](https://docs.github.com/en/webhooks/using-webhooks/handling-failed-webhook-deliveries)
confirms no automatic redelivery. Its
[redelivery procedure](https://docs.github.com/en/webhooks/testing-and-troubleshooting-webhooks/redelivering-webhooks)
confirms App owner/manager access through Edit → Advanced → Recent Deliveries →
GUID → Redeliver, limited to deliveries from the past three days.

Source inspection of `deploy/scripts/deploy.sh`, `deploy/scripts/rollback.sh`,
`deploy/compose/staging.yml` and `alembic/versions` corrects the earlier recovery
claim: after 0030, an image lacking that revision fails bootstrap's
`alembic upgrade head` with unknown revision. API and both workers require
successful bootstrap and cannot start. Pre-#126 rollback could replace containers
and leave staging unavailable. Merged #126 performs read-only revision admission
before application-container or release-config changes; it rejects that old image.
A failed deploy may already have affected services, so recovery requires a
fix-forward image containing 0030. No runtime old-image reproduction is claimed.

0030 leaves origin nullable after backfill. An independent old writer outside
Compose can insert NULL origin; only the new origin-aware worker cancels eligible
nonterminal Runs as `installation_unavailable` during lifecycle processing or
stale reconciliation. Terminal history remains unchanged.
Read migrations 0029/0030: manual downgrade removes origin, then account identity;
0029 conservatively folds installation availability into enabled. Re-upgrade does
not reconstruct lost lineage.

At the historical round-four main `620c81d`, #121 and #126 were open and rollback
had no revision guard. Both are now merged into main
`11ddfabf31a3345e6d21cdf57586281cb62cee7c`; the corrected runbook describes the
actual guard while preserving that historical verification boundary.

## Executed documentation checks

```sh
git diff --check
rg -n '/(tm[p]|User[s]|hom[e]|privat[e])/|localhos[t]:[0-9]+|127[.]0[.]0[.]1:[0-9]+|[[:alpha:]][[:alnum:]+]*://[^ /]*@' docs/plans/113*.md
```

Diff check: exit 0, no output. Private-path/service-address/DSN sweep: exit 1,
no matches. These are documentation checks only. Full combined-head repository gates and
independent review are recorded separately in the final proof.
