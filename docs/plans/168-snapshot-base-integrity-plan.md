# Implementation plan: pinned DiffMap base and snapshot integrity on read (#168, part 3)

## Status and baseline

Planned 2026-10-25. Decided by the tech lead on 2026-10-25 (per the coordinator's brief): D1–D3,
including the D2 refusal code `pr_base_changed`. Issue:
https://github.com/larchanka-training/dmc-268-api-t6/issues/168, part 3 (P2).

- Baseline: `origin/main` `4faa03dd`.
- One PR from `main` with Tasks 1–4. Whoever merges second rebases; if #280 lands first, we
  rebase.
- Worktree `projects/dmc-268-api-t6-168d`, branch `fix/168-snapshot-base-integrity`.
- Test database and RabbitMQ vhost: `imp168d`.
- Task list: [168-snapshot-base-integrity-todo.md](168-snapshot-base-integrity-todo.md).

Every `file:line` was checked against `4faa03dd` (`pr/<n>` refs: `git show pr/<n>:<path>`).
The issue cites `7546e53`. At `4faa03dd` those spots are `diff_map.py:196,209`
[`:186,199`] and `review_context_stage.py:429` [`:291`]. The brief had two facts wrong:

- `review_context_stage.py:429-430` raises `RunFailure("diff_fetch_failed")` on a new head.
  The `no_changes` path is `:431-436`.
- `models.py:86` is `CodeChange.base_sha`. The pinned value is `Run.base_sha` (`:238`).

## Context

### AC1: where the DiffMap base comes from

- `build_diff_map` (`diff_map.py:174-215`) has one base input, `pull_request.base_sha`. It
  goes into the digest (`:196`) and into the field (`:209`).
- `ReviewContextStage.run` passes the live PR (`:428`) to `get_merge_base` (`:437-444`) and
  to `build_diff_map` (`:445-451`). It compares only the head (`:429-430`).
- The policy is pinned: `begin` uses `run.base_sha` (`:395`), and `get_pins` reads
  `Run.base_sha` (`policy_snapshot_unit_of_work.py:114`).
- No app code reads `DiffMap.policy_base_sha`. The field with a real effect is `diff_old_sha`:
  - GitHub `get_merge_base` compares `{pull_request.base_sha}...{head}`
    (`github_vcs.py:322-323`). After a retarget it returns the merge base of the new target.
  - The stored patches were cut against the old one. LEFT blocks are read at the wrong
    revision (`context_provenance.py:91-96`), and LEFT anchors are checked against it
    (`review_output.py:282-287`).
  - CONTEXT §9.1 (`:1291-1293`) wants the merge base of the *pinned* target and head.

### Where the base is pinned, and when a retarget can happen

- `Run.base_sha` is copied from `CodeChange.base_sha` when the run is created: webhook
  (`webhook_runs.py:102`, insert `:131`) and rerun (`rerun_store.py:76`).
- `pull_request.edited` updates `CodeChange.base_sha` (`project_github_pull_request.py:464`).
  It cancels nothing: only a closed PR or a new head cancels (`:479-495`). So a rerun pins
  the new base. A second webhook run on the same head is impossible
  (`uq_runs_webhook_code_change_head`, `models.py:200-205`; SD:323, PIPELINE_SPEC:707).
- GitHub's `base.sha` does not follow the base branch by itself. Observed 2026-10-25 while
  `main` was `4faa03dd`: the `base.sha` of #283 (`1ae74b1a`), #220 (`5d183fe6`) and #227
  (`74b42a71`) each equals its merge base with `main`. Both D1 and D2 fire only on a real
  retarget. A head push is caught by the head checks.
- **Before the diff is stored (D2).** `ReviewRunProcessor.prepare` runs `begin` first
  (`process_run.py:157`), so the policy snapshot already exists. When no diff is stored yet
  (`:163-164`), it calls `FetchVcsReviewInput.execute(locator, head, Run.base_sha)` inside
  the `vcs.fetch_diff` step (`:170-175`).
  - A different base raises `ValueError("GitHub pull request base SHA changed")`
    (`vcs_diff.py:166-167`).
  - `classify_failure` maps it to `internal_error` (`run_failures.py:112-125`), which is
    retried. Every attempt fails again, because the pin never changes.
  - A retarget during pagination fails `github_vcs.py:310-311`, also with a `ValueError`.
    The next attempt then hits `:166-167`.
- **After the diff is stored (D1).** The stored patches are the pinned-base diff: the
  checks above guard the fetch, and the stage never fetches again
  (`tests/test_review_context_stage.py:126-129`). GitHub `/pulls/{n}/files` takes no base
  (`github_vcs.py:278-280`).
- GitLab (#220) returns `expected_input.base_sha` as the PR base, and that value is
  `Run.base_sha` (`pr/220` `app/bootstrap/gitlab_reviews.py:129`). It cancels as
  `superseded` on a mismatch (`pr/220` `gitlab_vcs.py:377-382`, `:532-536`). Fixes 1 and 2
  do not change GitLab.

### Publication path and digest agreement

- `load_publication_diff_map` (`run_repository.py:635-678`) already uses `Run.base_sha`
  (`:642`, `:675`). No edit there.
- On `main` its digest differs from the stage's anyway: it passes `diff_old_sha=None`
  (`:676`) and `files_changed=len(snapshots)` (`post_processing_diff_map.py:49`).
- #283 compares them. It rebuilds a map from `Run.base_sha`, the traced `diff_old_sha` and
  `files_changed = len(durable) + (not complete)`. It restores coverage only when that digest
  equals the saved one (`pr/283` `run_repository.py:715-739`, `:724`; `run_coverage.py:285`).
  So the retarget reason must stay out of the DiffMap and its digest.

### AC2 and D3: what is stored

- **Policy documents.** `run_policy_documents` keeps `raw_bytes` (`models.py:303`, migration
  `20261011_0029_run_policy_snapshots.py:32`) and the normalized `text` (`:304`), plus
  `raw_sha256`, `text_sha256` and `byte_count`. A CHECK ties the content columns to
  `presence` (`models.py:286-293`). So `raw_sha256` can be recomputed, with no new column.
- **What the reads check today.**
  - `get_snapshot` (`policy_snapshot_unit_of_work.py:165-200`) checks only that both
    documents exist (`:172-177`).
  - `get_staged_documents` (`:131-137`, `_document` `:66-77`) checks nothing, and a retry
    reuses its rows (`prepare_policy_snapshot.py:171-183`).
- **What `policy_digest` covers.** It hashes the documents' checksums and `ignore_digest`,
  but not the patterns (`prepare_policy_snapshot.py:103-124`; RULES §7.1 `:661-665`). The
  matcher is built from the patterns (`review_context_stage.py:401-403`).
- **Recompute from stored values only.** Module constants are unsafe: `PARSER_VERSION` is
  `markdown_it.__version__` (`review_ignore.py:25`). Rehydrated objects are unsafe too:
  `_bindings` and `_profile` drop unknown keys (`policy_snapshot_unit_of_work.py:50-63`).
- **Prompt pins on `main`.** `get_pins` (`:110-129`) pins only `review.system`: id
  `Run.prompt_version_id`, its checksum and `v{version}`. The conventions and verifier
  checksums are `""` with id `None`.
  - After #280, `get_pins` fills all three for new runs.
  - Historical runs stay `NULL`: migration `pr/280` `20261024_0039` does not backfill.
- **The referenced versions are immutable.** Prompt rows cannot change (`seed_prompts.py:123-127`).
  `rule_versions.checksum` is the SHA-256 of the canonical rules
  (`repositories/infrastructure/models.py:160-165`). The prompt checksum is over the source
  file (`seed_prompts.py:68`), so it cannot be recomputed from `prompt_versions.content`.
- **Specs.**
  - RULES §3.2 (`:194-213`): raw bytes and `raw_sha256`.
  - §7.1 (`:619-688`): `:641` "checksum проверяется при чтении"; `:642`: review id equals
    `Run.prompt_version_id`.
  - §7.2 `:718-719`: a pinned artifact that is missing or has a different checksum is
    `policy_snapshot_integrity_error`.
  - §9 `:958`: `failed`, no retry.
  - This code is already in place: `policy_errors.py:30`, `run_failures.py:41`, `check_runs.py:62`.

## Acceptance criteria (issue #168, part 3)

| AC | Requirement | Covered by |
| --- | --- | --- |
| 1a | `DiffMap.policy_base_sha` equals the pinned `Run.base_sha` | Task 1: `tests/test_diff_map_pinned_base.py` (retarget, control, publication guard) |
| 1b | A PR base that differs from the pinned one is handled explicitly; test with a retarget | Task 1 (after the diff is stored: pin + reason, D1); Task 2 (before: refusal, D2) |
| 2a | `get_snapshot` re-checks `policy_digest` and the documents' `raw_sha256` | Task 3: `tests/test_policy_snapshot_integrity.py`; Task 4 adds RULES §7.1 `:641` (D3) |
| 2b | A mismatch gives `policy_snapshot_integrity_error`; test with a tampered row | Tasks 3 and 4: `tests/test_policy_snapshot_integrity_postgres.py`, one `UPDATE` per field |

## Decisions

- **D1 — a retarget after the diff is stored.** Decided by the tech lead on 2026-10-25: A, pin
  and record the reason.
  - The map, the merge base and the policy use `Run.base_sha`.
  - The reason goes only into the `context.build` response, never into `incomplete_reasons`
    or the DiffMap digest.
- **D2 — a retarget before the diff is stored.** Decided by the tech lead on 2026-10-25: fix it in
  this PR.
  - The spec settles that the run must refuse:
    - fetching the pinned comparison through `compare/{base}...{head}` is ruled out. CONTEXT
      §9.1 `:1294-1296`: "Compare API … его ограниченный список files не заменяет список PR
      files";
    - re-pinning `Run.base_sha` is ruled out too. The snapshot is already pinned
      (`process_run.py:157`); RULES §7.2 `:714-716` and PIPELINE_SPEC:447 forbid changing the
      pinned revision; TEST_PLAN:86 says "Retry не меняет pins".
  - The spec does not settle the code. Decided by the tech lead on 2026-10-25 (per the
    coordinator's brief): a new terminal code, `pr_base_changed`.
    - State `failed`, class GitHub, no retry.
    - Text: «Целевая ветка PR изменилась после запуска ревью. Перезапустите ревью из UI.»
    - A rerun pins the new base (`rerun_store.py:76`).
    - New codes need no migration (PIPELINE_SPEC:399). It needs one PIPELINE_SPEC §6 row.
    - `running → failed` is an existing transition. A `cancelled` variant would add a
      `running → cancelled` cause the state machine lacks (PIPELINE_SPEC:27).
    - Rejected:
      - `diff_fetch_failed`: retryable, and every attempt fails the same way;
      - `RunCancelled("superseded")`: its text promises a new commit that never comes.
- **D3 — the RULES §7.1 `:641` cross-table check.** Decided by the tech lead on 2026-10-25: in
  this PR, without editing `get_pins`.
  - Placement: `get_snapshot` calls the existing `self.get_pins(run_id)` unchanged and
    compares through a pure function.
  - It compares `service_defaults_rule_version_id`, `defaults_checksum` and the full
    `prompt_bindings`. It never compares `execution_profile`, which is the worker's current
    one, or #280's publication settings.
  - Coverage on `main`: the defaults pin and `review.system` (id, checksum, version). The
    conventions and verifier bindings must stay empty.
  - After #280 the same comparison covers all three prompts with no code change.

## Design

### Fix 1 — the stage pins the PR base (Task 1, D1)

In `ReviewContextStage.run`, right after the head check (`:429-430`):

- If `pull_request.base_sha != run.base_sha`, log a WARNING with `run_id` and both SHAs.
  Keep the live value in `self._pr_base_sha`. Reset it at the start of `run`, the way
  `_diff_old_sha` is reset (`:415`).
- Rebind `pull_request = replace(pull_request, base_sha=run.base_sha)`.
- The `get_merge_base` and `build_diff_map` call lines stay as they are. They now see the
  pinned base:
  - `policy_base_sha == Run.base_sha`;
  - `diff_old_sha` is the pinned merge base.
- `meta.files_changed` stays live. A retargeted PR whose file count differs from the
  pinned-base diff gives:
  - `file_list_complete=false` and `incomplete_reasons=("file_count_mismatch",)`;
  - context `summary_only`/`diff_inventory_incomplete` (`review_context.py:475-476`);
  - no `all_changes_ignored` skip (`:466`).

  #283's rebuild gets the same flag back from the trace (`len(durable) + (not complete)`,
  `pr/283` `run_repository.py:724`), so the two digests match.
- The reason is the key `diff.pr_base_sha` in the `context.build` response,
  written only on a mismatch.
  - Two writers set it: `context_summary` (`:144-165`, new keyword; call at `:499`) and
    `_record_nothing_to_review` (`:515-533`).
  - The PIPELINE_SPEC `context.build` row (`:169`) gets the key.
  - The trace schema is open: `contracts/openapi.yaml` `:914`/`:1327` only names the tool.
    No contract edit.
- No edit to `diff_map.py`, `post_processing_diff_map.py` or `run_repository.py`.

### Fix 2 — refuse a retarget before the fetch (Task 2, D2)

- `FetchVcsReviewInput.execute` (`vcs_diff.py:166-167`) raises
  `RunFailure("pr_base_changed", "pull request base changed after the run pinned it")`
  instead of the `ValueError`. Import `RunFailure` (`:21`). The head `ValueError`
  (`:164-165`) is left as it is.
- It also needs:
  - `run_failures.py`: add the code to `KNOWN_ERROR_CODES` (`:59-76`), but not to
    `RETRYABLE_ERROR_CODES`;
  - `check_runs.py`: the text in `_ERROR_TEXTS` (`:14-70`);
  - `docs/PIPELINE_SPEC.md` §6: a row after `:412`, with `failed`, GitHub, no retry and the text.
- The `vcs.fetch_diff` step records the error. The worker sees a non-retryable code and
  fails the run on the first attempt, with no LLM call.

### Fix 3 — verify the stored chain on read (Task 3)

- `review_ignore.py`: extract `ignore_digest(patterns, *, matcher_version,
  builtin_exclusions_version, limits_version)` from `_policy` (`:190-202`). The three
  literals the row does not store stay inside it.
- `prepare_policy_snapshot.py`: extract `canonical_policy_payload(...)` from
  `build_policy_snapshot` (`:103-124`). It takes every input of the §7.1 formula explicitly,
  with the bindings and the profile as JSON mappings, and returns the canonical JSON text
  that `policy_digest` is the SHA-256 of. Keep line `:145` as is, because #280 inserts right
  after it: it hashes that text, so the helper cannot return the digest itself.
- New module `app/modules/reviews/application/policy_snapshot_integrity.py` (suggested
  name):
  - `verify_policy_document(document, raw)`.
    - Present: `raw` is bytes, `sha256(raw) == raw_sha256`, `len(raw) == byte_count` and
      `sha256(text.encode("utf-8")) == text_sha256`.
    - `presence='missing'` passes when `raw is None`, and nothing else is checked there:
      `PolicyDocument.__post_init__` (`policy_snapshot.py:53-65`) and the DB CHECK already
      force the other fields to `None`. The digest covers presence and the `null`
      checksums.
  - `verify_policy_snapshot(snapshot, *, prompt_bindings, execution_profile)`.
    - Recompute `ignore_digest` from the stored patterns and versions.
    - Recompute `policy_digest` from the stored fields and the stored JSON.
    - Compare both with the stored values.
  - A mismatch raises `PolicyError(SNAPSHOT_INTEGRITY_ERROR, …)` that names the checksum and
    never quotes content (`policy_errors.py:61-67`).
- Adapter: `get_staged_documents` verifies each row. `get_snapshot` verifies the chain
  before `return`, with `row.prompt_bindings` and `row.execution_profile`.
  `create_snapshot` re-reads through `get_snapshot` (`:230`).
- The failure path is wired already. `PreparePolicySnapshot.execute` (`:163`) raises; the
  `policy.load` step records it (`review_context_stage.py:396-399`); the run ends
  `failed/policy_snapshot_integrity_error`.

### Fix 4 — the cross-table pins check (Task 4, D3)

- In `policy_snapshot_integrity.py`, add `verify_snapshot_pins(snapshot, pins)`. It checks:
  - `pins` is not `None`; `None` means a pinned version row is missing (§7.2 `:718`);
  - `snapshot.service_defaults_rule_version_id == pins.service_defaults_rule_version_id`,
    which is `Run.rule_version_id`;
  - `snapshot.defaults_checksum == pins.defaults_checksum`, which is
    `rule_versions.checksum`;
  - `snapshot.prompt_bindings == pins.prompt_bindings`, field by field: ids, checksums,
    keys and versions.
- `get_snapshot` calls `await self.get_pins(run_id)` after Fix 3's check. It does not edit
  `get_pins`.
- No check recomputes `rule_versions.checksum` from `rules` JSONB or a prompt checksum from
  its content. The first risks JSONB number round-trips, and the second hashes the source
  file. Both are named in the PR body.

## Tests

All tests go into new files. `tests/test_review_context_stage.py` (#280, #220) and
`tests/test_policy_snapshot_postgres.py` (#280) are imported read-only.

**`tests/test_diff_map_pinned_base.py` (Tasks 1 and 2).**

- Task 1 setup:
  - `Retargeted(Vcs)` subclasses the fake (`tests/test_review_context_stage.py:94-131`).
    Its live base is `"e" * 40`, so it overrides `get_pull_request` (the fake hard-codes
    `_BASE` at `:116`). It records the base passed to `get_merge_base`.
  - A wrapper around `BuildReviewContext` captures `diff_map`. Build the stage like
    `_stage` does (`:215-245`); the default diff has 2 files (`:195`).
- Task 1 asserts, with literal values:
  - `policy_base_sha == "b" * 40`, and the merge base is requested with `"b" * 40`;
  - the trace `context.build` → `diff.pr_base_sha == "e" * 40`;
  - with `files_changed=2`: `file_list_complete is True` and `incomplete_reasons == ()`;
  - with `files_changed=5`: `False` and `("file_count_mismatch",)`;
  - `pr_base_sha` appears nowhere in the DiffMap. The digest equals the digest of a
    non-retargeted run with the same counts;
  - a rebuild in #283's form (`Run.base_sha`, traced `diff_old_sha`,
    `files_changed = 2 + (not complete)`) gives the stage digest;
  - a control run without a retarget has no `pr_base_sha`.
- Task 1 red at `4faa03dd`: `policy_base_sha` and the merge-base request are `"e" * 40`.
- Publication guard, green on arrival: `diff_map_from_snapshots(base_sha="b" * 40, …)` gives
  `policy_base_sha == "b" * 40`.
- Task 2:
  - `FetchVcsReviewInput.execute` with a provider whose base differs raises `RunFailure`
    with the D2 code, and `get_diff` is never called;
  - the code is in `KNOWN_ERROR_CODES` and not in `RETRYABLE_ERROR_CODES`;
  - `failure_reason(code)` equals the literal text.
  - Red at `4faa03dd`: a `ValueError`.

**`tests/test_policy_snapshot_integrity.py` (Tasks 3 and 4, unit).**

- Task 3: build the snapshot from a CRLF raw document plus a `missing` `AGENTS.md`.
  - The untouched snapshot passes; the missing document passes with `raw=None`.
  - Forward compatibility: an extra key in the stored profile JSON passes when the digest
    covered it.
  - Each tamper raises `policy_snapshot_integrity_error`: raw bytes, text, `raw_sha256`
    alone, `ignore_patterns`, `policy_base_sha`, `defaults_checksum`, one binding key,
    `policy_digest`.
  - Golden pin: `canonical_policy_payload(...)` over fixed inputs equals canonical JSON
    written literally in the test, and a built snapshot's `policy_digest` is its SHA-256.
- Task 4: `verify_snapshot_pins` passes for pins equal to the snapshot. It raises for
  `None`, another `defaults_checksum`, another rule version id, and another review checksum
  or id.

**`tests/test_policy_snapshot_integrity_postgres.py` (Tasks 3 and 4, PG).**

- Use the fixtures of `tests/test_policy_snapshot_postgres.py:45-59`. Store a snapshot
  through the real UoW, then tamper with one raw SQL `UPDATE` per test, keeping the presence
  CHECK valid.
- Task 3 tampers: `raw_bytes`, `text`, `raw_sha256`, `ignore_patterns = '[]'`,
  `policy_base_sha`.
- Task 4 tampers:
  - `rule_versions.checksum` of the pinned rule;
  - `prompt_versions.checksum` of the pinned prompt;
  - `runs.rule_version_id` pointing to another existing rule version.
- Each tamper makes `get_snapshot` raise `policy_snapshot_integrity_error`. One end-to-end
  case runs `PreparePolicySnapshot.execute`.
- The existing round trip and rival check (`tests/test_policy_snapshot_postgres.py:63-148`)
  stay green.
- Existing PG tests stay green under Task 4: every one that stores a snapshot through the SQL
  store takes its pins from `get_pins` (`test_policy_snapshot_postgres.py:81`,
  `test_review_prompt_pins_postgres.py:108`, `test_run_policy_snapshot_origin_postgres.py:53`).

## Coordination

The coordinator posts these comments before the edits. "Ours" = what this PR changes.

| PR | File: ours | Their change in the same file | Overlap |
| --- | --- | --- | --- |
| #283 `8d27fb53` | `review_context_stage.py`: `__init__` (new attribute near `:343`), `run` (reset near `:415`, new lines after `:430`), `context_summary` (`:144-165`), `_record_nothing_to_review` (`:515-533`) | `build_diff_map` args (`snapshot_vcs_file`), `_service_excluded_names`, the `context.build` try block (`:487-510`), the `_record_nothing_to_review` signature and response | adjacent lines; their coverage restore needs Fix 1 |
| #283 | `vcs_diff.py`: import `:21`, `FetchVcsReviewInput.execute` `:166-167` | `GIT_EMPTY_BLOB_SHA`, `VcsFile.git_mode`, `OmissionReason.EMPTY_FILE`, `_known_omission` tail | none |
| #283 | `check_runs.py`: one `_ERROR_TEXTS` entry | `CheckRunReport` fields, `check_run_view` | none |
| #283 | `docs/PIPELINE_SPEC.md`: `:169` row, new §6 row after `:412` | `:58`, `:106-182`, `:435`, `:564-608` | `:169` sits inside their range |
| #283 | `tests/test_diff_map_pinned_base.py` (new): imports `vcs_file_from_snapshot` from `review_context_stage` for the rebuild test | replaces that function with `run_coverage.snapshot_vcs_file` | whoever merges second fixes the import |
| #220 `b26076eb` | `review_context_stage.py`: as for #283 | the import block, the `provider=` line, the `except` tuple | adjacent |
| #220 | `vcs_diff.py`: import `:21`, `:166-167` | rewrites the same import line; `PullRequestLocator`, `VcsFile`, the blob `except` in `execute` | import line conflicts |
| #220 | `run_failures.py`: one `KNOWN_ERROR_CODES` entry (`:59-76`) | `Literal` import, the `RequiredSourceUnavailable` class | none |
| #220 | `check_runs.py`: one `_ERROR_TEXTS` entry | `CheckRunReport.provider` | none |
| #220 | `tests/test_vcs_diff.py`: `test_same_head_with_changed_base_is_rejected_before_file_fetch` (`:407`, `ValueError` → `RunFailure("pr_base_changed")`, import inside the function; addendum 6398058459) | the import block (`:4-12`), new tests after `:450` | none |
| #280 `5723fee6` | `policy_snapshot_unit_of_work.py`: `get_staged_documents` (`:131-137`), `get_snapshot` (`:165-200`; checks before `return`; calls `get_pins` unchanged) | `get_pins` body (`:92-129`), two fields at the end of the `get_snapshot` constructor, `create_snapshot` values | same function, other lines; D3 picks up their bindings |
| #280 | `prepare_policy_snapshot.py`: `build_policy_snapshot` `:103-124` | import, `RunPolicyPins` fields, two fields after `:145` | `:145` kept as is |
| #227 `79dc593b` | `docs/PIPELINE_SPEC.md`: `:169` and a row after `:412` | `:170`, `:415-419` | adjacent `:169`/`:170` |

No open PR touches `review_ignore.py`, `policy_errors.py`, `diff_map.py`,
`post_processing_diff_map.py` or the four new files.

## Out of scope

- #168 parts 1, 2 and 4–6; the #130 integration; T3 (the findings line check).
- The head `ValueError` in `FetchVcsReviewInput`; a new head is superseded by the webhook.
- Recomputing `rule_versions`/`prompt_versions` checksums from their content (Fix 4).
- A stored DiffMap aggregate (`diff_snapshot_id`, CONTEXT §2.2).
- Docs beyond the two PIPELINE_SPEC rows. Part 4 owns the doc pass.

## Verification

```
uv run ruff check .
uv run ruff format --check .
uv run mypy . --exclude '^docs/reports/'
TEST_DATABASE_URL=postgresql+psycopg://app:app@localhost:5432/imp168d \
TEST_RABBITMQ_URL=amqp://app:app@localhost:5672/imp168d uv run pytest
```

- Run `df -h /tmp` first. The full suite takes about 40 min, so run it in the background.
  The 7 `tests/test_deploy_monitoring.py` failures come from the local `sh` wrapper (part 1).
- Focused PG run with `-rs`, zero skips:
  - `tests/test_policy_snapshot_postgres.py`;
  - `tests/test_policy_snapshot_integrity_postgres.py`;
  - `tests/test_run_policy_snapshot_origin_postgres.py`;
  - `tests/test_review_prompt_pins_postgres.py`;
  - `tests/test_rules_md_e2e_postgres.py`.
- Every task brief says: never read any `.env`. A clean `code-reviewer` reviews with
  `git show`/`git diff` only.
- PR:
  - title `fix(reviews): pin the DiffMap base and verify snapshots on read (#168)`;
  - body What / Why / How to verify / `Refs #168`, with no closing keyword and no new
    dependency;
  - the body names the D2 code and the Fix 4 limits;
  - reviewer ndovnar; rebase on `main` before review and before merge.
