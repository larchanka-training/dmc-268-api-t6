# #333 disposable Bitbucket Cloud live acceptance

This is the execution checklist for a disposable private Bitbucket Cloud
repository selected for #333. Record sanitized evidence in the issue/PR after
each step. A PR, scoped credential and deployed same-SHA stack are still required;
the local review has not exercised this checklist. As of 2026-10-28 there is
no disposable private repository or token available, and work is limited to
local checks. The 2026-10-26 read-only probes covered a public UUID locator
only. Never copy an API token, webhook secret, Authorization header, keyring,
raw signed payload or ciphertext into Git, chat, issue or PR.

## Preconditions

1. Obtain a scoped Bitbucket Cloud API token through the team's secret channel;
   if stored locally for the disposable test, use a private file with mode 0600
   outside Git and read its value only inside the process making the request.
   The runtime connection token needs `read:repository:bitbucket` and
   `read:pullrequest:bitbucket` according to the REST endpoint references;
   test those exact scopes on private repository GET, PR GET, inline comment
   POST and commit-status POST/PUT. Record 401/403 without token text, and
   confirm no broader saved token scopes are required. Use separate
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
   Compare those five wire response events from connect and secret rotation
   with the webhook configuration; a three-event response is a contract failure.
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
   key (`AI-REVIEW-` plus 30 hex characters), terminal `SUCCESSFUL` state and
   a portal Run URL. Repeat after a token rotation and record an ownership
   failure rather than silently switching status keys if the author changes.
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

## Focused provider probes before disconnect

- **Private UUID route and fork (R4-01, R1-16).** Read the connected private
  repository through the runtime's percent-encoded UUID locator. Open a PR
  from a private fork that the destination token cannot read directly, while
  the destination PR and commits list remain readable. Verify one Run is
  projected, its pinned source SHA and destination repository UUID are exact,
  and an inaccessible fork yields a bounded, explicit failure only when the
  destination APIs cannot establish the source head. Repeat fulfillment and
  rejection after the fork branch is deleted (`source.commit = null`); neither
  event may create a new review. Keep the fork's repository and credential
  under this disposable test workspace.
- **Pinned diff and lifecycle (R1-15, R1-16).** Make a rename plus edit and
  inspect old/new file paths and inline coordinates. Compare optional or
  missing diffstat, more than one page of commit history and diffstat, and
  merge-base selection against the PR UI. Advance the destination branch tip
  without changing its branch identity: the existing Run must retain its
  original base SHA. Retarget the PR to a different branch or advance the
  source head between diff fetch and publication: the old Run must not
  publish comments or a successful status. Toggle draft with the same head
  and record the expected Run behavior.
- **Webhook ordering and diagnostics (P3-14).** Deliver two signed PR updates
  with equal `updated_on` and distinct source heads; the newer head must not
  be lost. Check a `diagnostics:ping` delivery without `repository.uuid`
  receives 400 and creates no receipt or Run. Keep sanitized event/order
  evidence only.
- **Limits and failure surfaces (R1-05, R4-02).** Observe the Bitbucket rate
  limit bucket and `Retry-After` on a disposable test request, including a
  missing or HTTP-date value if Bitbucket emits one. Record request counts for
  required diff/blob reads and optional context separately. A 429 or exhausted
  request budget must report the correct retryable or terminal code; optional
  context exhaustion must leave a diff-only review when required data is
  complete. Do not manufacture provider load to force a 429 on a shared repo.
- **Status and merge check (R1-03, R1-06, R1-31).** Confirm the ≤40-character
  deterministic status key and portal target URL on the pinned source SHA.
  Exercise a skipped/failed Run and inspect `STOPPED` plus its description and
  the repository's actual merge-check interpretation. For a fork, verify
  whether the destination token can write status to the source commit; record
  403 without claiming success. Redelivery and worker restart must not create
  duplicate keys or reverse a newer same-head Run. Whether an old head's
  `INPROGRESS` needs remote `STOPPED` after supersession remains a tech-lead
  decision (P3-52), not an inferred acceptance result.

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
| Focused probes | Private UUID/fork outcome, pinned SHAs, page counts, rate-limit headers, status/merge-check behavior |

No row in this checklist is evidence of live success until a real private PR
and deployed stack have been exercised. The public read-only locator probe
established only its documented URL shape; current open gates are recorded in
[the task list](333-todo.md) and [PR #340](https://github.com/larchanka-training/dmc-268-api-t6/pull/340).
