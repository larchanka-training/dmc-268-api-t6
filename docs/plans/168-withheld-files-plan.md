# Implementation plan: withheld removed files stay out of the prompts (#168, part 1)

## Status and baseline

Draft by the planning step of the agent-loop, 2026-10-23. The tech lead decided D1–D3 below
on 2026-10-23.
Issue: https://github.com/larchanka-training/dmc-268-api-t6/issues/168, part 1 (P1, AC5 of
#130). Baseline: `origin/main` `22c621ab`. Worktree `projects/dmc-268-api-t6-168b`, branch
`fix/168-withheld-removed-files`. Test database and RabbitMQ vhost: `imp168b`. Task list:
[168-withheld-files-todo.md](168-withheld-files-todo.md).

Every `file:line` below was checked against `22c621ab`. Where the issue cites `7546e53`, the
offset at that revision is given in brackets.

## Context

**Root cause: a withheld snapshot is parsed again from its UI patch.**

- `StoreDiffSnapshot.execute` (`app/modules/reviews/application/get_run_diff.py:63-93`)
  keeps the UI `patch` of an excluded file and sets `review_patch=None` (`:86-87`).
- `review_files_from_snapshots` (`get_run_diff.py:151-168`) reads such a row as "no
  prepared patch". When `blob_sha` is set, the row goes to `omitted` (`:164-165`). When
  `blob_sha` is None, as for a removed file, it calls `parse_unified_diff(snapshot.patch)`
  (`:166-167`), so the content of a withheld file becomes model input again. This breaks
  RULES §5.1 (`docs/RULES_FORMAT_SPEC.md:416-418`): "`review_patch` содержит только
  допустимый вход модели".
- The projection has two consumers:
  - the review prompt, through `review_prompt_repository.py:55-71` → `ExecuteReviewRun`;
  - the conventions `changed_files`, through `process_run.py:239`.

**Why the late filters do not catch it.** `execute_review.py:134-138` [`:107-108`] and
`process_run.py:244-246` drop a path the stage excludes. That only works when the path is
right. `_path_from_diff_header` (`prompt_builder.py:596-601` [`:333-338`]) cuts at the
first `" b/"`:

- `diff --git a/generated/q b/r.py b/generated/q b/r.py` becomes `r.py b/generated/q b/r.py`;
- `generated/**` does not match that string;
- the path and the removed lines reach the review prompt, and the path reaches the
  conventions `<changed_files>`.

`generated/gone.py` is re-parsed too. Its parsed path is correct, so the late filter drops
it. Reproduced at `22c621ab`.

**Second vector, reproduced here (the issue marked it "not reproduced").**
`parse_unified_diff` (`prompt_builder.py:534-593`) has two defects:

- It splits with `str.splitlines()` (`:550` [`:287`]), so U+2028, FF, VT, a lone CR and
  others inside a content line start a new line. For `+a  b` the parser yields an
  extra context line `b` and shifts every later number by one. It also yields a bare line,
  so `+x rename to evil.py` sets the path to `evil.py`.
- It honours `new file mode`, `deleted file mode` and `rename to` (`:560-569`) also inside
  a hunk. A bare `rename to evil.py` line after `@@` renames the file. A bare
  `deleted file mode` line flips the status to `removed`.

**The findings line check.**

- At `22c621ab` it goes `run_repository.py:638-678` → `_new_hunk_lines` (`:889-903`) →
  `parse_unified_diff` (`:897`). `_new_hunk_lines` keys by `CodeChangeDiff.filename`
  (`:668`) and ignores the parsed path, so only the line-splitting defect reaches it: the
  numbers shift.
- PR #229 (`origin/fix/171-publication-diffmap`, `dd851b1`) replaces that path:
  `load_publication_diff_map` (its `run_repository.py:635-684`) →
  `diff_map_from_snapshots` (`post_processing_diff_map.py`) → `_vcs_file` /
  `_provider_patch` (`review_context_stage.py:64-105`) → `build_diff_map`, which uses
  `split_lf` + `parse_hunks` (its `diff_map.py:334-342`).
- After #229, the findings path takes the path from `CodeChangeDiff.filename` and parses no
  `diff --git` header. It does not call `parse_unified_diff`.

**Where the fallback came from.** Migration `20260928_0016` added `review_patch` with no
backfill (`alembic/versions/20260928_0016_rekey_review_blobs.py:61`). It marked only the
generated and binary legacy rows (`:74-92`). The re-parse branch is the read path for those
pre-0016 rows, pinned by `tests/test_vcs_snapshot_postgres.py:367-382` (`src/first.py` is
re-parsed into `changed`). VCS snapshots written after 0016 always carry `review_patch`
when they are allowed and have hunks (`get_run_diff.py:96-118`). So today the branch fires
only for:

- withheld rows;
- `diff --git` patches without a hunk;
- pre-0016 rows.

## Acceptance criteria (issue #168, part 1)

| AC | Requirement | Covered by |
| --- | --- | --- |
| 1a | A file excluded by a mask or a built-in rule never reaches `changed_files` / `omitted_files` of the review and conventions prompts, in any status, including `removed` without `blob_sha` | Task 1: regression `tests/test_withheld_files_in_prompts.py` (both prompts, rendered text) |
| 1b | A withheld snapshot is not re-parsed from the UI patch: `review_patch` is the only model input (RULES §5.1) | Task 1: fix in `get_run_diff.py:151-168`; unit test in `tests/test_get_run_diff.py` |
| 1c | Regression on the real `ReviewContextStage`: with mask `generated/**`, removed `generated/gone.py` and `generated/q b/r.py` give 0 content lines and 0 name mentions in both prompts; red on `7546e53` | Task 1 regression; red shown on `22c621ab` before the fix; `7546e53` per D3 |
| 2a | The `diff --git` path is parsed without cutting at the first `" b/"` | Task 2: `tests/test_prompt_builder.py` |
| 2b | A `rename to` line inside a hunk does not change the path | Task 2: bare line and U+2028 vector; the sweep adds `deleted file mode` / `new file mode` |
| 2c | The fix holds for the findings line check (`run_repository` → `parse_unified_diff`) | At `22c621ab`: by the shared parser (Task 2). After #229: no header parsing remains; guard test in Task 3 (D2) |

## Decisions

- **D1 — legacy pre-0016 rows.** Decided by the tech lead on 2026-10-23: A, drop the fallback.
  - **A (recommended).** Drop the fallback. A pre-0016 row, or a hunkless `diff --git`
    patch, projects as `omitted` and is no longer re-parsed. The model sees less, never
    more. A rerun builds a fresh snapshot (SD:1269-1270).
    `tests/test_vcs_snapshot_postgres.py:375-382` changes its expectation to "`src/first.py`
    is omitted". The full-suite probe shows this is the only existing test that depends on
    the fallback. This is stated as a deviation in the PR body.
  - **B.** Keep a compatibility path. The projection cannot tell a withheld row from a
    legacy row: no column marks it. A backfill migration matching `review_patch IS NULL AND
    blob_sha IS NULL` would also match withheld rows and reopen the hole. Not recommended.
- **D2 — the findings test and #229.** Decided by the tech lead on 2026-10-23: A, Task 3
  waits for #229.
  - **A (recommended).** #229 is approved and merges first. Rebase on `main` and add
    Task 3.
  - **B.** Ship without Task 3. The PR body then states that `_new_hunk_lines` gets the
    line-splitting fix through the shared parser and uses no parsed path.
- **D3 — "red on `7546e53`".** Decided by the tech lead on 2026-10-23: by code identity, no run.
  - Between `7546e53` and `22c621ab`, these are unchanged: `get_run_diff.py`,
    `process_run.py`, `conventions.py`, and the parser functions in `prompt_builder.py`
    (only shifted by 263 lines).
  - The late filter is the same at `execute_review.py:107-108`.
  - The regression is run red on `22c621ab`.
  - Running it at `7546e53` would need a second worktree and the test helpers 194 commits
    back. If the tech lead wants the run anyway, it is a one-off: `git archive 7546e53` into
    the scratchpad. It is not a commit.

## Design

### Fix 1 — the projection reads only `review_patch` (Task 1)

- In `review_files_from_snapshots` (`get_run_diff.py:151-168`):
  - a snapshot with `review_patch is None` goes to `omitted` under its stored `filename`.
    This joins the `blob_sha` branch (`:164-165`) and replaces the re-parse (`:166-167`);
  - the other branches stay.
- Remove the now unused `parse_unified_diff` import (`get_run_diff.py:13`).
- After the fix, a withheld file appears only as its true name in `omitted`. That name is
  the one `StoreDiffSnapshot` withheld through
  `excludes_change(filename, previous_filename)` (`process_run.py:158-159`).
- The late filters then drop it:
  - `apply_ignore` puts the new path of every withheld file in the deny-set
    (`review_ignore.py:322-346`);
  - `ReviewContextStage.excluded` checks that deny-set first
    (`review_context_stage.py:240-251`).
- The worker passes `context_filter=context_stage` whenever the stage exists
  (`app/worker.py:275-280`).
- The conventions `changed_files` come from `changed` only (`process_run.py:239`), so they
  never list the file.
- `repo_tree` / `repo_files` are filtered by `PolicyFilteredConventionsSource`
  (`conventions.py:145-168`, wired at `app/worker.py:249-252`).
- No edit to `process_run.py`, `execute_review.py`, `review_prompt_repository.py`,
  `review_context_stage.py` or `conventions.py`. If one turns out to be needed, stop and
  return to planning.

### Fix 2 — the unified-diff parser (Task 2)

All changes are in `prompt_builder.py:534-601`:

- **Line split.** Iterate `line.removesuffix("\r") for line in split_lf(diff)`.
  - `split_lf` is in `app/modules/reviews/application/unified_diff.py:45-52`, a module with
    no app imports. Do not import from `vcs_diff`: it imports `prompt_builder`, so that
    would be a cycle.
  - The trailing-CR strip keeps the current CRLF result. It mirrors
    `vcs_diff._patch_lines` (`vcs_diff.py:267-269`).
- **Extended header.** `new file mode`, `deleted file mode` and `rename to` are honoured
  only while `not in_hunk`. A new `diff --git` line already resets `in_hunk`.
- **Header path.** `_path_from_diff_header` splits `a/<P> b/<P>` symmetrically (derived):
  - let `rest` be the text after `diff --git `, and `p = (len(rest) - 5) // 2`;
  - accept the split when `rest` starts with `a/`, `rest[2 + p : 5 + p] == " b/"` and the
    two halves are equal.
  - Otherwise (a rename or copy with different names) keep today's first-`" b/"` result as
    a provisional path. The pre-hunk `rename to` line overrides it, as today.
  - No `" b/"` at all still raises `ValueError`. Quoted headers stay out of scope: they are
    the provider's responsibility (docstring at `:597`).
- **Eval inputs.** A scan of the 25 eval diffs (`test-prs-dataset/cases/*/diff.patch`,
  `review/examples/sample.diff`) found none affected:
  - no header with two `" b/"`;
  - no in-line separator;
  - no extended-header keyword inside a hunk.

  So eval prompts and digests do not change, and no eval re-run is needed.

## Tests

**Regression `tests/test_withheld_files_in_prompts.py` (Task 1, new file).** The file
name is suggested. A new file avoids #220's append at the end of
`tests/test_review_context_stage.py` (`:898+`) and #230's edits there. It may import that
file's fakes (`Vcs`, `Policy`, `Repository`, `Parser`, `Trace`, `_checkpoint`; `:62-172`)
and `tests.test_prepare_policy_snapshot.Store/Uow`, read-only.

- **Real chain.** The real `ReviewContextStage`, built as `_stage` does (`:199-230`), with
  the default policy `generated/**` (`:122-124`). The real `ReviewRunProcessor`, VCS path,
  writes the snapshots through the real `StoreDiffSnapshot` with
  `excluded=stage.excludes_change`. Never hand-set `review_patch=None`.
- **One shared fake** serves the processor's repository and the stage's `runs`:
  `get_run_snapshots` returns None before the store and the stored rows after it.
- **Changed files from the fake provider:**
  - `src/app.py` modified, with a blob. This is the positive control.
  - `generated/gone.py` removed, `blob_sha=None`, removed line `GONE_SECRET = 1`.
  - `generated/q b/r.py` removed, `blob_sha=None`, removed line `QBR_SECRET = 2`.

  Without the allowed file, `stage.run` raises `RunSkipped("all_changes_ignored")` and no
  prompt is built. The test would then pass vacuously.
- **Review prompt.** `ExecuteReviewRun(processor, prompts, model, publisher,
  context_filter=stage)`. The prompts fake projects the stored rows exactly as
  `review_prompt_repository.py:55-71` does. Capture the `ReviewContext` and render it with
  `PromptBuilder().build(context)`.
- **Conventions prompt.** The real `GenerateRepoConventions` over
  `PolicyFilteredConventionsSource(source, stage.excluded)`.
  - Its tree holds all three paths: the base still has the removed files.
  - The fakes can follow `tests/test_repo_conventions.py:101-215`.
  - Render the captured request with `build_conventions_prompt(request).user`
    (`conventions_prompt.py:18-57`).
- **Assertions, on both rendered texts:**
  - none of `gone.py`, `q b/r.py`, `GONE_SECRET`, `QBR_SECRET` appears;
  - `src/app.py` and its added line do appear;
  - `context.changed_files` lists only `src/app.py`, and `context.omitted_files == ()`;
  - the conventions request has `changed_files == ("src/app.py",)`.
- **Red on `22c621ab`.** `r.py b/generated/q b/r.py` with `QBR_SECRET` appears in the review
  prompt and in the conventions `<changed_files>`. Record the failing assertion in the task
  report.
- **Green after Fix 1 alone,** before any parser change. That order shows that the root
  cause is the re-parse.

**Unit test, `tests/test_get_run_diff.py` (Task 1).** `StoreDiffSnapshot` with an
`excluded` predicate, over the two removed `generated/` files without `blob_sha`. Then
`review_files_from_snapshots` gives `changed == ()`, and both names, stored exactly, in
`omitted`. A second test covers an allowed hunkless `diff --git` patch without `blob_sha`:
it goes to `omitted` (D1).

**Legacy pin (Task 1, per D1).** `tests/test_vcs_snapshot_postgres.py:375-382`: `src/first.py`
moves from `changed` to `omitted`. Use the actual snapshot order of `get_run_snapshots`.

**Probe.** The fallback was removed in-process through a scratch pytest plugin, with no
repo edit, and the full suite was run with PG on `imp168b`. It took 38 min: 5277 passed and
8 failed.

- The only failure caused by the fallback is the legacy pin above,
  `tests/test_vcs_snapshot_postgres.py:376`.
- The other 7 are in `tests/test_deploy_monitoring.py` (`pg-backup`). They also fail
  without the probe: a local `~/.local/bin/sh` wrapper causes them, and they pass with the
  wrapper off `PATH`.

**Parser, `tests/test_prompt_builder.py` (Task 2),** next to the parse tests at `:109-170`.
Expected values are literal.

1. `diff --git a/src/q b/r.py b/src/q b/r.py` + hunk → path `src/q b/r.py`.
2. A rename header whose names both contain `" b/"`, with a pre-hunk `rename to` → the
   `rename to` path, status `renamed`.
3. A bare `rename to evil.py` inside a hunk → path and status unchanged; later lines keep
   their numbers.
4. `+x rename to evil.py` and `+a\x0c b` → one added line each, content intact, no
   extra context line, numbering unshifted, path unchanged.
5. A bare `deleted file mode 100644` inside a hunk → status stays `modified`.
6. CRLF lines give the same `DiffLine`s as before. This pins current behaviour, and it
   passes before and after.

Cases 1 and 3–5 are red at `22c621ab`; case 2 may already pass.

**Findings guard `tests/test_post_processing_diff_map.py` (Task 3, after #229; D2).** The
file is new. `tests/test_findings_post_processor.py` is touched by #227 and #229.

- Feed `diff_map_from_snapshots` a `StoreDiffSnapshot`-normalized row for `src/q b/r.py`.
  Its added lines include `+y rename to evil.py`.
- Assert that the DiffMap file has `new_path == "src/q b/r.py"`, with unshifted new-line
  anchors.
- Assert that `resolve_anchor(diff_map, path="src/q b/r.py", start_line=None, line=2)`
  resolves.
- This is a guard test, green on arrival: the fix there is #229's design. The PR body
  states that.

## Coordination

The coordinator posts the comments before the edit.

| File / function | Open PR | Overlap | Action |
| --- | --- | --- | --- |
| `prompt_builder.py` `parse_unified_diff`, `_path_from_diff_header` (`:534-601`) | #230 (`fa2f4cf`) | #230's hunks end near original `:465`; no textual overlap | comment in #230 |
| `tests/test_review_context_stage.py` (imported, not edited) | #220, #230 | a helper rename there would break our import | rebase check before merge |
| post-processing / `run_repository.py` | #229, #227 | not edited; Task 3 lands after #229 merges | note in #229 |

The following files have no open PR: `get_run_diff.py`, `unified_diff.py` (#229 changes
only `HunkRow`, not `split_lf`), `tests/test_get_run_diff.py`, `tests/test_prompt_builder.py`,
`tests/test_vcs_snapshot_postgres.py`.

## Out of scope

- #168 parts 2–6. Among them:
  - the general counter in place of excluded names (RULES §5.3, part 6);
  - the TEST_PLAN §2.7 mapping (part 4);
  - the staging run with a removed excluded file (part 5).
- The #130 integration of the snapshot and context into the prompt.
- Any edit to `run_repository.py`.
- Docs: no change is needed. RULES §5.1 (`:416-418`), SD:1268-1273 and TEST_PLAN:87 already
  state the contract. The code catches up with them.

**Observations, pre-existing, not fixed:**

- `parse_unified_diff` does not read `copy to`: a copy keeps the header path and the status
  `modified`.
- A quoted `diff --git "a/…" "b/…"` header raises `ValueError`.

## Verification

```
uv run ruff check .
uv run ruff format --check .
uv run mypy . --exclude '^docs/reports/'
TEST_DATABASE_URL=postgresql+psycopg://app:app@localhost:5432/imp168b \
TEST_RABBITMQ_URL=amqp://app:app@localhost:5672/imp168b uv run pytest
```

- Run `df -h /tmp` first. The full suite takes about 38 min here, so run it in the
  background. The 7 `tests/test_deploy_monitoring.py` failures come from the local `sh`
  wrapper, not from this change.
- Focused PG run, zero skips: `tests/test_vcs_snapshot_postgres.py`,
  `tests/test_review_prompt_pins_postgres.py`, `tests/test_run_context_origin_postgres.py`
  with `-rs`.
- Process:
  - each task is delegated through a brief file that says never read any `.env`;
  - review by a clean `code-reviewer` with `git show` / `git diff` only, no checkout;
  - check the reviewer's advice against `.agents/rules/backend.md` before pushing.
- PR:
  - title `fix(reviews): keep withheld files out of review prompts (#168)`;
  - body What / Why / How to verify / `Refs #168`, no closing keyword, no new dependency;
  - deviations D1, and D2 if B;
  - reviewer ndovnar; rebase on `main` before review and before merge.
