# Plan: Bitbucket repository rules status (UI #195 review round 3)

## Context

The UI #195 reviewer found that `GET /api/repos/{id}/rules-status` can return
`pending` forever for a connected Bitbucket repository. The API requests a read,
but `SqlAlchemyRulesRefreshStore.claim` selects only GitHub installations and
`webhook-worker` composes the rules reader only when GitHub is configured. The
UI mock currently shows `pending` followed by `missing`, so it hides the API gap.

The existing `BitbucketRepositorySnapshotSource` already provides bounded,
credential-fenced `head()` and `rules_document(sha)` reads. Reuse it to deliver
a real terminal result. This preserves the product's promised repository rules
behavior and avoids inventing an unsupported status. No HTTP call may run inside
a database transaction.

## Design and dependency order

1. Extend the worker's claimed target with a provider discriminator. Claim active
   GitHub and Bitbucket repositories; leave the existing GitHub push lookup scoped
   to GitHub. Keep claim, provider read, and save as separate transactions.
2. Add a Bitbucket rules reader that resolves the current bound repository target,
   obtains the default branch head, reads `.review/rules.md` through
   `BitbucketRepositorySnapshotSource`, and maps the result to the existing
   `RulesReading` variants. Dispatch by the claimed provider. A missing file is
   reported only after a complete pinned tree listing. Inactive credentials,
   ambiguous reads, and malformed responses are `unavailable`, never `missing`.
   The reader must not log token or document content.
3. Compose the multi-provider refresher in `webhook-worker` whenever GitHub or
   Bitbucket is configured. The Bitbucket credential cipher and HTTP client are
   already available to that worker. Connect should request an initial read in
   the same short transaction as the repository binding; GET remains a recovery
   trigger and the ten-minute refresh path stays intact.
4. Align the API/UI architecture documentation with actual Bitbucket triggers.
   The UI mock's pending-to-missing fixture and its tests remain valid once the
   backend can make the same transition. Do not change PR #196-owned mock rules
   fixture or PR #194-owned repository list.

## Verification

- RED then GREEN: focused tests for Bitbucket target claim and provider routing;
  present, reliably missing, invalid, and unavailable documents; branch change;
  inactive/rotated binding; connection-triggered refresh; GitHub regression.
- PostgreSQL integration tests, where `TEST_DATABASE_URL` is available, prove
  claim/lease/save and status transition through the endpoint. Test HTTP reads
  with `httpx.MockTransport`; a mock is not a live Bitbucket acceptance result.
- API gates: `uv run ruff check .`, `uv run ruff format --check .`,
  `uv run mypy .`, `uv run pytest`. UI gates after documentation or mock changes:
  `pnpm lint`, `pnpm check-types`, `pnpm format:check`, `pnpm test`,
  `pnpm build`, `pnpm verify:prod-bundle` and docs-site checks.
- After API #340 is merged and deployed to staging, connect a Bitbucket test
  repository and confirm `GET /api/repos/{id}/rules-status` leaves `pending`
  within one minute. Record API/UI deploy SHAs and the provider fields in the
  other P1 probe endpoints before asking the reviewer to close either thread.

## Integration and ownership

### UI `c581bf6` contract parity on the stacked API branch

The isolated branch is based on API #340 `f7135aa`, whose three Bitbucket request DTOs still accept unrestricted strings and whose webhook setup emits three events. UI #195 now pins 1–4096 printable ASCII token characters and all five webhook events. The five failing Zod/OpenAPI mirror cases are real differences from this base; the separate API integration candidate already has the intended contract but is not the base of this stacked PR.

Before publishing the stacked branch, add one focused RED/GREEN contract-parity task. Use one shared token validation path for discovery, connect and credential rotation, preserve route paths and response envelope, and emit `pullrequest:fulfilled` and `pullrequest:rejected` alongside the existing three events for connect and secret rotation. Update the three OpenAPI token fields (maxLength 4096, exact printable-ASCII pattern) and webhook event enum. The current base's global FastAPI 422 handler echoes rejected input, so a new invalid-token test must first prove that the token is absent from the response body; use the candidate's sanitized validation-handler pattern or an equally complete non-echoing handler. Do not add token validation without that protection. Keep slug changes outside this narrow task.

The intended candidate already edits `app/main.py`, `connections.py`, `contracts/openapi.yaml`, and `tests/test_openapi_contract.py`. Treat this as a small compatibility delta for the stacked branch; compare those files during later integration and keep the candidate's more complete validation, rather than blindly replaying or overwriting either version. Repin the API fixture if UI moves beyond `c581bf6`.

- Work in isolated `/private/tmp/api340-contract-pin`; it already contains the
  reviewed UI contract-pin changes for UI SHA `99077506ddee9efc0282f0f88ce2e4aa061c9ffe`.
  Do not edit `/private/tmp/pr340-review-comments`: it has more than 100 local
  changes from parallel API review work. Inspect and merge with that owner before
  publishing API #340. Rebase the integration branch and regenerate the UI Zod
  fixture if the UI SHA changes.
- `/private/tmp/ui195-review-round2` is clean at published UI SHA `9907750`.
  Limit UI edits to its own documentation/mock behavior. PR #195 remains Draft
  until API-first staging evidence is attached.
- Reply in the new UI #195 P2 thread with the exact API head and test evidence
  after publishing the backend fix. Reply in the P1 thread with the API pin and
  explain that staging probe remains pending; leave thread resolution to the
  reviewer.

## Risks

| Risk | Mitigation |
| --- | --- |
| Bitbucket default branch changes during a read | Use the pinned SHA from `head()` and existing credential/binding fences; save branch and SHA actually read. |
| Incomplete tree or provider failure looks like absence | Reuse `rules_document` complete listing and map all uncertain outcomes to `unavailable`. |
| Concurrent API #340 review edits overwrite this patch | Keep isolated worktree and integrate only after inspecting the parallel diff; use a patch or cherry-pick with conflict review. |
| Staging behavior differs from mocked provider | Keep PR #195 Draft and retain both review threads until the live probe. |
