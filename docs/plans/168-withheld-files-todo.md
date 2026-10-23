# Task list: withheld removed files stay out of the prompts (#168, part 1)

See [168-withheld-files-plan.md](168-withheld-files-plan.md) for the design. The plan is the
source of truth and this list adds no scope. AC 1a–2c and D1–D3 are named there.

- Baseline: `origin/main` `22c621ab`.
- Branch: `fix/168-withheld-removed-files`, one PR.
- Each task is one Conventional commit whose subject ends with `(#168)`.

**Process.**

- Each task that writes code is delegated to a subagent through a brief file. Every brief
  says: never read `.env` of any clone.
- Each task is test-first. Write the named tests, run them, and record the red output in
  the task report. Then write the fix, and commit the test and the fix together. No commit
  carries a failing test.

Order: Task 0 → 1 → 2 → (after #229 merges, D2) 3.

**Common verification**, before every commit:

```
uv run ruff check .
uv run ruff format --check .
uv run mypy . --exclude '^docs/reports/'
TEST_DATABASE_URL=postgresql+psycopg://app:app@localhost:5432/imp168b \
TEST_RABBITMQ_URL=amqp://app:app@localhost:5672/imp168b uv run pytest
```

- Run `df -h /tmp` first.
- The full run takes over 15 min, so run it in the background.
- Add the focused PG run with `-rs`: zero skips.

## Task 0: Plan documents

**Commit:** `docs(plans): plan the withheld removed file fix (#168)`

**Files:** `docs/plans/168-withheld-files-plan.md`, `docs/plans/168-withheld-files-todo.md`.

**Done when:**

- [ ] D1–D3 are approved by the tech lead, and the plan records the answers;
- [ ] only the two files are committed. `docs/plans/` is listed in the shared clone's local
  `.git/info/exclude:14`, while the tracked plans stay in the repo, so stage these two files
  with `git add -f`.

**Dependencies:** none.

## Task 1: The projection reads only `review_patch`

**Commit:** `fix(reviews): never re-parse a withheld snapshot for the model (#168)`

**Files:**

- `app/modules/reviews/application/get_run_diff.py`;
- `tests/test_withheld_files_in_prompts.py` (new, suggested name);
- `tests/test_get_run_diff.py`;
- `tests/test_vcs_snapshot_postgres.py`.

**What to do:**

- Write the regression and the unit tests from the plan, section "Tests".
  - The regression includes the allowed `src/app.py` as a positive control.
  - It writes the snapshots through the real `StoreDiffSnapshot` and the real stage.
  - It asserts on the rendered review and conventions prompts.
- Run them on `22c621ab`. The regression must fail on `q b/r.py` / `QBR_SECRET`. Record it.
- In `review_files_from_snapshots` (`get_run_diff.py:151-168`):
  - `review_patch is None` → `omitted`;
  - delete the re-parse (`:166-167`);
  - remove the unused `parse_unified_diff` import (`:13`).
- Per D1, update the legacy expectation at `tests/test_vcs_snapshot_postgres.py:375-382`:
  `src/first.py` is omitted.
- Edit no other app file (plan, "Fix 1").

**Done when:**

- [ ] the regression is red before the fix and green after it, with no parser change;
- [ ] in both prompts: zero mentions of `gone.py` / `q b/r.py` and zero content lines of
  either file; `src/app.py` is present;
- [ ] the full suite and the focused PG run are green, with zero skips.

**Dependencies:** Task 0.

## Task 2: Exact diff-header and hunk-line parsing

**Commit:** `fix(reviews): parse diff headers and hunk lines exactly (#168)`

**Files:** `app/modules/reviews/application/prompt_builder.py` (`:534-601`),
`tests/test_prompt_builder.py`.

**What to do:**

- Add parser cases 1–6 from the plan, section "Tests". Cases 1 and 3–5 are red first.
- Change the line split to `split_lf` from `unified_diff.py`, plus a trailing-CR strip.
- Honour `new file mode`, `deleted file mode` and `rename to` only while `not in_hunk`.
- Change `_path_from_diff_header` to the symmetric split, keeping the provisional fallback.

**Done when:**

- [ ] all six cases pass, and the existing parse tests (`:109-170`) still pass;
- [ ] `tests/test_llm_gateway.py` is green: eval inputs are unchanged, per the plan's scan;
- [ ] the gates are green.

**Dependencies:** Task 1.

## Task 3: Findings guard after #229 (D2)

**Commit:** `test(reviews): pin stored names in the findings diff map (#168)`

**Precondition:** #229 is merged. Rebase the branch on `main` first.

**Files:** `tests/test_post_processing_diff_map.py` (new).

**What to do:**

- Write the guard test from the plan: `diff_map_from_snapshots` over the normalized
  `src/q b/r.py` row with `+y rename to evil.py`.
- Assert the stored path, the unshifted anchors, and that `resolve_anchor` resolves.
- The test is green on arrival. Say so in the PR body.

**Done when:**

- [ ] the test passes on `main` with #229, and the gates are green.

**Dependencies:** Task 2, and #229 merged. If D2 = B, drop this task and add the PR-body
note instead.

## Checkpoint and publication

- [ ] Coordination comments are posted: #230 (`prompt_builder.py`) and #229 (Task 3 timing).
- [ ] A clean `code-reviewer` reviews the branch with `git show` / `git diff` only. Its
  advice is checked against `.agents/rules/backend.md`.
- [ ] The PR is opened: title `fix(reviews): keep withheld files out of review prompts
  (#168)`; body What / Why / How to verify / `Refs #168`, no closing keyword; deviation D1,
  and D2 if B; reviewer ndovnar.
- [ ] The branch is rebased on `main` before review and before merge, and the import of the
  `tests/test_review_context_stage.py` helpers is re-checked after #220 / #230.
- [ ] After merge, part 5 runs the staging check with a removed excluded file. That needs
  the user's explicit OK.
