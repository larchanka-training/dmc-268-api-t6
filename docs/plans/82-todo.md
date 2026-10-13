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
- [x] Verify exact branch SHA, model, org-secret availability by workflow preflight, and human paid-run/raw-export authorization.

Pre-capture source readiness is verified by the Task 5 evidence below. The user
authorized one complete fast capture with raw export and primary `mistral-small-4`.
The exact dispatched source SHA and successful workflow secret preflight/provider
execution are recorded in Task 6; the readiness prerequisite is now satisfied.

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
- [x] One approved complete 24-case fast run; record exact run ID/attempt/SHA/effective models and exported configured pricing/policy limits; explicitly state actual spend and billed-token totals are unavailable from these artifacts.
- [x] `baseline_publishable=true`; safe artifact confirms every raw file hash/size and exact active-case mapping; no SEC-05.
- [x] Copy all 24 answers and manifest byte-for-byte; no mixed capture, repair selection, or hand-edited digest.

**Verification:** Download raw and safe artifacts into separate directories; verify
regular files, completeness and matching SHA-256/size before replacing responses.
**Observed evidence (Task 6):** The single approved raw-export workflow succeeded:
[run 38468081001, attempt 1](https://github.com/larchanka-training/dmc-268-api-t6/actions/runs/38468081001),
source SHA `80ede3cd274d745616b8a24082ff7d1d00f5b736`, recorded at
`2026-10-13T21:06:56.682693Z`. Its successful secret preflight/provider execution
satisfies the pending org-secret prerequisite without copying or exposing the key.
The exported engine is `fast`, primary `mistral-small-4`, fallback
`mistral-small-3.2-24b`; all 24 first calls are `answer` / `primary` using
`mistral-small-4` with serving-provider label `mistral` and accepted gateway status.
Every paid-metadata error is false; `baseline_publishable=true` and the
nonpublishable-case list is empty.

Independent verification found exactly the active 24 case IDs, no `SEC-05`,
canonical response mapping, and only regular files with no symlinks in both exports.
All 24 raw hashes and byte counts match their safe per-case metadata; the raw/safe
manifests are byte-equal. All 25 response-directory files were replaced as one set,
byte-for-byte from this capture, then rechecked against the downloaded raw bytes.
No answer normalization, repair selection, mixed capture or hand-edited digest was
used. The 24 raw answers total 42,174 bytes. Previous responses remain backed up
outside the checkout at `/tmp/82-task6-previous-responses`.

Exported configured prices in USD per million tokens (input/cache-read/output):
primary `0.56125 / 0.56125 / 2.35725`; fallback
`0.2245 / 0.2245 / 0.449`. Both use input budget 52,000, output cap 8,000,
`json_schema` structured output and 3 characters/token; context windows are
262,144 primary and 128,000 fallback. Exported policy: nominal per-case cost limit
USD 0.50 (USD 12 across 24 cases), input-token limit 60,000, maximum 4 calls/attempt,
90s call timeout, timeout retry delays `[2]`, unavailable delays `[2, 8]`, rate-limit
default delay 2s, maximum Retry-After 30s and jitter 1s. These are configured pricing
and policy values; actual spend, billed-token totals and remaining balance are not
exported and are unavailable from these artifacts.

Final corpus validation passed 24/24. Two offline JSON reports are byte-identical
with `warnings=[]`; redacting only validator diagnostics produces byte-identical
JSON to the live safe report. Independently confirmed first-answer metrics are
7/24 valid, TP 2 / FP 2 / FN 17, critical recall 2/5 and verdict agreement 4/24.
Existing metric documentation remains unchanged until Task 7. Focused replay,
recorded-baseline CI-contract and provenance tests: 100 passed in 8.90s, no skips.
Ruff lint, format check (435 files) and mypy (432 source files) pass. The Task 7
final full dedicated-stack gate and independent artifact review remain pending.

Verification hashes (SHA-256):
- Raw and safe manifest: `00ed7404809d86fea4beb7ba1b6cf52800591740ea0f38ea8cb54b7553a4992e`.
- Live safe/redacted offline report: `a875e1489fe7a47d35222f9b9feca95b757a19b792c70ba0b44d2a59d0425bad`.
- Each complete offline report: `918123f7a490c479410834b664f9834d694c66e123c3b4b6688de35801b9ceae`.

| Case | Raw bytes | Raw SHA-256, matched to safe metadata |
| --- | ---: | --- |
| CLEAN-01 | 1581 | `460edbc600f88736427a445c15b3f2bf438c2ab08a437d19a88c32cc0d3ba82a` |
| CLEAN-02 | 261 | `f9791ceaecb29792c4b649ac8968c7a9aa87fb5d577e24f289b18058db9827a8` |
| CLEAN-03 | 6455 | `161c03fe0caf14600215ba7cf3ecc1aa31a5bc4441c655869f034747d4b2d72f` |
| CLEAN-04 | 1077 | `39509236f596345c4f36fab40466f7712a710ca30e6f0916dfd3dcad0c4dc5cb` |
| CLEAN-05 | 227 | `33409e200e9bda8b4659c337de0c9ecea1d47ee7b996b3a65d65f919b6cae99a` |
| LOG-01 | 1676 | `02fecad5b64b93984a5382bfb661f857649a0df9ddb738ec56b7c60849bb1ad4` |
| LOG-02 | 901 | `c3ff9ab7d450b9bf52305cadc8a70f42f9b04a6281f3ed9105c94a96a777fda8` |
| LOG-03 | 3683 | `0755ecac456b5b7ab57de3aef0e903bbf003d410143ede8cf98a75c280a6cc12` |
| LOG-04 | 1061 | `a7982d9c184d92ef3087622d03de4c155dc9ed82f6f72800e9a650f3c70bc792` |
| LOG-05 | 1036 | `563e99af6d1bb959e8b7566aecb2747c0c608f3c94e3c6a213fb82c847391b37` |
| RES-01 | 1774 | `ef14ac40ca69731fc4f326ded308ae8fd1763290258c97cf5d4b4cebabe22a92` |
| RES-02 | 1026 | `1680bd8979dfb89d64cd149e433ee6931d709008090ae3d17ec84b464ffcc05e` |
| RES-03 | 2875 | `b2f0ebbf8a307d7b4e36b5090eb5596b96cc2bedb963bd38e42cb18a04c69b2b` |
| RES-04 | 1029 | `42376b653fed2089c25eb4b920bbfcec5b33e37e9f7ec8c37503bf2325678759` |
| RES-05 | 876 | `645a6f4ab02a831eaf592da88900ee93d226398563b3c6669b423bf5db177212` |
| SEC-01 | 1427 | `4467075e4f8727e0fd1eea2263fac920e4dc4a1052578957aa4198b0fdcb9756` |
| SEC-02 | 3141 | `c37b1ec757a457b248733bc5f15c820463897aca3fc468851f28e27323950009` |
| SEC-03 | 247 | `43aeb074fbd71b20d6673e82732e821aa6020fd7d676e9a2358850827d1c2065` |
| SEC-04 | 1542 | `d5a7124f46138fc862913168d4891647c877e628a6d5a90a2a9ebda46b515375` |
| SYN-01 | 267 | `c765d7ac52650157f4d05c7fa279ef23f0f6422bb28dbef1de2d38220f9417e4` |
| SYN-02 | 2143 | `c5c66b382b3aed406c41bc42e3b3c5231c33e69ee5f4857c4024b3cd22597b18` |
| SYN-03 | 1820 | `45efc1510310173ac5011f3b1dd0fc3f1c731e54fb4bd0ce07c0af7495bf88eb` |
| SYN-04 | 5033 | `65b8c6cb5f762eaad344e5c0f890b3f4a8e2995f230452985870baaa61c8fe76` |
| SYN-05 | 1016 | `32599342d7ea34be88f330af59c6dd10e81801ff641edbc4ea83f72276764406` |

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
