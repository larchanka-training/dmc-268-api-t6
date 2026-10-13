# Task List: PR commit context (#82)

Plan approved by the user ("go") on 2026-10-13; seven tasks follow.

## Task 1: Bounded production retrieval

**Description:** Fetch the bounded commit prefix through GitHubRunSource and compose
it into PullRequestMeta while preserving installation checkpoints and failure policy.

**Acceptance criteria:**
- [x] Two numeric pages maximum, 10/page, API order, 20-by-500 retained text and 1 MiB streamed page cap; no Link navigation or guessed omitted count.
- [x] Empty lists succeed; malformed JSON/types/messages/oversized pages fail explicitly; HTTP failures use existing safe classification.
- [x] Checkpoints surround token/network work; cancellation between pages prevents later requests and no GitHub request spans a DB transaction.

**Verification:** `uv run pytest tests/test_github_run_source.py` (or new focused
`tests/test_github_commit_messages.py`); mocked pagination, length boundaries,
Unicode, malicious Link, error/status, cancellation and transaction assertions.
**Observed evidence (Task 1):** Incremental red/green failures proved missing messages,
numeric pagination, Unicode truncation, malformed payloads, streamed limits and HTTP
classification. Independent review identified pre-cap gzip expansion and JSON
recursion errors; red/green regressions now prove compressed responses are rejected
before any stream read and excessive nesting raises safe `diff_fetch_failed`.
Commit requests use `Accept-Encoding: identity`; nonidentity response encodings fail
explicitly before decoding, so the streamed cap bounds buffered response bytes.
The first dedicated-stack full gate produced 3,469 passed / 2 failed in 859.21s; both
failures were the worker GitHub fake missing the new commits endpoint. That fixture
is corrected, and the focused source/origin/PostgreSQL suite plus both worker cases
now passes all 90 tests (14 other worker cases deliberately deselected). Lint,
format and mypy pass. An intermediate broad pytest gate with DB/Rabbit variables
unset is running; its integration skips must be reported. The final complete
stand-backed gate remains required after later source/provenance edits.
The three real PostgreSQL commit scenarios observed SQLAlchemy transaction begin/end
events and verified no open transaction at token or HTTP calls, including retirement
between pages and during the second-page token. Later tasks and whole-issue
completion remain pending.
**Dependencies:** Human plan approval; open-PR ownership refresh.
**Files likely touched:** `app/modules/reviews/infrastructure/github_run_source.py`,
optional adjacent commit helper, `tests/test_github_commit_messages.py`,
`tests/test_github_run_source.py` (at most 4).
**Estimated scope:** Medium.

## Task 2: Production prompt proof

**Description:** Verify fetched metadata reaches prompt construction and refresh
realistic golden fixtures, keeping existing XML escape and shortening behavior.

**Acceptance criteria:**
- [ ] Literal prompt assertions cover markup, multiline, empty and capped messages from fetched context.
- [ ] Golden fixture reflects agreed bounded context; no fabricated total omission marker.
- [ ] Required scope comment exists before editing any #133-owned file; otherwise use independent source/prompt tests and fixtures.

**Verification:** `uv run pytest tests/test_prompt_builder.py tests/test_github_commit_messages.py`.
**Dependencies:** Task 1.
**Files likely touched:** `tests/test_prompt_builder.py`,
`tests/fixtures/prompt_builder/sample.user.txt`, optional independent integration
test, system golden only if wording actually changes (at most 4).
**Estimated scope:** Medium.

## Checkpoint: Production behavior

- [ ] Retrieval and rendering tests green; no database/network-boundary regression.
- [ ] Existing renderer caps preserved; needed collision comments posted with authorization.

## Task 3: Honest deterministic evaluation context

**Description:** Freeze actual source-revision messages for the five real cases in
one repository-side JSON sidecar; use these snapshots in `_case_input` offline.

**Acceptance criteria:**
- [ ] Every real case has verified case ID/source URL/revision/commit URL/message; no invented historical PR list or synthetic history.
- [ ] All 19 synthetic cases stay empty; existing patch, pre-image and truth remain untouched.
- [ ] Missing, duplicate, mismatched or malformed records fail before paid calls; recorder tests prove nonempty context reaches the real-case provider request.

**Verification:** Focused recorder tests and `uv run python review/scripts/validate_dataset.py --final`; safe GitHub revision lookups provide metadata evidence without credential output.
**Dependencies:** Task 2; authorized #124 scope comment before `tests/test_eval_live.py` edit.
**Files likely touched:** `review/eval/pr-commit-messages.json`,
`review/scripts/eval_live.py`, optional `review/scripts/eval_commit_context.py`,
`tests/test_eval_live.py` (at most 4).
**Estimated scope:** Medium.

## Task 4: Pin shaping inputs

**Description:** Extend static provenance to cover production retrieval and frozen
evaluation commit context so replay detects input changes honestly.

**Acceptance criteria:**
- [ ] GitHubRunSource, sidecar and any new shaping helper are pinned; existing `eval_live.py` pin remains.
- [ ] Tests prove changes to each new input produce drift; no old manifest digest is edited.
- [ ] Fixture capture/replay stays deterministic and covers populated commit context.

**Verification:** Focused provenance/replay tests; two fixture replay reports compare equal.
**Dependencies:** Task 3.
**Files likely touched:** `review/scripts/eval_provenance.py`,
`tests/test_eval_provenance.py` (if existing) or new focused provenance tests,
`tests/test_eval_replay.py` (at most 3).
**Estimated scope:** Medium.

## Checkpoint: Ready for one capture

- [ ] Complete offline gates green; current old-baseline drift explicitly expected until replacement.
- [ ] Recheck/rebase main and open-PR ownership; all source/prompt/provenance changes final.
- [ ] Verify exact branch SHA, model, org-secret availability by workflow preflight, and human paid-run/raw-export authorization.

## Task 5: Scope documentation

**Description:** Document production prefix limits, evaluation snapshot semantics,
and retire the #82 deferral without claiming a completed baseline prematurely.

**Acceptance criteria:**
- [ ] Docs state bounds/order/error behavior and honest 5-real/19-synthetic metadata policy.
- [ ] Scope comments exist before editing #124/#148-owned documentation.
- [ ] Review branch contains all final shaping inputs before dispatch; no new dependency.

**Verification:** Inspect docs against literal retrieval tests; run all four required gates.
**Dependencies:** Task 4; collision-comment authorization.
**Files likely touched:** `docs/SYSTEM_DESIGN.md`, `review/README.md`,
`test-prs-dataset/README.md` (at most 3).
**Estimated scope:** Small.

## Task 6: Complete paid raw-first capture

**Description:** Dispatch `eval-live.yml` on the final approved branch with
`model=mistral-small-4`, `export_raw_responses=true`, using the team org secret.

**Acceptance criteria:**
- [ ] One approved complete 24-case fast run; record exact run ID/attempt/SHA/effective models and safe usage/cost metadata.
- [ ] `baseline_publishable=true`; safe artifact confirms every raw file hash/size and exact active-case mapping; no SEC-05.
- [ ] Copy all 24 answers and manifest byte-for-byte; no mixed capture, repair selection, or hand-edited digest.

**Verification:** Download raw and safe artifacts into separate directories; verify
regular files, completeness and matching SHA-256/size before replacing responses.
**Dependencies:** Task 5; explicit paid-run/export approval; external secret/balance/dispatch access.
**Files likely touched:** All 24 `test-prs-dataset/responses/<id>.json` plus
`manifest.json` as one atomic artifact publication (deliberate exception to the
5-file task guideline; splitting this capture would violate #82).
**Estimated scope:** Medium operational task; no implementation.

## Task 7: Recomputed baseline evidence

**Description:** Recompute offline #53 metrics from the accepted replacement and
update documentation with measured results and provenance.

**Acceptance criteria:**
- [ ] Two offline JSON reports equal, no static/corpus drift, all 24 cases represented.
- [ ] README/test strategy show actual first-answer validity, TP/FP/FN and requested quality metrics with denominators and run evidence; no unsupported improvement claim.
- [ ] Four required gates pass; CI replay passes; integration skips/limitations reported honestly.

**Verification:** Commands in `82-plan.md`: final corpus validation, replay twice,
`uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy .`, `uv run pytest`.
**Dependencies:** Task 6.
**Files likely touched:** `test-prs-dataset/README.md`, `docs/TEST_PLAN.md` (at most 2).
**Estimated scope:** Small.

## Checkpoint: Complete

- [ ] Issue #82 retrieval, tests, golden/provenance and full replacement criteria met.
- [ ] Final main rebase does not change shaping inputs after capture; if it does,
  obtain replacement-run approval and recapture all 24 before claiming completion.
- [ ] Ready for standards/spec review and conventional PR referencing #82.
