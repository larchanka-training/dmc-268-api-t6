# Task list: pinned DiffMap base and snapshot integrity on read (#168, part 3)

See [168-snapshot-base-integrity-plan.md](168-snapshot-base-integrity-plan.md) for the
design. The plan is the source of truth, and this list adds no scope. AC 1a–2b and D1–D3
are named there.

- Baseline: `origin/main` `4faa03dd`.
- Branch: `fix/168-snapshot-base-integrity`. One PR from `main` with Tasks 1–4.
- Whoever merges second rebases. If #280 lands first, we rebase.
- Each task is one Conventional commit, and its subject ends with `(#168)`.

**Process.**

- Each task that writes code goes to a subagent through a brief file. Every brief says:
  never read `.env` of any clone.
- Each task is test-first:
  - write the named tests and run them;
  - record the red output in the task report;
  - write the fix;
  - commit the test and the fix together. No commit carries a failing test.
- Post the comments from the plan's Coordination table before the first edit of a shared
  file.

Order: Task 0 → 1 → 2 → 3 → 4. Tasks 1 and 2 share a test file. Task 4 builds on Task 3's
module and its `get_snapshot` hook.

**Common verification**, before every commit:

```
uv run ruff check .
uv run ruff format --check .
uv run mypy . --exclude '^docs/reports/'
TEST_DATABASE_URL=postgresql+psycopg://app:app@localhost:5432/imp168d \
TEST_RABBITMQ_URL=amqp://app:app@localhost:5672/imp168d uv run pytest
```

- Run `df -h /tmp` first.
- The full run takes about 40 min, so run it in the background.
- Add the focused PG run from the plan with `-rs`: zero skips.

## Task 0: Plan documents

**Commit:** `docs(plans): plan the pinned diff base and snapshot checks (#168)`

**Files:** `docs/plans/168-snapshot-base-integrity-plan.md`,
`docs/plans/168-snapshot-base-integrity-todo.md`.

**Done when:**

- [ ] the plan records D1–D3 as decided on 2026-10-25, with the D2 code `pr_base_changed`;
- [ ] only the two files are committed, staged with `git add -f`.

**Dependencies:** none.

## Task 1: The stage builds the DiffMap on the pinned base (D1)

**Commit:** `fix(reviews): build the diff map on the pinned run base (#168)`

**Files:**

- `app/modules/reviews/application/review_context_stage.py`;
- `docs/PIPELINE_SPEC.md` (the `context.build` row, `:169`);
- `tests/test_diff_map_pinned_base.py` (new; suggested name).

**What to do:**

- Write the Task 1 tests from the plan's "Tests" section:
  - the retarget case, with both file counts;
  - the reason only in the trace, and the digest unaffected;
  - the rebuild in #283's form;
  - the control run and the publication guard.
- Run them on `4faa03dd`. The regression must fail at `"e" * 40`. Record it.
- In `run`, after `:429-430`:
  - keep the live base when it differs and log a WARNING;
  - rebind `pull_request` to `replace(..., base_sha=run.base_sha)`;
  - write `diff.pr_base_sha` in both `context.build` writers, only on a mismatch;
  - add the key to the PIPELINE_SPEC row.
- Leave the `get_merge_base` and `build_diff_map` lines unchanged. Do not edit
  `tests/test_review_context_stage.py`.

**Done when:**

- [ ] on a retarget, `policy_base_sha` and the merge-base request use `Run.base_sha`;
- [ ] `file_list_complete` and `incomplete_reasons` are asserted literally for both counts;
- [ ] the reason is never in the DiffMap;
- [ ] the full suite is green.

**Dependencies:** Task 0.

## Task 2: Refuse a retarget before the diff is stored (D2)

**Commit:** `fix(reviews): fail a run whose PR base changed before the diff (#168)`

**Files:**

- `app/modules/reviews/application/vcs_diff.py` (`FetchVcsReviewInput.execute`
  `:166-167`, import `:21`);
- `app/modules/reviews/application/run_failures.py` (`KNOWN_ERROR_CODES`);
- `app/modules/reviews/application/check_runs.py` (`_ERROR_TEXTS`);
- `docs/PIPELINE_SPEC.md` (a §6 row after `:412`);
- `tests/test_diff_map_pinned_base.py`;
- `tests/test_vcs_diff.py` (the base-drift test `:407` expects the new code; import inside
  the function).

**What to do:**

- Write the Task 2 tests:
  - the code is raised, and `get_diff` is never called;
  - the code is known and not retryable;
  - the author text is checked literally.
- Run them. At `4faa03dd` they fail with a `ValueError`. Record it.
- Replace the `ValueError` with `RunFailure("pr_base_changed", …)` and add the catalog entries.
- Leave the head `ValueError` (`:164-165`) as it is.

**Done when:**

- [ ] a base mismatch at fetch time fails the run on the first attempt, with the confirmed
  code and text;
- [ ] the full suite is green.

**Dependencies:** Task 0.

## Task 3: Verify the stored snapshot chain on read

**Commit:** `fix(reviews): verify policy snapshot checksums on read (#168)`

**Files:**

- `app/modules/reviews/application/review_ignore.py` (extract `ignore_digest`);
- `app/modules/reviews/application/prepare_policy_snapshot.py` (extract `policy_digest`;
  keep `:145`);
- `app/modules/reviews/application/policy_snapshot_integrity.py` (new; suggested name);
- `app/modules/reviews/infrastructure/policy_snapshot_unit_of_work.py`
  (`get_staged_documents`, `get_snapshot`);
- `tests/test_policy_snapshot_integrity.py` (new);
- `tests/test_policy_snapshot_integrity_postgres.py` (new).

**What to do:**

- Write the Task 3 unit and PG tests:
  - the golden formula pin;
  - the missing-document case;
  - forward compatibility of the stored JSON;
  - one tamper per field.
- Run them. The tamper tests fail because nothing raises today. Record it.
- Extract the two digest helpers. Behaviour stays the same: the golden pin and
  `tests/test_prepare_policy_snapshot.py:240-276` stay green.
- Add the verifier. It recomputes from the stored row values and the stored JSON, never from
  module constants.
- Call it from `get_staged_documents` (each document) and from `get_snapshot` (the chain).

**Done when:**

- [ ] every tamper gives `policy_snapshot_integrity_error`, both in the unit tests and on PG;
- [ ] a `missing` document passes;
- [ ] untouched and rival snapshots still load (`tests/test_policy_snapshot_postgres.py:132-141`);
- [ ] the full suite and the focused PG run are green, with zero skips.

**Dependencies:** Task 0.

## Task 4: Cross-table check of the pinned versions (D3)

**Commit:** `fix(reviews): check snapshot pins against the run versions on read (#168)`

**Files:**

- `app/modules/reviews/application/policy_snapshot_integrity.py`;
- `app/modules/reviews/infrastructure/policy_snapshot_unit_of_work.py` (`get_snapshot` only;
  `get_pins` is called, never edited);
- `tests/test_policy_snapshot_integrity.py`;
- `tests/test_policy_snapshot_integrity_postgres.py`.

**What to do:**

- Write the Task 4 tests:
  - unit: `None` pins, a different defaults checksum, a different rule version id, and a
    different review checksum or id;
  - PG: tamper `rule_versions.checksum`, `prompt_versions.checksum` and
    `runs.rule_version_id`.
- Run them, and record the red output.
- Add `verify_snapshot_pins(snapshot, pins)`. It compares the defaults id and checksum, and
  the bindings field by field. It does not compare the execution profile or the publication
  settings.
- Call it in `get_snapshot` after Task 3's check, using `await self.get_pins(run_id)`.

**Done when:**

- [ ] each tamper gives `policy_snapshot_integrity_error`;
- [ ] every existing PG test that stores a snapshot stays green;
- [ ] the full suite is green.

**Dependencies:** Task 3.
