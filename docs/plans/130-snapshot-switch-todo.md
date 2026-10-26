# Task list: switching the snapshot review path on (#130, PR F2)

The design is in [130-snapshot-switch-plan.md](130-snapshot-switch-plan.md); this list adds no scope.
Base: `origin/main` `1417faa7`, with F1 (#328) and F4 (#329) merged and F1 on staging.

- Each task is one Conventional commit whose subject ends with `(#130)`.
- Test first: find the flips, flip them and add the new tests, watch them fail, then write the file.
- Gates for every task: the four commands in the plan, with DB and vhost `imp130f2`.
- Every brief says: never read any `.env`.

## T0: Plan documents

- **Commit:** `docs(plans): plan the snapshot path switch-on (#130)`
- **Files:** these two files, staged with `git add -f` (`docs/plans/` is excluded locally).
- **Done when:**
  - [ ] only these two files are committed;
  - [x] Q1 is answered and recorded in the plan (decided by the tech lead on 2026-10-26).

## T1: The switch (R8)

- **Commit:** `feat(prompts): add review.system.v4 and switch the snapshot path on (#130)`. This is
  one commit (TL-1).
- **Files:**
  - new `review/prompts/review.system.v4.md`, following the plan's section outline;
  - `app/modules/reviews/application/policy_snapshot.py`: `PINNED_PROMPT_CHECKSUMS["review.system:v4"]`;
  - `tests/test_prompt_seeding.py`;
  - `tests/test_review_prompt_pins_postgres.py`;
  - `tests/test_run_prompt_pins_postgres.py`;
  - `tests/test_snapshot_path_e2e_postgres.py` (docstring only);
  - new `tests/test_review_prompt_v4.py`.
- **Steps:**
  0. **Find the flips.**
     1. Put a throwaway `review/prompts/review.system.v4.md` in the worktree: valid front matter
        (`key`, `version: 4`, `description`, `input_tags`, `output_schema`) and one line of body.
     2. Run the full suite with the old tests unchanged.
     3. The red set is the authoritative flip list. Compare it with the plan's table "Tests that flip"
        and add any missing entry with its reason.
     4. Delete the throwaway file, and do not commit it.
  1. **Red.**
     - Flip the tests in the table and add the three tests from "New tests".
     - `_V4_CONTENT` now reads the packaged asset, so the modules that import it fail at collection
       until the file exists. That is the expected red.
  2. **The file.** Write `review.system.v4.md`.
     - Every block an example shows is pasted from `PromptBuilder().render_input` of that example's
       context, so each `block_id` and line number is real.
     - An example shows only the blocks it needs.
     - Keep the file at about v3's size.
  3. **The pin.** Compute the file's sha256 and add the `PINNED_PROMPT_CHECKSUMS` entry.
     - Run the full suite again.
     - Anything outside the table that goes red is added to the table with its reason, and then fixed.
  4. **Green.** Run the gates.
- **Tests (the proof of R8):**
  - after the full seed, v4 is active and a new run pins it; the stage serves its snapshot, and the
    conventions key is repo-conventions-v2;
  - a v3 run created before the seed stays legacy: binding, prompt text, pins and snapshot are all
    unchanged;
  - F1-14 (`test_snapshot_path_e2e_postgres.py`) passes with the real file's bytes as the review system
    message;
  - the v4 contract tests pass: immutability, front matter, the golden envelope, examples valid under
    ReviewOutputV2 with coordinates and attribution checked against the example's manifest, no prefix,
    and the trust section.
- **Done when:**
  - [ ] the gates pass;
  - [ ] the step-0 red set and the step-3 rerun both match the plan's table, which was extended where
    needed;
  - [ ] the PR body has:
    - [ ] the size of v4 in bytes and in counter/v2 tokens;
    - [ ] the deploy section: the F1 image to prod first, then F2, deployed by the tech lead;
    - [ ] the rollback floor F1 and the runbook line "no image older than F1 after the v4 seed";
    - [ ] the live-path impact;
    - [ ] the deep engine as an accepted risk, with its follow-up;
    - [ ] TL-8 and TL-10 skipped by the tech lead's decision of 2026-10-26, checked in the phase-3
      smoke (this replaces the parent todo's "the PR body names the TL-8 query result");
    - [ ] a link to the #220 comment;
    - [ ] eval not switched (R15);
  - [ ] the coordination comments are posted on #220, #303 and #330, after their texts are approved;
  - [ ] IMP#144 is signalled with the F2 sha for PR3b.
