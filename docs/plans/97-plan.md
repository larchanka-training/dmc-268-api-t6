# Implementation Plan: GitLab publication, overview review, and pipeline-step SSE (#97)

## Overview

Specification: https://github.com/larchanka-training/dmc-268-api-t6/issues/97.
UI design input: comment by axyi on that issue, from
[larchanka-training/dmc-268-ui-t6#84](https://github.com/larchanka-training/dmc-268-ui-t6/issues/84).
This document is planning only. It does not authorize product-code edits until the
development loop picks up `docs/plans/97-todo.md`.

Issue #97 reverses two v1 decisions. Publication today is one GitHub REST review
(`GitHubPullRequestReviewGateway` → `POST /pulls/{n}/reviews`), idempotent by the
`<!-- ai-review findings_hash=… -->` marker. The review body is `problem`,
`done_well`, `effort`, plus Additional findings. Verdict and severity counts are
only on the check-run. `GET /api/stream` emits only `run.updated` `{runId, status}`
after `NOTIFY run_updated`. Phases are rows in `run_actions` and do not leave the
process. `InMemoryRunUpdateHub` keeps one pending status per run.

The acceptance criteria to implement:

- A succeeded run of an open GitLab merge request leaves inline comments on the
  new-side lines of the findings and one summary review. A second publish with the
  same `findings_hash` does not create another copy.
- The summary text posted to GitHub and GitLab contains the verdict
  (`blocking` / `attention` / `clean`), the checked file paths, and a severity
  risk summary. The GitHub check-run title still shows the verdict.
- A client of `GET /api/stream` can tell a recorded pipeline step from
  `run.updated` while the run is in progress. A state change still sends
  `run.updated` with `runId` and `status`.
- GitLab publication, the overview body, and the step event are tested with no
  live VCS and no LLM. `uv run ruff check .`, `uv run ruff format --check .`,
  `uv run mypy .`, and `uv run pytest` are green.
- `docs/SYSTEM_DESIGN.md` and `docs/PIPELINE_SPEC.md` describe the new behavior
  in the same change as the code.

Out of scope, and not a task below: GitHub GraphQL, WebSocket, a separate
publisher process, frontend parsing (`dmc-268-ui-t6`), GitLab webhook ingestion
or `try_enqueue`, the full GitLab `line_code` / `line_range` adapter and
multi-operation receipt model in `docs/CONTEXT_AND_VERIFICATION_SPEC.md` §9.5
and §10, LEFT-side comments, Bitbucket, an `attempt` or `kind` field, and
writing the still-absent `engine.fallback` action.

Branch when implementation starts: `feat/97-vcs-publish-gaps`. No new dependency.
`httpx` is already the HTTP client (`httpx.MockTransport` is the existing test
double). GitHub and GitLab calls stay outside database transactions.

## Current behavior

- `PublishRunReview` (`app/modules/reviews/application/publish_run_review.py`)
  loads `PublishContext`, returns without finishing the run when the GitHub
  gateway is missing, posts outside the transaction, records
  `github.publish_review`, and on one coordinate `422` moves every inline
  finding into the body and posts a single review. Check-run title for success
  is already `AI-ревью: {verdict}` (`check_run_view`).
- `render_review_body` (`review_output.py`) emits only Main issue, Done well,
  and Effort. `FindingsPostProcessor._render_body` appends Additional findings
  and cuts the whole body at 4000 characters.
- `provider_installations.provider` exists (`github` today). `CodeChange` has
  `base_sha` and `head_sha` and does not store GitLab `start_commit_sha`.
  `comments.github_comment_id` is globally unique.
- `run_actions` are inserted in four places: `SqlAlchemyRunTraceStore.add_action`,
  `store_review_output` (`llm.review_output`), `enter_publishing`
  (`review.postprocess`, same transaction as the transition to `publishing` and
  `NOTIFY run_updated`), and `conventions_unit_of_work` (`llm.repo_conventions`).
- Worker and portal-api are different processes. Status reaches
  `GET /api/stream` only because the worker `NOTIFY`s `run_updated` and
  `app/bootstrap/run_update_listener.py` publishes into the portal hub. An
  in-process publish from the worker never reaches the browser.
- `InMemoryRunUpdateHub.publish` replaces the unread event for that `run_id`.
  Live `run.updated` frames carry `id:`; reconnect replay is status-only.

Baseline before any edit, from the issue kickoff:

```text
uv run pytest tests/test_publish_run_review.py tests/test_review_publisher.py tests/test_run_stream.py tests/test_run_stream_postgres.py
```

## Architecture Decisions

1. **Overview is part of the stored review body, shared by both providers.**
   Extend the body built in `FindingsPostProcessor` so the string already stored
   on the run is what both gateways post. Do not render a second body inside
   either gateway. Keep the existing prose sections and Additional findings.
   Put the new block first, after the `## Review summary` heading:

   ```text
   **Verdict:** blocking
   **Risk summary:** critical: 0, high: 1, medium: 0, low: 0, info: 0
   **Checked files:**
   - app/example.py
   ```

   Verdict and counts use `verdict()` and `severity_counts()` on the same
   non-dropped findings `hashed_findings` already hashes (inline + body-only).
   Checked paths are the sorted keys of `hunk_lines` (the files the
   post-processor treated as the reviewable diff). An empty set still leaves
   the heading, followed by `(none)`. The 4000-character cap applies only to
   the Additional findings tail. Verdict, risk summary, checked paths, and the
   three prose sections are not cut. A long path list may make the stored body
   longer than 4000 characters; it stays under GitHub's review-body limit.
   `move_inline_to_body` appends after this text, so a coordinate fallback is
   still one review and still contains the overview. The check-run title is
   unchanged (`AI-ревью: {verdict}`).

2. **GitLab publication is a second gateway behind the existing
   `review.publish` consumer, not a new process and not the §9.5 adapter.**
   Add `GitLabMergeRequestDiscussionGateway` next to the GitHub gateway. It
   uses `httpx` against `GITLAB_BASE_URL` (default `https://gitlab.com`) with
   header `PRIVATE-TOKEN` from `GITLAB_TOKEN`. Both variables are read in the
   worker. When `GITLAB_TOKEN` is unset, a GitLab run stays in `publishing`
   and is not marked succeeded — the same outcome as a missing GitHub App.
   One token and one base URL cover this issue; per-installation GitLab
   credentials are a later change.

   The gateway speaks the current GitHub idempotency rule, not
   `publication_key` / `line_code`:

   - Read `GET /api/v4/projects/{id}/merge_requests/{iid}/versions` outside
     any database transaction. Use the version whose `head_commit_sha` equals
     the run head. Take `base_commit_sha`, `start_commit_sha`, and
     `head_commit_sha` from that version. Do not copy `head_sha` into
     `base_sha` or `start_sha`. If that version is gone because the MR head
     moved, raise the existing stale-commit outcome (`cancelled` /
     `superseded`). A closed MR is still rejected from `PublishContext`
     before HTTP.
   - Inline discussion, one per finding, new side only:
     `POST .../discussions` with `position.position_type=text`, both paths set
     to the finding path, `new_line` set to the finding line, and the three
     SHAs above. No `old_line`, no `line_range`, no `line_code`. Comment text
     matches the GitHub comment body (title, body, optional suggestion fence).
   - One summary discussion, no `position`, whose body is the stored review
     body. That body already contains the overview.
   - Marker, same string GitHub already uses:
     `<!-- ai-review findings_hash={hash} summary -->` on the summary, and
     `<!-- ai-review findings_hash={hash} inline={path}:{line} -->` on each
     inline note. Before creating a discussion, list discussions with
     pagination. A failed or truncated list is retryable, not "nothing was
     posted". Skip a discussion whose marker is already present. A retry with
     the same hash therefore does not add a second summary or a second copy
     of an inline note.
   - A position rejection (GitLab 400 that names line, position, or diff
     refs, with the head still current) uses the existing one-shot
     `move_inline_to_body` path: stop further inline posts, put the remaining
     inline text into the summary, post that summary once. Do not post a
     second summary.
   - Map other HTTP failures onto the kinds `PublishRunReview` already
     handles: `retryable`, `forbidden`, `coordinates`, `stale_commit`. Store
     `gitlab_forbidden` and `gitlab_publish_failed` on the run. Do not reuse
     the `github_*` codes. Do not call the GitHub check-run gateway for a
     GitLab run.
   - Record `gitlab.publish_review` through the existing trace, with the same
     request shape as `github.publish_review`.
   - `complete_publication` still marks findings published and finishes the
     run. Do not insert GitLab discussion ids into `comments`:
     `github_comment_id` is globally unique and can collide with a GitHub
     comment id. Pass an empty `comment_ids` tuple so the current insert is
     skipped. Remote identity for a retry is the marker, not that table.

   `PublishContext` gains `provider` (`github` or `gitlab`), numeric project id
   (`repositories.external_id`), and `base_sha`. The consumer keeps the GitHub
   gateway for `provider=github` and the GitLab gateway for `provider=gitlab`.
   GitHub request JSON, marker lookup, 422 fallback, and check-run title stay
   as they are. This issue does not create GitLab installations, webhooks, or
   runs. Tests build a `publishing` run whose installation provider is
   `gitlab`.

3. **A pipeline step is a different SSE event on a different channel, and the
   hub does not collapse it.** Event name: `run.step`. Public `data` is
   camelCase and matches the UI comment:

   | Field | JSON | Rule |
   | --- | --- | --- |
   | `runId` | uuid | the run |
   | `tool` | string | `run_actions.tool`, not an enum |
   | `index` | integer ≥ 0 | `run_actions.index` |
   | `ok` | boolean | false only when the response object contains `error`; null and other objects are true |
   | `startedAt` | date-time | UTC RFC 3339 with `Z` |
   | `durationMs` | integer ≥ 0 | the stored duration |

   `additionalProperties: false`. No `status`, so the payload cannot be parsed
   as `RunUpdatedEvent`. No `attempt` and no `kind` / `operation` in this
   issue; ui#84 can add them later. Serialize `data` with `json.dumps`. Do not
   interpolate `tool` into a hand-built JSON string.

   The step frame has `event:` and `data:` and **no `id:` line**. `Last-Event-ID`
   replay stays exactly the current `run.updated` replay. A step must not
   replace the id the client will send on reconnect. Replay of steps is not
   required: after reconnect the UI reads `/actions` itself.

   Add `InMemoryRunStepHub` beside the status hub. Each subscriber has a FIFO
   list, not a dict keyed by run. Two steps of one run are both delivered, in
   order. Do not replace an unread step with a later step. Fan-out matches the
   status hub. The stream subscribes to both hubs before status replay, drops
   a step when `run_updated_at` is `None` (same visibility rule as status), and
   never turns a step into `run.updated`.

   Cross-process delivery uses a new channel `run_step`, not `run_updated`.
   Payload is snake_case and small: `run_id`, `tool`, `index`, `ok`,
   `started_at`, `duration_ms`. No request or response body. `NOTIFY` runs in
   the same transaction as the `run_actions` insert, so a rollback emits
   nothing. `ok` is computed from the response object before it is stored.
   A separate listener publishes into the step hub. A malformed step payload
   is logged and ignored. Every insert of `RunAction` goes through one helper
   that assigns the index and notifies, so `llm.review_output`,
   `review.postprocess`, `llm.repo_conventions`, and traced tools cannot forget
   the event. `review.postprocess` and `llm.review_output` stay in the
   transaction that moves the run to `publishing`; their step events become
   visible with that commit, together with `run.updated`. Do not split that
   transaction to emit them earlier.

4. **Docs change with the code, and they describe this adapter rather than
   §9.5.** Update Р-11, §8.1, and §8.4 in `docs/SYSTEM_DESIGN.md`, and the SSE
   rule, the GitLab row, and the phase table in `docs/PIPELINE_SPEC.md`.
   State that GitLab write-back implemented here is discussions plus
   `findings_hash` markers, and that `CONTEXT_AND_VERIFICATION_SPEC` §9.5 and
   §10 remain a later publication model. Bitbucket stays unimplemented.
   Document `GITLAB_TOKEN` and `GITLAB_BASE_URL` in `docs/SECRETS.md` and
   `.env.example` in the same change. No migration. No new package.

## Dependency graph

```text
Task 1 overview body ───────────────┐
Task 2 GitLab gateway (HTTP only) ──┼── Task 3 consumer dispatch
                                    │
Task 4 OpenAPI run.step ── Task 5 hub + stream frames
                              │
                              └── Task 6 NOTIFY on the trace insert + listener
                                        │
                                        └── Task 7 NOTIFY on the other inserts
                                                  │
Tasks 3 and 7 ────────────────────────────────────┴── Task 8 docs
```

Tasks 1, 2, and 4 share no product files and can run in parallel. Task 3 waits
for 1 and 2. Task 5 waits for 4. Task 6 waits for 5. Task 7 waits for 6.
Task 8 waits for 3 and 7 and lands in the same branch, not a follow-up PR.

## Task List

### Phase 1: Overview body

- [ ] Task 1: Render verdict, checked paths, and severity counts in the review body

### Phase 2: GitLab publication

- [ ] Task 2: Post GitLab MR discussions idempotently by findings hash
- [ ] Task 3: Dispatch the publish consumer for a GitLab run

### Checkpoint: Publication

- [ ] GitHub publish tests still pass, including the check-run title
- [ ] A fake GitLab MR receives inline notes and one summary, and a second pass does not duplicate them
- [ ] A missing GitLab token leaves the run in `publishing`

### Phase 3: Step stream

- [x] Task 4: Add the `run.step` OpenAPI event
- [ ] Task 5: Deliver `run.step` on the stream without coalescing
- [ ] Task 6: Notify `run_step` when a traced action commits
- [ ] Task 7: Notify `run_step` from the other action inserts

### Checkpoint: Step stream

- [ ] Two steps of one run both arrive, and neither is `run.updated`
- [ ] A rolled-back insert emits no step
- [ ] Status replay via `Last-Event-ID` is unchanged

### Phase 4: Docs

- [x] Task 8: Describe GitLab publication and `run.step` in the specs

### Checkpoint: Complete

- [x] Issue acceptance criteria are met by the tests and the two spec files
- [x] Gates green: `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy .`, `uv run pytest`
- [x] Ready for review

## Risks and Mitigations

| Risk | Impact | Mitigation |
| --- | --- | --- |
| GitLab rejects a new-side comment that needs `old_line` or `line_code` | High for live MRs; tests use a fake HTTP server | One-shot move of remaining inline text into the single summary. Do not invent SHAs or implement §9.5 in this issue. |
| Nothing on main creates a `provider=gitlab` run | High for a real MR; the AC is still testable | Tests insert the provider and the publishing run. Webhook ingestion stays out of scope and is named in the docs. |
| Reusing `comments.github_comment_id` for discussion ids | High if ids collide | Do not write GitLab ids into `comments`. Idempotency is the marker. |
| Putting the step on `run_updated` or into the status hub | High: breaks `RunUpdatedEvent` or drops intermediate steps | Separate channel, separate FIFO hub, no `status` field, no `id:` line. |
| Step notify only inside the API process | High: the worker's actions never reach the browser | `NOTIFY run_step` in the insert transaction, portal listener, same pattern as `run_updated`. |
| `CONTEXT_AND_VERIFICATION_SPEC` §9.5 still says GitLab is unimplemented and specifies a richer adapter | Medium: a later edit reverts this work as a spec violation | Task 8 states which contract the code follows. Do not edit the context spec in this issue. |
| `llm.review_output` and `review.postprocess` commit with the jump to `publishing` | Low: those two steps are not visible during `running` | Leave the transaction as it is. The events still use `run.step`. UI keeps using status for the publish stage. |
| Unbounded per-subscriber step list | Low at current action counts | FIFO with no per-run replacement. Do not add a drop policy in this issue. |
| One `GITLAB_TOKEN` for every GitLab project | Medium outside a single instance | Document the limit. Do not design installation metadata for tokens here. |

## Open Questions

None that block implementation. The choices above are the plan of record:
event name `run.step`, fields from the UI comment including `ok`, `startedAt`,
and `durationMs`, no step replay, discussions API with `findings_hash` markers,
and no schema migration.

Follow-ups explicitly not in this issue: `attempt` on the step or on
`RunAction`; `kind` / `operation` so the UI can tell a conventions `llm.call`
from the review `llm.call`; writing `engine.fallback`; GitLab webhook intake;
storing discussion ids without colliding with `github_comment_id`.
