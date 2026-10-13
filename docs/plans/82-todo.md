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
format and mypy pass. The intermediate broad pytest gate with DB/Rabbit variables
unset passed: 2,860 passed / 616 skipped / 2 existing deprecation warnings in 286.00s.
The final complete stand-backed gate remains required after later source/provenance
edits; the intermediate skipped tests are not claimed as executed.
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
- [x] Literal prompt assertions cover markup, multiline, empty and capped messages from fetched context.
- [x] Golden fixture reflects agreed bounded context; no fabricated total omission marker.
- [x] Required scope comment exists before editing any #133-owned file; otherwise use independent source/prompt tests and fixtures.

**Verification:** `uv run pytest tests/test_prompt_builder.py tests/test_github_commit_messages.py`.
**Observed evidence (Task 2):** The updated realistic sample context first failed the
old user golden at byte 1,146; a literal commit-block fixture update passed. The
fixture covers multiline markup, an empty message, and a 542-character summary
shortened to 500 including ellipsis. Public source-to-`build_prompt` assertions prove
literal escaped text, a 20-message API-order prefix, the Unicode cap, and no guessed
omission marker. The focused suite passed 61 tests with the dedicated PostgreSQL
service enabled (2 existing deprecation warnings, no skips); lint, format and mypy
pass. No #133-owned source or test file was edited; the renderer and system golden
remain unchanged. A real end-of-file hook added a terminal newline to the exact-byte
user golden and reproduced its failure. Only that hook-added byte was restored from
the staged intended fixture. Both whitespace-mutating hooks now narrowly exclude
`tests/fixtures/prompt_builder/*.txt`, preserving system/user wire-byte goldens while
keeping existing raw-artifact exclusions. The restored golden and scoped pre-commit
run pass. Later evaluation work and final full-service gates remain pending.
**Dependencies:** Task 1.
**Files likely touched:** `tests/test_prompt_builder.py`,
`tests/fixtures/prompt_builder/sample.user.txt`, optional independent integration
test, system golden only if wording actually changes (at most 4).
**Estimated scope:** Medium.

## Checkpoint: Production behavior

- [x] Retrieval and rendering tests green; no database/network-boundary regression.
- [x] Existing renderer caps preserved; needed collision comments posted with authorization.

## Task 3: Honest deterministic evaluation context

**Description:** Freeze actual source-revision messages for the five real cases in
one repository-side JSON sidecar; use these snapshots in `_case_input` offline.

**Acceptance criteria:**
- [x] Every real case has verified case ID/source URL/revision/commit URL/message; no invented historical PR list or synthetic history.
- [x] All 19 synthetic cases stay empty; existing patch, pre-image and truth remain untouched.
- [x] Missing, duplicate, mismatched or malformed records fail before paid calls; recorder tests prove nonempty context reaches the real-case provider request.

**Verification:** Focused recorder tests and `uv run python review/scripts/validate_dataset.py --final`; safe GitHub revision lookups provide metadata evidence without credential output.
**Observed evidence (Task 3):** The first real-case provider-request test failed with no
commit block, then passed with the verified frozen snapshot. Six late `SYN-04`
missing/duplicate/mismatch/type regressions failed before validation, then passed
with zero provider requests and no capture staging. Malformed JSON, nested JSON,
invalid schema/record/Unicode data, and invented synthetic-history regressions also
pass before any provider call. A 24-case fixture capture with HTTP sends forbidden
proved exactly five literal real commit blocks and nineteen synthetic requests with
no commit block. The `LOG-03` snapshot retains all 878 characters verbatim; its
literal provider context is 500 characters including ellipsis, matching production.
All five sidecar records and verification evidence equal the verified research
artifact; `test-prs-dataset` has no changes. Final corpus validation passed 24/24.
Recorder/workflow suite: 108 passed in 122.08s. Lint, format, mypy and patch whitespace
checks pass. The parser stays inside already-pinned `eval_live.py`; Task 4 must pin
the new sidecar and production source. Final full-service gates and paid replacement
remain pending; no evaluation quality or billed-cost improvement is claimed.
**Dependencies:** Task 2; authorized #124 scope comment before `tests/test_eval_live.py` edit.
**Files likely touched:** `review/eval/pr-commit-messages.json`,
`review/scripts/eval_live.py`, optional `review/scripts/eval_commit_context.py`,
`tests/test_eval_live.py` (at most 4).
**Estimated scope:** Medium.

## Task 4: Pin shaping inputs

**Description:** Extend static provenance to cover production retrieval and frozen
evaluation commit context so replay detects input changes honestly.

**Acceptance criteria:**
- [x] GitHubRunSource, sidecar and any new shaping helper are pinned; existing `eval_live.py` pin remains.
- [x] Tests prove changes to each new input produce drift; no old manifest digest is edited.
- [x] Fixture capture/replay stays deterministic and covers populated commit context.

**Verification:** Focused provenance/replay tests; two fixture replay reports compare equal.
**Observed evidence (Task 4):** Public capture/replay tests first failed because edits
to either new input produced no warning. Adding exactly two static paths made both
mutations report the literal static-digest drift warning, retaining the existing
`eval_live.py` pin and preserving captured raw bytes and all manifest bytes. Two
populated-context fixture captures produced identical provider messages/manifests
and two byte-identical JSON replay reports with `warnings=[]`, including the JSON
sidecar's existing custom-rule path inference. The first broader focused run found
18 outdated fixture-inventory failures; adding the same two required paths to
`_record_with_digests` fixed them without changing replay code or assertions.
The complete focused provenance/replay/recorder suite passed 175 tests in 30.12s;
lint, format, mypy and patch whitespace checks pass. The historical corpus/manifest
is unchanged and its old-baseline drift remains expected until complete replacement.
The pre-capture full-service gate and remaining readiness checkboxes stay pending.
**Dependencies:** Task 3.
**Files likely touched:** `review/scripts/eval_provenance.py`,
`tests/test_eval_provenance.py` (if existing) or new focused provenance tests,
`tests/test_eval_replay.py` (at most 3).
**Estimated scope:** Medium.

## Checkpoint: Ready for one capture

- [x] Complete offline gates green; current old-baseline drift explicitly expected until replacement.
- [x] Recheck/rebase main and open-PR ownership; all source/prompt/provenance changes final.
- [ ] Verify exact branch SHA, model, org-secret availability by workflow preflight, and human paid-run/raw-export authorization.

Pre-capture source readiness is verified by the Task 5 evidence below. The user
already authorized one complete fast capture with raw export; the selected primary
model is `mistral-small-4`. The final dispatch SHA and workflow secret preflight
remain publisher actions, so their combined checkbox is intentionally pending.

## Task 5: Scope documentation

**Description:** Document production prefix limits, evaluation snapshot semantics,
and retire the #82 deferral without claiming a completed baseline prematurely.

**Acceptance criteria:**
- [x] Docs state bounds/order/error behavior and honest 5-real/19-synthetic metadata policy.
- [x] Scope comments exist before editing #124/#148-owned documentation.
- [x] Review branch contains all final shaping inputs before dispatch; no new dependency.

**Verification:** Inspect docs against literal retrieval tests; run all four required gates.
**Observed evidence (Task 5):** The architecture, prompt-contract and corpus READMEs
now describe numeric two-page retrieval, 20-by-500 API-order limits, the 1 MiB
identity-response cap, explicit failure/installation checkpoints, and frozen
5-real/19-synthetic revision snapshots. Required #124/#148 scope comments preceded
these edits. The original response set/manifest and old measured metrics remain
intact; complete recapture and new metrics are explicitly pending. A read-only
GitHub PR-64 commits request with `per_page=10` and identity encoding returned 200
JSON with no Content-Encoding, as verified by the coordinator.
Publisher's main rebase was a no-op at `c1d8f59d`; integrated source/docs review at
`68ed8b1` reported Standards 0 / Spec 0. Mandatory Ruff lint, format check and mypy
passed. The complete dedicated PostgreSQL/RabbitMQ pytest gate passed 3,508 tests,
zero skips, and 2 existing deprecation warnings in 662.32s (11:02), including both
previously failing worker composition scenarios. Log: `/tmp/82-pre-capture-pytest.log`.
No source tuning or dependency change followed these checks. Publisher will record
the exact dispatch head and run the workflow secret preflight before the single
authorized paid capture; secret availability and actual spend are not claimed here.
**Dependencies:** Task 4; collision-comment authorization.
**Files likely touched:** `docs/SYSTEM_DESIGN.md`, `review/README.md`,
`test-prs-dataset/README.md` (at most 3).
**Estimated scope:** Small.

## Task 6: Complete paid raw-first capture

**Description:** Dispatch `eval-live.yml` on the final approved branch with
`model=mistral-small-4`, `export_raw_responses=true`, using the team org secret.

**Acceptance criteria:**
- [ ] One approved complete 24-case fast run; record exact run ID/attempt/SHA/effective models and exported configured pricing/policy limits; explicitly state actual spend and billed-token totals are unavailable from these artifacts.
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
