# Developer C correction evidence (#113 / PR #132)

Worktree: `132-review-fixes/dmc-268-api-t6`; source base `46a9077`, development
head `69505ab`. Final publication head and whole-plan gate counts belong to the
coordinator/publisher and are not inferred here. No dependency added.

## P2-4: successful review receipt survives retirement

The real `GitHubPullRequestReviewGateway` with httpx MockTransport and real
PostgreSQL now retains `SubmittedReview(review_id, ())` when the comment-lookup
checkpoint raises RunCancelled. It logs review_id/reason at INFO. No relaxed
checkpoint or post-retirement HTTP exception was introduced. Existing
`complete_publication` remains fenced and cancels without findings/comment writes.
The transactional append-only trace records `github.publish_review` with
`github_review_id: 55`. Repeated consumer execution starts no additional HTTP.

Red: `test_retirement_during_token_stops_next_http_request_with_live_positive_control[True-review-post]`
failed because the persisted trace response was None rather than review ID 55.
The strengthened matrix also covers post-rebind and retirement after comments,
retains literal request sequence checks, unchanged findings/terminal history and
no comments, and active positive controls. Green initial publication/context
suite: 48 passed, 0 skipped, 36.50 s.

## P2-5: RUNNING context producer with retired origin

`test_running_context_write_fences_retired_origin_without_changing_history`
exercises TransactionalRunTrace.save_context_payload directly, with RUNNING state
and retired origin, both absent and existing context rows. Literal cancellation
reason is installation_unavailable; context, Run/diff/findings/comments history
stay unchanged. Active controls insert/update the requested payload. This is
separate from terminal-state rejection in paid conventions tests.

Peer B yielded lifecycle for the isolated mutation. Removing only origin_active
from the context guard produced 2 failed (retired cases), 2 passed (active cases),
6 deselected in 3.55 s: cancellation was not raised. Original restored immediately;
no implementation change to lifecycle was needed.

## P3 cancellation, diagnostics and fixture proof

- Blob classification test cancels on first blob and asserts no result containing
  omissions, no second blob and no classification warning. Removing the explicit
  `except RunCancelled: raise` produced 1 failed/11 deselected in 0.15 s and two
  classification warnings; source restored.
- Projector retirement INFO test first failed due to empty logs, then passed with
  installation_id, repository_id and installation_unavailable. Existing early
  projector guard is retained; docstring now distinguishes App availability from
  user pause. B separately owns update_check_run diagnostics.
- Retained post-price LLM checkpoint has a concrete purpose before shared attempt
  counters increment: cancellation during FX/ledger awaits must not consume the
  paid-call allowance. `test_retirement_after_pricing_does_not_consume_provider_attempt_budget`
  uses real transport, fake ledger-triggered cancellation, and another generation
  of the same one-call attempt after its fake predicate clears. Green 1 passed,
  177 deselected in 0.58 s. Removing that checkpoint failed with llm_unavailable
  because the first cancelled generation consumed the attempt allowance (0.48 s).
  Source restored; transport checks still fence every actual HTTP/key rotation.
- HTTP reinstall and context test files own TEST_DATABASE_URL skip fixtures and
  use the non-test portal_schema helper; no skip fixture import from test_*.
- Snapshot lock-wait watcher now recognizes FOR SHARE, matching B's provider fence
  change; its deterministic retirement/rebind assertion remains intact.

Focused 8-file suite: 327 passed, 0 skipped, 51.78 s (publication/context, HTTP
reinstall, run diff/VCS, projector, publication unit, LLM gateway). Focused mypy:
10 source files, no issues. Ruff formatting applied only to owned files.
Final repeated-publication/local-fixture publication/context verification:
48 passed, 0 skipped, 33.05 s. Whole-plan final gates are coordinator-owned.

## Documentation and accepted limits

SYSTEM_DESIGN §6.9 documents deferred unique same-account archive upsert,
permanent clean identity after late deletion, and explicit clean-target acceptance
by an exact newly persisted personal grant when the fresh OAuth snapshot omitted
numeric account identity. Such a grant never authorizes archive history transfer.
WEBHOOK_WORKER removes stale re-enable/pause-reset claims. PIPELINE_SPEC T3 names
paused/unavailable 409; T6/T11 and §6 expose both cancelled errorCode values; the
global transition note states inactive-origin precedence over retry/reconcile
T9/T12/T13/T17/T18 without queue publication or provider calls.

CICD runbook stops every old Run writer (api/webhook-worker/worker and independent
writers) before migration 0030/bootstrap; its host compose example was verified
against deploy/scripts/deploy.sh APP_DIR/.env and compose.yml + edge/ports override.
Downgrade 0029 folds availability into enabled but irreversibly drops numeric
account identity; repeat upgrade cannot reconstruct lineage. Original #106 legacy
installation 168453694 cannot restore #57 history; receipt backfill is out of scope.

Strict short-session per-request checkpoints remain accepted. Sampling/time-based
checks would permit additional work after retirement; future combined SELECT/pool
optimization may preserve the same request boundary. UI read-only evidence: every
409 currently maps to «У этого PR уже есть активный прогон или PR закрыт», which
omits paused/App-unavailable reasons. Documented as frontend follow-up only;
no UI edit or new issue. Pending companion #127/#125/#126/#121 combined-head
validation is an external prerequisite, not claimed complete here.
