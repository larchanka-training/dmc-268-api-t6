# Bitbucket follow-ups (#360)

This page records the API and worker behavior added for
[#360](https://github.com/larchanka-training/dmc-268-api-t6/issues/360).
The browser contract is [`contracts/openapi.yaml`](../contracts/openapi.yaml).
The Bitbucket webhook delivery contract is [BITBUCKET_WEBHOOK.md](BITBUCKET_WEBHOOK.md).

## Connections and credential rotation

`GET /api/workspaces/{workspace_id}/bitbucket/connections` still returns the
complete active connection list with `nextCursor: null`. The internal
`list_bindings` store now scans bounded keyset pages ordered by repository name
and ID. Each page and its position are scoped to the portal workspace and,
when supplied, the installation. Rotation drains all internal pages before
validating the replacement token. There is no public connection cursor: the
merged [UI #195](https://github.com/larchanka-training/dmc-268-ui-t6/pull/195)
schema requires `nextCursor` to be null.

Token rotation probes the current Bitbucket user and every bound repository
under one 30-second deadline, with at most four repository probes in flight.
Provider calls finish before the write transaction. On failure, the existing
safe JSON `detail` code is returned; a repository-specific 409, 422, 429 or
502 also carries the failing UUID in `X-Bitbucket-Repository-UUID`. No token
or provider URL is returned. Failed validation or a concurrent binding change
leaves the stored credential, generations, repository metadata and webhook
intake state unchanged. A successful token rotation preserves an already
confirmed webhook's `intake_enabled` state.

For encryption-key retirement, use the bounded, resumable
[key rotation runbook](KEY_ROTATION.md). It covers Bitbucket connection tokens
and repository webhook secrets as well as GitLab connection tokens. Keep old
and new keys deployed while migrating; verify zero old-key rows before removing
an old key. Its dry-run and verification output contains key-ID counts only.

## Push projection and retries

A signed push receipt selects matching open PRs, including fork-source branch
candidates, in bounded database pages instead of stopping after 500 rows. The
worker projects at most five PRs per pass, persists the completed page's next
position on the receipt, and resumes on retry or redelivery. A binding or head
change fences stale work; the existing PR/head Run uniqueness prevents a
duplicate Run after replay.

Deferred Run admission and stale canonical reads enter a receipt-owned retry
queue while later PR pages continue. A candidate gets up to three retries,
spaced by 30 seconds. Exhaustion records its reason and failure time and fails
the receipt. Revoking the origin terminalizes queued candidates. Continuing
receipts are ordered by effective due time so older large pushes do not starve
newer deliveries in a worker sweep.

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

The old whole-job 160-entry cap was removed in the Task 12 source changes. A
161-entry, two-page pinned tree completed at the exact listing, raw-read and
entry limits in a MockTransport test. This is local test evidence; live
Bitbucket request counts remain pending. With Task 14's independent review
complete, these limits supersede the older 160-entry/request statement in
[SYSTEM_DESIGN.md §8.5](SYSTEM_DESIGN.md#85-bitbucket-cloud-333). Reconcile
that core section after its owning PR #424 merges.
Other index limits still apply, including the global worker deadline, maximum
archive entries, per-file size, and storage quotas.

Completed pinned listing pages are checkpointed by repository, target SHA,
binding generation and index generation. A worker restart can reuse validated
pages without sending those requests again. A partial page cannot yield a
complete manifest or a ready index. A new target SHA, changed binding or purge
invalidates the old checkpoint. Quota exhaustion, HTTP 429 and a preempting
deadline release the build lease with a transient retry outcome; they do not
publish a partial index or write a sticky `failed` state. The provider's
`Retry-After` is honored when available. Build outcomes and logs count newly
sent pinned listing, metadata and raw blob requests separately; restored pages
do not count as new provider requests.

After index publication, the worker stores the complete pinned manifest for
that commit. This cache is reconstructible: a missing or untrusted published
manifest makes the next build list the full target tree. An ordinary push can
reuse the published manifest only with a verified `AHEAD` compare and complete
diffstat. The source checks each changed path through pinned
`GET /src/{commit}/{path}?format=meta`, validating its path, commit hash, type,
size and attributes before deriving the target manifest. An incomplete or
untrusted compare or metadata response falls back to a full listing. Changed
blobs are read as raw files; unchanged paths retain their published graph data.
The metadata request budget also covers head and compare calls.

In the 201-file MockTransport test, a full build sent 3 listings, 2 metadata
requests and 201 raw reads. A verified one-file push in a new worker instance
sent 0 listings, 5 metadata requests and 1 raw read. It wrote one changed file
and published no path deletion. These are local test counts, not live private
repository measurements.

## Contract and remaining proof

The API's UI Zod fixture is pinned to the exact merged UI source commit
[`7b18dcca46cc2edec88b482a70b1544c8e139651`](https://github.com/larchanka-training/dmc-268-ui-t6/commit/7b18dcca46cc2edec88b482a70b1544c8e139651)
from [UI #195](https://github.com/larchanka-training/dmc-268-ui-t6/pull/195).
Its `bitbucketConnectionList.nextCursor` remains null.

A disposable private Bitbucket repository has not been available for a live
listing/raw-read measurement. Record only sanitized request counts and commit
IDs when that measurement is run. The independent
[#333 live PR acceptance](https://github.com/larchanka-training/dmc-268-api-t6/issues/333),
including the native-suggestion decision, remains open.
