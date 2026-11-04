# Stored credential key rotation

This runbook covers Bitbucket connection tokens, Bitbucket repository webhook
secrets, and GitLab connection tokens. GitLab webhook secrets are stored as
digests and have no encryption key to migrate. Run the operator from a trusted
host with protected `DATABASE_URL`, `BITBUCKET_CREDENTIAL_KEYS`,
`BITBUCKET_ACTIVE_KEY_ID`, `GITLAB_CREDENTIAL_KEYS`, and
`GITLAB_ACTIVE_KEY_ID` in its environment. The keyring variables contain JSON
objects of key ID to canonical base64 encoded 32-byte keys. Never put key
material or database credentials in command arguments, shell history, or logs.
Revoked GitLab connections can retain encrypted tokens; the operator migrates
those rows too, preserving their revoked state, and counts them before key
retirement.

## Forward order

1. Take and verify a restorable PostgreSQL backup before changing either
   keyring. Keep the backup encrypted and protected under the existing backup
   procedure. Record its recovery point and retain the old keyrings securely.
2. Add each new key ID to the corresponding keyring **alongside every old key**.
   Deploy that expanded keyring to every API, review worker, webhook worker,
   and trusted operator process that reads the provider's encrypted rows.
   Leave the old active IDs selected until the expanded keyrings are live.
3. Switch each provider's active key ID to its new ID and deploy consistently.
   Confirm new writes use the new IDs. Keep old keys available for reads.
4. Run `uv run python -m app.credential_key_rotation dry-run`. Its JSON output
   contains row counts by key ID and table only. Then run
   `uv run python -m app.credential_key_rotation run --batch-size 100 --max-batches 10`.
   Each pass selects at most 100 rows from each table, commits each successful
   compare-and-swap separately, and reports updated counts. Repeat `run` until
   its `ready` value is true. Restarting after interruption is safe: rows
   already using the active key are skipped. A concurrent rotation or
   revocation wins over a stale operator update.
5. Quiesce credential writes for the final retirement check. Run
   `uv run python -m app.credential_key_rotation verify` and require exit code
   zero. Confirm its count map contains **zero rows under every old key ID**
   in all three tables. Investigate a nonzero count or operator error while
   retaining both keys; never remove an old key on a partial result.
6. Remove an old key only after that check, while writes remain quiesced, and
   deploy the reduced keyring to all readers and workers. Resume writes after
   the rollout. Retain the protected backup according to backup policy.

`--batch-size` accepts 1–500 rows per table and `--max-batches` accepts
1–10000 passes per invocation. Use smaller batches if operational load warrants
it. The command selects bounded ciphertext only in `run`; `dry-run` and
`verify` select key IDs and counts, never plaintext or ciphertext. The CLI
prints a generic error code on failure to avoid exposing secrets through
database or cryptography exceptions.

## Rollback order

Before old-key removal, keep both keyrings deployed. If the new deployment or
migration fails, set the previous active key IDs again, deploy to all readers
and writers, then rerun the operator with the previous IDs active. Verify zero
rows under the abandoned new IDs before removing those new keys. Existing rows
remain readable throughout because both keys are retained. If the database
must be restored from the pre-change backup, stop writers, restore the matching
database and keyrings together, verify connections, then resume services. A
database restore discards later writes and requires the normal recovery review.

After old-key removal, restore the old keyring to every process before any
database rollback or restore that may contain old-key rows. Do not restore an
older database against a keyring that cannot decrypt it.
