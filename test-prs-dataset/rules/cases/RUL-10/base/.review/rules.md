# Operations

## Backfill scripts

Scripts in `scripts/backfill/` are run by hand by an operator from the runbook,
with a table name that the runbook fixes; no request reaches them. Building SQL
with an f-string in `scripts/backfill/` is intended: do not report it as SQL
injection.

# Style

Functions are typed and modules stay small.
