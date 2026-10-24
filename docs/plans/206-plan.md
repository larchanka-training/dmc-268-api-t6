# Implementation plan: replay warnings and GitHub failure docs (#206)

## Status and scope

**Human approved the plan and all five coordination comments on 2026-10-23. Development is authorized; publication follows the completed implementation/review checkpoint.**
Issue: https://github.com/larchanka-training/dmc-268-api-t6/issues/206
Latest scope clarification: https://github.com/larchanka-training/dmc-268-api-t6/issues/206#issuecomment-6341879938
Checklist: [206-todo.md](206-todo.md).

Work only in an isolated checkout.
At planning time HEAD and fetched `origin/main` both equal
`66077f292bcd829d1807c520f48b48fd053891f2` (ahead/behind `0/0`); worktree was clean.
Leave the primary checkout, unrelated branches, and untracked files untouched.

The issue's original 49-versus-46-pin baseline description is historical. #178/#233
replaced it with 46 matching pins. Reproduce the masking bug with temporary fixtures;
do not edit a real pin or recapture responses. Existing or future accepted baseline drift
from other PRs must be recorded separately from warnings introduced by this change.

## Design and acceptance mapping

1. In `review/scripts/eval_replay.py:351–354`, retain path-set comparison and warning text,
   then independently compare the digest of **recorded** paths against the manifest.
   A combined drift emits the existing path-set warning first, then the existing digest
   warning. Keep digest safety/missing-file handling, scoring, provenance, report format,
   and CLI exit policy unchanged. A clean fixture still has `warnings=[]`.
2. Treat `classify_vcs_error` in
   `app/modules/reviews/infrastructure/vcs_failures.py:21–39` as the source of truth.
   Update only the diff-failure row in `docs/PIPELINE_SPEC.md:317` and error-class wording
   in `review/README.md:112–114`. Document HTTP 5xx, 401, 422, **all 429 including without
   headers**, other HTTP errors except 404/nonlimited 403, timeout and connection/transport
   failures as retryable `diff_fetch_failed` or `commit_messages_fetch_failed` respectively.
   Preserve commit-page validation failures in README. 404 and 403 without limit evidence
   remain `github_forbidden`. For 403, limit evidence is numeric `Retry-After` (parsed
   seconds) or `X-RateLimit-Remaining: 0`; avoid implying arbitrary header text suffices.
   Keep §5.2's existing commit-message row and access-denied row consistent.
3. Do not change the classifier, GitLab rows, API/OpenAPI, DB, dependencies, live runner,
   static-input definitions, manifest/digests, or recorded responses. No Postman setup or
   running service is needed: the change is offline diagnostic behavior plus documentation.

## Ordered work

1. **Preflight and baseline evidence (small, no source changes).** Record approval and the
   coordination comments below; recheck open PR ownership and fetched main. Run the existing
   replay suite and real recorded replay, preserving JSON, console, exit status and HEAD as
   private temporary evidence. Inventory recorded/current static pins and their union.
2. **TDD regression and minimal fix (two files).** Add a regression using `fixture_root`
   and `_record_with_digests`: first assert a clean fixture, then add a valid rule file
   (changes current path set) and alter the existing recorded rule's bytes, preserving the
   manifest and raw answer bytes. Assert the literal two-warning list. Demonstrate RED
   against the current `elif`; make the smallest independent-check change; demonstrate
   GREEN. Existing C6 dropped-pin/custom-rule tests must remain green.
3. **Warning and exit compatibility (same two files).** Cover/verify the four conditions:
   clean → `[]`; path-only → the original path warning; digest-only → the original digest
   warning; combined → both, in that order. Verify CLI success remains 0 for publishable
   captures even with warnings, console renders both warnings, and existing error/nonpublishable
   exit checks stay green. Reuse fixtures/main monkeypatching as needed; do not modify real
   pins to induce CLI drift. Run the entire replay suite as the checkpoint.
4. **Document existing classes (two files).** After required coordination comments are
   posted, make the two narrow documentation edits above. Manually compare each class with
   `classify_vcs_error` and §5.2's commit/access rows; inspect the diff for unrelated changes.
5. **Final evidence (no additional source files).** Run the complete gates, replay again,
   and compare pre/post report, warnings, metrics/provenance, console and exit status at the
   same base. Run the pin-union intersection check below. Refresh main/PR ownership before
   review; if rebasing changes the base or pins, repeat pre/post comparison at that base.
   Review Standards and Spec through the agent-loop skill; only after approval and completed
   implementation/review use its publisher phase. No push/PR before the final review checkpoint.

## Verification commands and invariants

```sh
uv run pytest tests/test_eval_replay.py -q
uv run python review/scripts/eval_replay.py --report-json <private-output>/replay-before.json
# After implementation: same command with replay-after.json; record both exit statuses.
uv run ruff check .
uv run ruff format --check .
uv run mypy .
uv run pytest
```

Run the regression alone for RED/GREEN before the replay-suite checkpoint. All four gates
are mandatory before claiming implementation done. The planning attempt was blocked by
sandboxed uv cache access; development obtained approved access and completed the commands.
See final evidence below for actual results.

Before editing and again before publication, run this read-only union check from the
worktree. It includes recorded paths, currently produced paths (including selected custom
rules/conventions), tracked changes and untracked proposed files:

```sh
uv run python - <<'PY'
import json
import subprocess
from pathlib import Path
from review.scripts.eval_provenance import rule_json_paths, static_input_paths
from review.scripts.eval_replay import _is_rule_set

repo = Path.cwd()
manifest = json.loads((repo / 'test-prs-dataset/responses/manifest.json').read_text())
recorded = set(manifest['static_inputs'])
conventions = [p for p in recorded if p.startswith('review/prompts/') and p != manifest['prompt_path']]
custom = [p for p in recorded if _is_rule_set(repo / p)]
current = set(static_input_paths(
    manifest['prompt_path'], [*rule_json_paths(repo), *custom],
    conventions_prompt=conventions[0] if len(conventions) == 1 else None,
    repo_root=repo,
))
changed = set(subprocess.check_output(
    ['git', 'diff', '--name-only', 'origin/main', '--'], text=True).splitlines())
changed.update(subprocess.check_output(
    ['git', 'ls-files', '--others', '--exclude-standard'], text=True).splitlines())
intersection = sorted(changed & (recorded | current))
print({'recorded': len(recorded), 'current': len(current), 'intersection': intersection})
assert not intersection, 'A proposed change touches a recorded or current static pin'
assert not any(p.startswith('test-prs-dataset/responses/') for p in changed)
PY
```

The required replay must gain no warning; ideally both reports have `warnings=[]`.
If the base already has accepted digest drift, preserve and explain its exact warnings
and verify equality pre/post rather than refreshing or suppressing them. Synthetic tests
must still prove clean inputs produce no warnings. Preserve all response/manifest bytes.

## Ownership, risks and coordination

Our concrete target lines at this base are PIPELINE_SPEC 317 and README 112–114.
Current reviewed patches have **no direct target-line overlap**, but the repository's
file-level rule still applies: “Never edit files owned by another open PR without a
comment there.” Recheck before editing; posting comments needs human authorization.

| PR | Shared file and reviewed location | Consequence |
| --- | --- | --- |
| #220 | PIPELINE_SPEC §§1/7/8/12/14 | Same file; target row 317 disjoint. |
| #227 | PIPELINE_SPEC lifecycle/SSE and GitLab addition after the existing §5.2 table (patch old line 326), plus later error-code rows | Same file; GitHub diff row 317 unchanged. |
| #228 | PIPELINE_SPEC `index.repo` in §12 | Same file; target row 317 disjoint. |
| #229 | PIPELINE_SPEC coordinate-fallback row around 322 and new GitLab subsection | Same file; nearby row but target 317 disjoint. |
| #230 | README prompt table around 13 and verifier/reflection around 180 | Same file; target paragraph 112–114 disjoint. |

Approved comment posted on #220/#227/#228/#229 (IDs 6360255501, 6360256339, 6360257174, 6360257939):

> Работаю над #206: уточняю только строку чтения GitHub diff в PIPELINE_SPEC §5.2 по classify_vcs_error. GitLab и остальные разделы не меняю; текущие изменения этого PR сохраняются.

Approved comment posted on #230 (ID 6360258831):

> Работаю над #206: уточняю только абзац ошибок чтения сообщений коммитов в review/README.md по classify_vcs_error; описание verifier и таблицу prompts не меняю.

Issue #239 plans a replay edit, but no current open PR owns the replay/test files.
Check again before development/review; coordinate if one appears. PR movement can shift
line numbers, so identify rows by their phase and content when rebasing. The highest
technical risk is accidentally comparing current-path digest instead of recorded-path
digest or changing warning-only exit behavior; explicit regression/compatibility checks
cover both. No paid baseline recapture is in scope.

## Approval checkpoint

The human explicitly approved the plan and all five coordination comments on 2026-10-23;
all comments were posted before development. Initial baseline at `66077f2`: replay suite
98 passed, recorded replay warnings=[], exit 0; 46 matching pins. Task 2 regression first
failed because the digest warning was absent, then passed after the one-condition fix.
Task 2 replay suite: 99 passed. Task 3 compatibility checks: 6 passed; replay suite: 100 passed.

Main advanced during final verification to `5d183fe` (#185). The detached worktree moved
safely with all edits preserved; README now matches its ASCII-decimal Retry-After parser.
The obsolete old-base full suite was deliberately interrupted (852 passed, 203 skipped,
exit 2), never claimed passed. Original-base evidence is historical; fresh-base
results below replace its final gates.

Final verification at `5d183fe6cb3a0bb0ed68ebba062f1852f2d16bf9` (HEAD = fetched main;
remote main rechecked unchanged at handoff):
- `uv run ruff check .`: passed, exit 0.
- `uv run ruff format --check .`: 528 files already formatted, exit 0.
- `uv run mypy .`: no issues in 525 source files, exit 0.
- `uv run pytest` with reporting-only `PYTEST_ADDOPTS=-ra`: 4,186 passed, 886 skipped,
  2 dependency deprecation warnings in 352.63 s, exit 0. Skips: 845 require PostgreSQL,
  24 require PostgreSQL + RabbitMQ, and 17 require RabbitMQ; integration URLs were unset.
- Before/after replay JSON, console and exit files are byte-identical; warnings=[], exit 0,
  unchanged metrics/provenance. Exact base replay source was executed in memory at its
  original file path for the fresh-base baseline, without changing reviewed worktree files.
- Recorded/current/union pins: 46/46/46; changed-file intersection empty. Every pin and all
  25 recorded response/manifest files match base Git-object bytes; no response edits.
- Ownership rechecked: same five commented PRs; no new owner. `git diff --check` passed.
- Each incremental review and the final full-diff review reported Standards 0 / Spec 0.
  Final review includes the refreshed base and README ASCII-decimal clarification.

Implementation/review checkpoints complete; ready for publisher handoff. Publication itself
is pending. No commit, push or PR was created by development.
