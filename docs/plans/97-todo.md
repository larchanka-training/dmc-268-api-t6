# Tasks: GitLab publication, overview review, and pipeline-step SSE (#97)

Status: Tasks 1–8 done. Overview body budget (4000 / checked paths) deferred to
[#170](170-todo.md) (D-1 supersedes Task 1 overflow AC).
Design constraints: [97-plan.md](97-plan.md).
Every task uses RED → GREEN → refactor. Gates for every task:
`uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy .`,
`uv run pytest`. No new dependency. No `--no-verify`.

## Task 1: Render verdict, checked paths, and severity counts in the review body

**Description:** The stored review body gains a verdict line, a severity risk
summary, and the checked file paths, ahead of the existing prose. GitHub and
GitLab both post this string later; this task only changes how the body is
built. The check-run title stays `AI-ревью: {verdict}`.

**Acceptance criteria:**

- [x] For a processed review, `review_body` contains `**Verdict:**` with
      `blocking`, `attention`, or `clean` from the non-dropped findings, a
      `**Risk summary:**` line with `critical`, `high`, `medium`, `low`, and
      `info` counts, and a `**Checked files:**` list of the sorted `hunk_lines`
      paths (`(none)` when there are no paths).
- [x] Main issue, Done well, Effort, and Additional findings remain. A
      coordinate-style append still sits after the overview. **Body budget:** the
      stored overview is ≤ 4000 characters with whole finding bullets only;
      checked paths cap/truncate and overflow footer — see [#170](170-todo.md)
      (supersedes the pre-#170 “path list may exceed 4000” note).
- [x] Existing check-run title assertions (`AI-ревью: blocking` and the other
      verdict titles) still pass without a new check-run format.

**Verification:**

- [x] RED: a post-processor test fails because the body has no verdict, risk
      summary, or checked paths.
- [x] GREEN: render the overview from `verdict()`, `severity_counts()`, and
      `hunk_lines`, then run the gates.

**Dependencies:** None

**Files likely touched:**

- `app/modules/reviews/application/review_output.py`
- `app/modules/reviews/application/findings_post_processor.py`
- `tests/test_findings_post_processor.py`
- `tests/test_publish_run_review.py` (only if a body fixture assertion changes)

**Estimated scope:** Medium

## Task 2: Post GitLab MR discussions idempotently by findings hash

**Description:** Add a GitLab discussions gateway that, given a submission and
a token, posts new-side inline discussions and one summary discussion, and
skips any discussion whose `findings_hash` marker is already on the MR. No
database and no use-case wiring in this task.

**Acceptance criteria:**

- [x] Against `httpx.MockTransport`, a submission with two findings issues
      `GET .../versions`, then one `POST .../discussions` per finding with
      `position.new_line` and no `old_line`, then one summary `POST` whose
      body contains the given review body and the summary marker. SHAs on the
      position come from the version whose `head_commit_sha` matches the run.
- [x] A second `submit` with the same hash, when those markers are already in
      the listed discussions, issues no create `POST`.
- [x] A position `400` raises the coordinate kind once; a moved head raises
      stale-commit; `401`/`403` raises forbidden; `5xx` and a short discussion
      list raise retryable. No call runs inside a database session.

**Verification:**

- [x] RED: the new test fails because the gateway module does not exist.
- [x] GREEN: implement the gateway with `httpx` only, then run the gates.
- [x] Manual check: the test transport records URLs under `/api/v4/projects/`
      and never under `api.github.com`.

**Dependencies:** None. Safe to build in parallel with Tasks 1 and 4.

**Files likely touched:**

- `app/modules/reviews/infrastructure/gitlab_review_publication.py`
- `tests/test_gitlab_review_publication.py`

**Estimated scope:** Medium

## Task 3: Dispatch the publish consumer for a GitLab run

**Description:** `PublishRunReview` posts through the GitLab gateway when the
run's installation provider is `gitlab`, and through the GitHub gateway
otherwise. A missing GitLab token leaves the run in `publishing`. The posted
summary is the stored body from Task 1.

**Acceptance criteria:**

- [x] A publishing run with `provider=gitlab`, an open MR, and a configured
      token finishes `succeeded` after the gateway returns, records
      `gitlab.publish_review`, and does not call the GitHub check-run gateway.
      The summary body the gateway receives contains the Task 1 overview.
- [x] Calling execute again with the same `findings_hash` does not create a
      second summary or a second inline discussion. `comments` gains no row
      for the GitLab discussion ids.
- [x] With `GITLAB_TOKEN` unset the run stays `publishing` and no GitLab HTTP
      call is made. A GitHub run still posts one review, still collapses one
      coordinate failure into that same review, and still gets a check-run
      whose title includes the verdict.

**Verification:**

- [x] RED: extend `tests/test_publish_run_review.py` with a fake GitLab gateway
      and a gitlab `PublishContext`; it fails on the current GitHub-only consumer.
- [x] GREEN: thread `provider`, project id, and `base_sha` through
      `publish_context`; select the gateway in the consumer; construct the
      real gateway from `GITLAB_TOKEN` / `GITLAB_BASE_URL` in `app/worker.py`
      only when the token is set. Then run the gates.
- [x] Manual check: GitHub tests in `tests/test_publish_run_review.py` and
      `tests/test_review_publisher.py` still pass.

**Dependencies:** Tasks 1 and 2

**Files likely touched:**

- `app/modules/reviews/application/publish_run_review.py`
- `app/modules/reviews/infrastructure/run_lifecycle_store.py`
- `app/worker.py`
- `tests/test_publish_run_review.py`
- `.env.example`

**Estimated scope:** Medium

## Checkpoint: After Tasks 1-3

- [x] `uv run pytest tests/test_publish_run_review.py tests/test_review_publisher.py tests/test_gitlab_review_publication.py tests/test_findings_post_processor.py` passes
- [x] Gates green
- [x] GitHub publication behavior is unchanged apart from the richer body
- [x] Review with the development loop before starting the stream tasks if Tasks 1-3 were done in isolation

## Task 4: Add the `run.step` OpenAPI event

**Description:** Declare `run.step` on `GET /api/stream` so a client can tell it
from `run.updated` by the event name and the schema. No hub and no writer yet.

**Acceptance criteria:**

- [x] `contracts/openapi.yaml` defines `RunStepEvent` with required `runId`,
      `tool`, `index`, `ok`, `startedAt`, and `durationMs`, and
      `additionalProperties: false`. `GET /api/stream` `x-events` lists
      `run.step` beside `run.updated`.
- [x] The stream description still says `run.updated` is the status event and
      says `run.step` is not replayed and has no `id:`.
- [x] `tests/test_openapi_contract.py` validates a sample `run.step` payload
      against that schema and rejects a payload that includes `status` or omits
      `tool`. Redocly still ignores the schema as an unused component, same as
      `RunUpdatedEvent`.

**Verification:**

- [x] RED: the contract test fails because `x-events` has no `run.step`.
- [x] GREEN: add the component, the `x-events` entry, and the Redocly ignore,
      then run the gates.

**Dependencies:** None. Safe to build in parallel with Tasks 1 and 2.

**Files likely touched:**

- `contracts/openapi.yaml`
- `tests/test_openapi_contract.py`
- `.redocly.lint-ignore.yaml`

**Estimated scope:** Small

## Task 5: Deliver `run.step` on the stream without coalescing

**Description:** The portal stream writes a `run.step` frame from a step hub
that keeps every unread step. Status frames and status replay stay as they are.

**Acceptance criteria:**

- [x] Publishing two steps for one run before the subscriber reads yields two
      frames, in order, each `event: run.step` and neither `event: run.updated`.
      `data` is the Task 4 object (`runId`, `tool`, `index`, `ok`, `startedAt`,
      `durationMs`) produced with `json.dumps`.
- [x] A step frame contains no `id:` line. `Last-Event-ID` replay still emits
      only `run.updated`. A step whose run is not visible is dropped.
- [x] A slow subscriber still receives only the latest `run.updated` per run.
      A step never occupies that slot, and a status publish never drops a step.

**Verification:**

- [x] RED: a hub test expects two queued steps and observes the status hub's
      single-slot behavior instead.
- [x] GREEN: add `InMemoryRunStepHub` and multiplex it in `GET /api/stream`,
      then run `uv run pytest tests/test_run_stream.py tests/test_openapi_contract.py`
      and the gates.

**Dependencies:** Task 4

**Files likely touched:**

- `app/modules/reviews/application/run_events.py`
- `app/main.py`
- `tests/test_run_stream.py`

**Estimated scope:** Medium

## Task 6: Notify `run_step` when a traced action commits

**Description:** Committing a traced `run_actions` row notifies `run_step` in
that same transaction. The portal listener fans the payload into the step hub
from Task 5. Rollback emits nothing.

**Acceptance criteria:**

- [x] After commit, a `LISTEN run_step` client receives one payload with
      `run_id`, `tool`, `index`, `ok`, `started_at`, and `duration_ms` for that
      row. `ok` is false when the response contains `error`, and true otherwise.
      The payload has no request or response body.
- [x] A rolled-back insert delivers no notification. A second action for the
      same run is a second notification, not a replacement.
- [x] The portal lifespan listens on `run_step` and publishes into the step hub.
      A malformed payload is logged and does not kill the `run_updated` listener.
      `vcs.fetch_diff`, `context.build`, `llm.call`, and `github.publish_review`
      (the trace path) are covered by this helper.

**Verification:**

- [x] RED: a PostgreSQL test waits for `run_step` after `RunTrace.record` and
      times out on current main.
- [x] GREEN: one insert helper used by `SqlAlchemyRunTraceStore.add_action`,
      plus `app/bootstrap/run_step_listener.py` started from `app/main.py`.
      Run the new test, `tests/test_run_stream_postgres.py`, and the gates.
- [x] Manual check: `NOTIFY` is not issued on a connection of its own; it uses
      the session that inserts the action.

**Dependencies:** Task 5

**Files likely touched:**

- `app/modules/reviews/infrastructure/run_action_records.py`
- `app/modules/reviews/infrastructure/run_lifecycle_store.py`
- `app/bootstrap/run_step_listener.py`
- `app/main.py`
- `tests/test_run_step_postgres.py`

**Estimated scope:** Medium

## Task 7: Notify `run_step` from the other action inserts

**Description:** `llm.review_output`, `review.postprocess`, and
`llm.repo_conventions` are inserted outside the trace store. Route those
inserts through the Task 6 helper so they emit `run_step` too, without moving
the publishing transaction.

**Acceptance criteria:**

- [x] Saving a review output and entering `publishing` each produce a
      `run_step` notification for `llm.review_output` and `review.postprocess`
      only after that transaction commits. The existing `run_updated`
      notification for the transition to `publishing` still happens once.
- [x] Saving repository conventions emits `run_step` for `llm.repo_conventions`
      with the assigned `index`.
- [x] A failure that rolls back the publishing transaction emits neither
      `run_step` nor `run_updated` for that attempt.

**Verification:**

- [x] RED: the PostgreSQL test records `llm.review_output` or
      `review.postprocess` and sees no `run_step` payload.
- [x] GREEN: call the Task 6 helper from the three remaining insert sites,
      then run `tests/test_run_step_postgres.py` and the gates.

**Dependencies:** Task 6

**Files likely touched:**

- `app/modules/reviews/infrastructure/run_repository.py`
- `app/modules/reviews/infrastructure/conventions_unit_of_work.py`
- `tests/test_run_step_postgres.py`

**Estimated scope:** Small

## Checkpoint: After Tasks 4-7

- [x] A client of `GET /api/stream` receives `run.step` for a committed action and `run.updated` for a state change, and can tell them apart by `event:`
- [x] Two steps of one run are both present; status coalescing still keeps one `run.updated` per run
- [x] `tests/test_run_stream.py` and `tests/test_run_stream_postgres.py` pass
- [x] Gates green

## Task 8: Describe GitLab publication and `run.step` in the specs

**Description:** `docs/SYSTEM_DESIGN.md` and `docs/PIPELINE_SPEC.md` currently
say GitLab has no implementation and that the only outward SSE event is
`run.updated`. Update them to match Tasks 1-7. Document the GitLab token.

**Acceptance criteria:**

- [x] Р-11, §8.1, and §8.4 say GitHub and GitLab discussion publication are
      implemented, Bitbucket is not, and GitLab intake (webhooks, diff, blobs)
      is not. §8.4 describes `findings_hash` markers and new-side `new_line`
      posts, and points at `CONTEXT_AND_VERIFICATION_SPEC` §9.5 and §10 as a
      later model this code does not implement.
- [x] PIPELINE_SPEC §1 says a state change still notifies `run_updated` /
      `run.updated`, and an action insert notifies `run_step` / `run.step`
      with the public fields. The phase table includes `gitlab.publish_review`.
      T16 names `gitlab_publish_failed` and `gitlab_forbidden`. The §13 row
      that says GitLab is only a port is updated. Step frames have no `id:`
      and are not replayed.
- [x] `docs/SECRETS.md` lists `GITLAB_TOKEN` and `GITLAB_BASE_URL` for the
      worker. `.env.example` already names them from Task 3.

**Verification:**

- [x] Read the edited sections against `gitlab_review_publication.py`,
      `publish_run_review.py`, `run_events.py`, and `contracts/openapi.yaml`.
      The docs name the same event, fields, marker, and error codes as the code.
- [x] Gates green. No product-code change in this task unless a name drifted;
      if it drifted, fix the doc to the code.

**Dependencies:** Tasks 3 and 7

**Files likely touched:**

- `docs/SYSTEM_DESIGN.md`
- `docs/PIPELINE_SPEC.md`
- `docs/SECRETS.md`

**Estimated scope:** Medium

## Checkpoint: Complete

- [x] After a succeeded GitLab run, the fake MR has inline notes on finding lines and one summary that includes verdict, checked paths, and severity counts; the same hash does not post again
- [x] GitHub summary text includes those three overview parts, and the check-run title still shows the verdict
- [x] `GET /api/stream` delivers `run.step` distinct from `run.updated` while a run is in progress
- [x] `docs/SYSTEM_DESIGN.md` and `docs/PIPELINE_SPEC.md` match the code
- [x] `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy .`, and `uv run pytest` are green
- [x] Ready for review
