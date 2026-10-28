# #333 disposable Bitbucket Cloud live acceptance

This is the execution checklist for a disposable private Bitbucket Cloud
repository selected for #333. Record sanitized evidence in the issue/PR after
each step. A PR, scoped credential and deployed same-SHA stack are still required;
the local review has not exercised this checklist. Never copy an API token, webhook secret, Authorization
header, keyring, raw signed payload or ciphertext into Git, chat, issue or PR.

## Preconditions

1. Obtain a scoped Bitbucket Cloud API token through the team's secret channel;
   if stored locally for the disposable test, use a private file with mode 0600
   outside Git and read its value only inside the process making the request.
   The runtime connection token needs `read:repository:bitbucket` and
   `read:pullrequest:bitbucket` according to the REST endpoint references;
   test those exact scopes against the disposable private PR. Use separate
   operator authority for creating the branch/PR and configuring its webhook;
   do not grant write scopes to the stored runtime token merely to prepare the fixture.
2. Verify the `staging` GitHub Environment has a protected
   `BITBUCKET_CREDENTIAL_KEYS` secret and `BITBUCKET_ACTIVE_KEY_ID` plus
   `BITBUCKET_WEBHOOK_BASE_URL` variables. The public URL must be HTTPS on
   `staging-api`, not the reserved `staging-webhook` 503 host. Check only names
   and URL origin; do not print keyring values. Deploy backend and UI from the
   reviewed commits with migrations and the API, webhook worker, review worker,
   RabbitMQ, PostgreSQL and LLM configured. Capture both deployed SHAs.
3. Initialize the empty disposable repository with a default branch, then
   create `codex/333-live` with a small Python change that can produce a
   clear review finding and a pull request from that branch into the default
   branch. Record the
   repository UUID, PR URL/number, source/destination commit SHAs and whether
   the repository is private. No other repository may be written.

## End-to-end checks

1. In the portal, explicitly select the intended portal workspace. Discover
   and connect the private Bitbucket repository using the scoped read token.
   Record portal workspace ID, installation ID, portal repository ID and the
   returned Bitbucket repository UUID; never record the token or one-time
   webhook secret. Verify a member sees it in `/api/repos` and a nonmember does
   not.
2. Set the repository webhook in Bitbucket to the one-time URL and secret from
   the connect response. Subscribe to `repo:push`, `pullrequest:created`,
   `pullrequest:updated`, `pullrequest:fulfilled` and `pullrequest:rejected`.
   The first valid signed delivery must switch that
   repository from `awaiting_webhook` to `active`; invalid signature must not.
   Record only delivery HTTP status, event type, hook/request UUIDs if safe and
   a timestamp. Inspect durable receipt/Run state without dumping body bytes.
3. On the open PR/head, wait for one Run to reach `succeeded`. Record Run UUID,
   portal Run URL, head SHA, deployed backend/UI SHAs and completion time.
   Inspect bounded Bitbucket PR comment pages: count findings with
   `ai-review run=<RUN_ID> ... inline=...` markers and exactly one summary
   marker for that Run. Check each inline path/line in the PR UI. Inspect the
   commit status on the same head; it must have the tenant-scoped deterministic
   key, terminal `SUCCESSFUL` state and a portal Run URL.
4. Trigger redelivery of the same signed event, then count Runs for the same
   PR/head, inline markers, summary markers and commit-status keys again.
   Expected: one Run, unchanged comment counts, one summary and one status key.
   Repeat after a worker restart if possible to exercise durable replay.
5. For a finding with a replacement, probe whether Bitbucket's public REST
   comment API can create a native applyable suggestion on this disposable PR.
   A fenced patch in `content.raw` is only manual comment text. Record the
   sanitized request shape, HTTP status and whether the Bitbucket UI shows an
   apply action. If no native operation is demonstrable, leave the native AC
   open and request explicit acceptance of the manual fallback; do not claim
   native support from mocked tests or the UI guide.
6. Disconnect the repository in the portal and remove its Bitbucket webhook.
   Confirm later hook deliveries and queued work are rejected, while a sibling
   binding (if used) remains usable. Record only statuses and counts.

## Evidence fields

| Field | Safe value |
| --- | --- |
| Repository and PR | Disposable URL, immutable repository UUID, PR number, privacy flag |
| Deployment | Backend/UI commit SHA, migration head, public API origin |
| Intake | Signed delivery timestamp/status, active binding state, receipt count |
| Review | Run UUID/URL/state, head SHA, inline/summary counts and coordinates |
| Status | Key, head SHA, terminal state and target URL |
| Replay | Counts before/after redelivery and restart |
| Suggestion | Sanitized REST shape, status and UI apply action or explicit fallback decision |

An earlier read-only private-repository probe established only access and UUID
at that time. No row in this checklist is evidence of live success until a
real PR and deployed stack have been exercised. See the
[round 1 checklist](333-review-round1-todo.md) for the remaining gates.
