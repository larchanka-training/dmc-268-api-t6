# Bitbucket follow-ups (#360)

This page records the API and worker behavior added for
[#360](https://github.com/larchanka-training/dmc-268-api-t6/issues/360).
The browser contract is [`contracts/openapi.yaml`](../contracts/openapi.yaml).
The Bitbucket webhook delivery contract is [BITBUCKET_WEBHOOK.md](BITBUCKET_WEBHOOK.md).

## Connections and credential rotation

`GET /api/workspaces/{workspace_id}/bitbucket/connections` returns up to 100
active bindings per page, ordered by repository full name and ID. A non-null
`nextCursor` is an opaque, versioned keyset position bound to that portal
workspace; pass it as the optional `cursor` query parameter for the next page.
The last page returns `nextCursor: null`. An invalid or foreign-workspace cursor
returns `400 invalid_cursor`, an unsupported version returns
`409 cursor_expired`, and an empty or overlong query value returns 422. The
cursor is at most 1,024 characters. Pages are not a snapshot: a binding added
before the saved position may be missed, while removals or name changes between
requests can change later pages. Replaying a cursor against unchanged bindings
returns the same position.

The internal `list_bindings` store still drains bounded keyset pages for token
rotation. Each internal page and its position are scoped to the portal
workspace and, when supplied, the installation. Rotation drains all pages
before validating the replacement token.

The binding-page migration backfills `repositories.workspace_id` for existing
Bitbucket bindings once. During a rolling deployment, pause connection writes
from old API instances between that migration and the new API rollout. An old
writer can otherwise create a binding with `workspace_id = NULL`, which the
new list and rotation queries cannot see. Before resuming writes, verify that this
read-only query returns zero and investigate/rebind any missing rows:

```sql
SELECT count(*)
FROM repositories AS repository
JOIN bitbucket_repository_identities AS identity
  ON identity.repository_id = repository.id
WHERE repository.workspace_id IS NULL;
```

Token rotation probes the current Bitbucket user and every bound repository
under one 30-second deadline, with at most four repository probes in flight.
Provider calls finish before the write transaction. On failure, the existing
safe JSON `detail` code is returned; a repository-specific 409, 422, 429 or
502 also carries the failing UUID in `X-Bitbucket-Repository-UUID`. No token
or provider URL is returned. Failed validation or a concurrent binding change
leaves the stored credential, generations, repository metadata and webhook
intake state unchanged. A successful token rotation preserves an already
confirmed webhook's `intake_enabled` state.
The overall deadline can expire while several healthy probes are in flight;
that response has no repository UUID header. Provider-specific failures log
only their UUID and safe code, while an ambiguous deadline logs
`repository_uuid=unknown` with `provider_unavailable`.
The 30-second deadline bounds the whole rotation, not each request. A
workspace whose repositories cannot all be checked within that window gets
`provider_unavailable` (502), even if no repository is broken. Retrying alone
does not increase this deadline; there is no separate repository-count limit.

For encryption-key retirement, use the bounded, resumable
[key rotation runbook](KEY_ROTATION.md). It covers Bitbucket connection tokens
and repository webhook secrets as well as GitLab connection tokens. Keep old
and new keys deployed while migrating; verify zero old-key rows before removing
an old key. Its dry-run and verification output contains key-ID counts only.

## Push projection and retries

A signed push receipt selects open PRs by source branch name in bounded
database pages instead of stopping after 500 rows. The scan can include PRs
from forks with the same branch name; each PR is checked against its canonical
head before Run admission. The worker processes at most five PRs per pass and
persists the completed page's next position. A subsequent worker retry resumes
from that position; a duplicate delivery of a completed receipt does no work.
A binding or head change fences stale work, and PR/head Run uniqueness prevents
a duplicate Run after replay.

An unfinished page schedules the next pass immediately and sends a
transaction-bound worker notification. Thus 503 candidates require 101 bounded
passes without a fixed 30-second polling wait between pages. Actual completion
time still depends on provider response time and rate limits.

Deferred Run admission, stale canonical reads, malformed individual PR responses,
and a PR or its commit returning 404, 5xx or a transport error after a successful
repository lookup enter a receipt-owned retry queue while later PR pages continue.
HTTP 403 remains a repository/credential error handled by ordinary failed-dispatch
retries; it is not treated as a candidate-local response.
Deferred Run admission is retried no earlier than 30 seconds, for up to one hour
from receipt arrival. Other candidate failures wait at least five minutes
between queued attempts and get up to three queued retries. Exhaustion records
the reason and failure time, then fails the receipt. HTTP 429 at any stage, or
5xx/transport failure during repository lookup, instead pauses at the saved cursor,
honors `Retry-After` when present and does not consume ordinary receipt attempts.
If a shared provider error recurs at or after six hours from receipt arrival,
the receipt fails with `provider_retry_expired` in a WARNING log. A normal 429
window inside that limit remains retryable. Parsed `Retry-After` hints are capped
at 24 hours; a single window may extend beyond six hours, and the receipt is
terminalized only if a shared provider retry recurs on its next attempt. When
a retry page leaves more candidates already due, the next pass continues
immediately; only future-due
candidates wait. Revoking the origin terminalizes queued candidates.
Continuing receipts are ordered by effective due time so older large pushes
do not starve newer deliveries in a worker sweep.

## Code-index source budgets and retry state

The Bitbucket index source has separate limits for pinned-tree listing requests,
metadata requests, raw blob reads, listed entries, source response bytes and
source time. Worker defaults and their environment variables are:

| Limit | Default | Environment variable |
| --- | ---: | --- |
| Pinned listing requests | 512 | `CODE_INDEX_BITBUCKET_LISTING_REQUESTS` |
| Metadata requests | 2,048 | `CODE_INDEX_BITBUCKET_METADATA_REQUESTS` |
| Raw blob requests | 2,048 | `CODE_INDEX_BITBUCKET_RAW_REQUESTS` |
| Listed entries | 100,000 | `CODE_INDEX_BITBUCKET_ENTRIES` |
| Source response bytes | 100 MiB | `CODE_INDEX_BITBUCKET_READ_BYTES` |
| Source time | 600 seconds | `CODE_INDEX_BITBUCKET_DEADLINE_SECONDS` |

The old whole-job 160-entry cap has been removed. A
161-entry, two-page pinned tree completed at the exact listing, raw-read and
entry limits in a MockTransport test. This is local test evidence; live
Bitbucket request counts remain pending. These budgets are reflected in
[SYSTEM_DESIGN.md §8.5](SYSTEM_DESIGN.md#85-bitbucket-cloud-333).
Other index limits still apply, including the global worker deadline, maximum
archive entries, per-file size, and storage quotas.

Completed pinned listing pages and size-verified raw blobs are checkpointed by
repository, target SHA, binding generation and index generation. Raw checkpoint
storage has a hard 100 MiB cap per repository, outside repository and workspace
storage quotas. A worker restart reuses validated pages and blobs without
sending those requests again. A partial page cannot
yield a complete manifest or a ready index. If a saved raw blob disagrees with
the pinned listing size, the worker clears that target checkpoint and refetches
the blob. A new target SHA, changed binding or purge invalidates the old
checkpoint; successful manifest publication clears it. HTTP 429 and a
preempting deadline release the build lease with a transient retry outcome,
honoring provider `Retry-After` when present.
Deterministic request, entry and byte limits instead produce a terminal
`limit_exceeded:*` state. Raw checkpoint data can remain after that terminal
state until a target or binding change or repository purge. Neither path
publishes a partial index. Build outcomes and logs count newly sent pinned
listing, metadata and raw blob
requests separately; restored pages do not count as new provider requests.

After index publication, the worker stores the complete pinned manifest for
that commit. This cache is reconstructible: a missing or untrusted published
manifest makes the next build list the full target tree. An ordinary push can
reuse the published manifest only with a verified `AHEAD` compare and complete
diffstat. The source checks each changed path through pinned
`GET /src/{commit}/{path}?format=meta`, validating its path, commit hash, type,
size and attributes before deriving the target manifest. An incomplete or
untrusted compare forces a full index rebuild. Untrusted metadata makes the
source list the full target tree before a possible sparse update. An unexpected
empty diff for different SHAs forces a full rebuild. Changed blobs are read as
raw files; unchanged paths retain their published graph data.
The metadata request budget also covers head and compare calls.

In the 201-file MockTransport test, a full build sent 3 listings, 2 metadata
requests and 201 raw reads. A verified one-file push in a new worker instance
sent 0 listings, 5 metadata requests and 1 raw read. It wrote one changed file
and published no path deletion. The same-size change with an empty diffstat at
a different head SHA instead took the full-build path. These are local test
counts, not live private repository measurements.

## Default rules, publication and status

Default backend/frontend rule artifacts are cached by their file metadata.
Changing an artifact invalidates that cache, and each caller receives a deep
copy so one onboarding cannot mutate another repository's defaults.

The Bitbucket status publisher checks the Run's provider before sending a
commit status. A GitHub or GitLab Run does not enter the Bitbucket status path.
Publication still rechecks the pinned head and claim before writing each
operation. The in-repo
[`test_publication_request_and_claim_cost_is_bounded_by_findings`](../tests/test_bitbucket_publication_resources.py)
uses Bitbucket's 12-character `source.commit.hash` and records GET counts of
6, 8 and 22 for 0, 1 and 8 inline findings respectively (a `2N+6` slope);
claim renewals were 2, 3 and 10. These are MockTransport counts, not a live
private-repository measurement or an `N+4` GET bound for the live response shape.

## Contract and remaining proof

The API's UI Zod fixture is pinned to the exact merged UI source commit
[`5f94a8e0725195dbdd022af498a2f21f8f9befed`](https://github.com/larchanka-training/dmc-268-ui-t6/commit/5f94a8e0725195dbdd022af498a2f21f8f9befed)
from [UI #262](https://github.com/larchanka-training/dmc-268-ui-t6/pull/262).
Its `bitbucketConnectionList.nextCursor` accepts a string or null.

A disposable private Bitbucket repository has not been available for a live
listing/raw-read measurement. Record only sanitized request counts and commit
IDs when that measurement is run. The independent
[#333 live PR acceptance](https://github.com/larchanka-training/dmc-268-api-t6/issues/333),
including the native-suggestion decision, remains open.
