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

Read `deploy/scripts/deploy.sh`, `deploy/scripts/rollback.sh`, staging compose and
`.github/workflows/ci-cd.yml`: failed compose startup or external health check can
restore the previous image; image rollback does not run Alembic downgrade.
Migration 0030 permits NULL origin for old writers after backfill. The new worker's
claim/lifecycle and stale-origin reconciliation cancel eligible nonterminal work
as `installation_unavailable`; terminal history is not rewritten. The old image
has no origin guard, so it cannot be credited with that cancellation behavior.
Read migrations 0029/0030: manual downgrade removes origin, then account identity;
0029 conservatively folds installation availability into enabled. Re-upgrade does
not reconstruct lost lineage.

Read-only GitHub checks returned #121 and #126 OPEN with `mergedAt: null`;
#126 head was `c6b7b2bc6b46119490e48d2cbc09b1b6095d1634` and #121 head was
`03f539b7fd4f0e4353104db894d6f938ea071467`. Main `620c81d` has no rollback revision
guard. CICD describes current behavior separately from #126 and requires checking
actual combined behavior after its merge, without bypassing a compatibility guard.

## Executed documentation checks

```sh
git diff --check
rg -n '/(tm[p]|User[s]|hom[e]|privat[e])/|localhos[t]:[0-9]+|127[.]0[.]0[.]1:[0-9]+|[[:alpha:]][[:alnum:]+]*://[^ /]*@' docs/plans/113*.md
```

Diff check: exit 0, no output. Private-path/service-address/DSN sweep: exit 1,
no matches. Only assigned prose and B checklist entries were changed; no application,
test, migration, dependency or deployment implementation edits were made here.
Repository gates and independent review remain the coordinator's combined-head
verification; this document claims documentation checks only.
