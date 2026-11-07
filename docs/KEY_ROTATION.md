# Stored credential key rotation

This runbook covers Bitbucket connection tokens, Bitbucket repository webhook
secrets, and GitLab connection tokens. GitLab webhook secrets are stored as
digests and have no encryption key to migrate. Run the operator in the deployed
image on the application host; PostgreSQL is reachable only on its Compose
network, and that image does not contain `uv`. The profile-only
`credential-key-rotation` service receives the protected `gitlab.env` and
`bitbucket.env` files and its database URL from Compose. The keyring variables
contain JSON objects of key ID to canonical base64 encoded 32-byte keys. Never
put key material or database credentials in command arguments, shell history,
or logs.
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
   Confirm new writes use the new IDs. Check that API, review worker and webhook
   worker were recreated by the deployment and are healthy; an old long-lived
   worker must not keep an earlier keyring. Keep old keys available for reads.
4. On the authorized staging or production host, choose its actual `APP_DIR`:
   `/opt/dmc-268-api-staging` or `/opt/dmc-268-api` for staging, and
   `/opt/dmc-268-api-prod` for production. Use the following Bash prefix in
   that directory; it reads no credential file into the shell:

   ```bash
   set +o xtrace
   # Select the target before running the remaining commands:
   APP_DIR=/opt/dmc-268-api-staging  # course VPS staging
   # APP_DIR=/opt/dmc-268-api        # Terraform staging
   # APP_DIR=/opt/dmc-268-api-prod   # production
   cd "${APP_DIR:?set the target deployment APP_DIR}" || exit 1
   project="${COMPOSE_PROJECT:-$(basename "$PWD")}"
   source ./env-file.sh
   mode="$(read_compose_env_var DEPLOY_MODE .env)"
   [[ "$mode" == edge || "$mode" == ports ]] || exit 2
   rotation=(docker compose -p "$project" --env-file .env -f compose.yml \
     -f "compose.${mode}.yml" --profile credential-operator)
   "${rotation[@]}" run --rm --no-deps -T credential-key-rotation dry-run &&
   "${rotation[@]}" run --rm --no-deps -T credential-key-rotation run \
     --batch-size 100 --max-batches 10
   ```

   `dry-run` checks that every stored key ID is available and reports counts by
   key ID and table, without selecting ciphertext. Do not run migration with
   a missing old key. Each `run` pass selects at most 100 rows per table,
   commits each successful compare-and-swap separately, and reports updated
   counts. Repeat `run` until its `ready` value is true. Restarting after
   interruption is safe: rows already using the active key are skipped. A
   concurrent rotation or revocation wins over a stale operator update.
5. Serialize this step with deployment and other operator actions. Quiesce
   credential writes by stopping `api`, `worker` and `webhook-worker` in the
   selected Compose project, and confirm they are stopped. Continue in the
   same Bash session as step 4; if it ended, repeat that prefix with the same
   `APP_DIR` before running `verify`:

   ```bash
   "${rotation[@]}" stop api worker webhook-worker
   "${rotation[@]}" ps
   "${rotation[@]}" run --rm --no-deps -T credential-key-rotation verify
   ```

   Require exit code zero and **zero rows under every old key ID** in all three
   tables. Investigate a nonzero count or operator error while retaining both
   keys; never remove an old key on a partial result. `run` with a corrupt row
   prints only its table kind, row UUID, key ID and exception class for up to
   100 failed rows, plus a total failure count; it continues later rows and
   tables but exits nonzero with
   `ready=false`. Repair the stored row or restore its keyring from the protected
   backup, then rerun `dry-run`, `run` and `verify`. Never copy ciphertext or
   exception text into logs or issues. If retirement must be postponed, keep
   both keys deployed and restart the stopped services with
   `"${rotation[@]}" start api worker webhook-worker` before ending maintenance.
6. Remove an old key only after that check, while writes remain quiesced, and
   deploy the reduced keyring to all readers and workers. The reviewed deploy
   starts the stopped services with the reduced keyring; confirm health and
   newly created container IDs before resuming operator changes. Retain the
   protected backup according to backup policy.

`--batch-size` accepts 1–500 rows per table and `--max-batches` accepts
1–10000 passes per invocation. Use smaller batches if operational load warrants
it. The command selects bounded ciphertext only in `run`; `dry-run` and
`verify` select key IDs and counts, never plaintext or ciphertext. The CLI
prints structured row diagnostics for credential errors; unexpected database
or configuration exceptions still print only a generic error code. For a
disposable local test database, `uv run python -m app.credential_key_rotation`
uses the same commands when its protected environment is set.

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
