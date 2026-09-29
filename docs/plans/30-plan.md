# Implementation Plan: issue #30 — Gold Benchmark and LLM Eval

## Scope and source of truth

This plan covers [api#30](https://github.com/larchanka-training/dmc-268-api-t6/issues/30) in **both** `dmc-268-api-t6` and `dmc-268-ui-t6`. Target: 20–30 reproducible PR cases, replay/live evaluation, CI reports, real integration coverage, UI contract/auth tests, and a canonical TEST_PLAN in api. The sprint deadline in the issue is 04.10.2026. This document is planning only; task completion and gates are tracked in `30-todo.md`.

Read against api `origin/main` at `3cdb417` on 29.09.2026, `docs/SYSTEM_DESIGN.md`, `docs/PIPELINE_SPEC.md`, `contracts/openapi.yaml`, `review/schemas/review-output.schema.json`, the existing [ui TEST_PLAN](https://github.com/larchanka-training/dmc-268-ui-t6/blob/main/docs/TEST_PLAN.md), and the three dependency issues [api#11](https://github.com/larchanka-training/dmc-268-api-t6/issues/11), [api#34](https://github.com/larchanka-training/dmc-268-api-t6/issues/34), [ui#50](https://github.com/larchanka-training/dmc-268-ui-t6/issues/50).

## Current readiness and ownership

| Area | On current api main | Required handoff |
| --- | --- | --- |
| Review contract | `review/schemas/review-output.schema.json`, `review/scripts/validate_findings.py`, `PromptBuilder`, and unit tests exist. Validator exit 0 plus `OK ReviewOutput` is the validity oracle; 1/2 and `RepoConventionsDraft` count invalid. | Replay can start now. Live uses PromptBuilder but needs selected model/adapter and LLM credentials (#33/OQ-2). |
| Runs and DB | Run model, migrations, API read paths, worker composition, and migration tests exist. `tests/test_initial_migration.py` checks index presence only; `/api/*` currently has no auth. | SQL invariant tests now; webhook/authorization tests after #11, rerun after #34. |
| CI | Required `Python lint / type / test` runs uv gates with no Postgres/RabbitMQ service or `TEST_DATABASE_URL`; integration tests skip. | Add services and fail on an integration skip; retain the check name. |
| UI | Main holds sprint-1 `docs/TEST_PLAN.md` and nine unusable `tests/golden_dataset/` cases. Existing Zod schemas/tests provide a contract starting point. | [ui#50](https://github.com/larchanka-training/dmc-268-ui-t6/issues/50) is in open PR #55 for auth code; UI PR #58 edits `docs/TEST_PLAN.md` and `FRONTEND_ARCHITECTURE.md`. |

Open api PR #38 owns #11 auth/webhook code, tests, `app/main.py`, and `pyproject.toml`. Open api PR #10 owns service reorganization, `docker-compose.yml`, `pyproject.toml`, and `docs/SYSTEM_DESIGN.md`. Open UI PR #55 owns auth files, UI CI workflow, `package.json`, and more; PR #58 owns UI TEST_PLAN and frontend architecture. **No task edits a file owned by another open PR without first commenting in that PR.** Build independent work in new files; defer shared-file tasks until merge/rebase or coordinate by a PR comment. Recheck diffs before each slice, especially `pyproject.toml`, workflows, docs, auth tests, and migration files. Do not edit frozen merged Alembic revisions.

## Canonical decisions

1. Use `Run`, not `ReviewJob`. Webhook → `try_enqueue` creates a queued Run. There is no `POST /api/runs`; browser rerun is `POST /api/runs/{run_id}/rerun` (T3).
2. **Issue text conflicts with current main:** issue #30 still describes `review_requested` / `review_request_removed` for the bot, but SD Р-10 and PIPELINE_SPEC §8 now specify a human-managed `ai-review` label and green CI (decision in api#37). GitHub App cannot be requested as reviewer. Tests and TEST_PLAN must use `labeled`/`unlabeled`, `synchronize`, `reopened`, check-suite/status and sweep; flag `reviewer_requested` now means the label is active. Sync the stale acceptance wording in issue #30 with the owner before considering it closed. No implementation should revive `review_requested`.
3. Case = JSON metadata/ground truth, valid unified patch, committed pre-image, and replay output. Schema is specific to the case; do not copy ReviewOutput schema. Ground truth uses Finding contract `path`, `start_line`, `line`, `severity`, `category`; `start_line: null` means single line, otherwise `< line`. Expected verdict uses `blocking|attention|clean`, derived from severity by PIPELINE_SPEC §11. Preserve source URL, revision/PR, license name and URL, and attribution for every real case. Confirm license permits the exact excerpt; never include real secrets.
4. Plan a **24-case corpus** within the issue's 20–30 range: four completed security cases (C01–C04) and five cases in each other curator class. Keep at least one licensed real PR in each class (at least five total), both Python and TypeScript/React, and at least five separate `critical` ground-truth findings. C01/C02 provide two critical truths; at least three more must come from resource/logic cases, with severity justified by the concrete impact rather than assigned to satisfy a count. C05/SEC-05 is omitted after two proposed variants were stopped by the automatic safety filter; do not attempt another security case for this slot. Proposed primary category mapping: security → `security`; resource/memory leak → `performance` unless the case's specific failure is correctness; logic → `correctness`; syntax overhead → `readability`; clean → no findings/category. Record every exception explicitly in case metadata and README. The old TC-01…06 are ideas only and must be repaired; TC-07 becomes a generated-file unit test, TC-08 a webhook integration test, TC-09 is phase 3.
5. Evaluate the **raw** ReviewOutput before postprocessing so prompt quality is measurable. Document one-to-one matching in TEST_PLAN §3: same path and category, matching new-side changed-line anchor within a fixed ±2-line tolerance, at most one prediction per truth; duplicates and findings in clean cases are FP, unmatched truths FN. Severity is reported, not silently normalized. Define denominator-zero behavior, corpus-level micro aggregation, per-category counts, Critical Recall, validity percentage, verdict agreement, and the separate manual oracle for semantic hallucinations. Do not claim automatic semantic correctness merely from path/line/category; corpus design should avoid two different defects with indistinguishable anchors. LLM-as-a-judge is out of this sprint.
6. Replay runs without network/keys and uses committed raw responses with model ID and prompt-file SHA/version. A response passes only when existing validator exits 0 and prints `OK ReviewOutput`. Invalid outputs remain in the validity denominator and are handled by documented scoring rules. Live calls the selected model using `review/prompts/` through PromptBuilder, records raw response and provenance, and requires an explicit model/secret decision. LLM metrics are reports, not failing threshold gates this sprint.
7. Preserve `.patch` bytes: exclude `test-prs-dataset/` patches from `trailing-whitespace` and `end-of-file-fixer` before committing cases, then rerun `git apply --check` after hooks. Configure a **narrow** Gitleaks exception before a synthetic fixture that resembles a secret is committed, or use unmistakably fake values instead. Do not use a whole-directory secret-scan bypass. Case code stays in patch/data files, avoiding repository-wide Ruff/mypy on fixture source.

## Dependency graph and implementation order

```text
api contracts on main ──┬── TEST_PLAN metric rules ── case schema/validator ── 24 cases ── replay scorer ── replay CI
                        │                                                └── live baseline (model + key) ── live CI
                        ├── PostgreSQL Run invariants ── required CI Postgres/RabbitMQ
api#11 / PR#38 ──────────┴── webhook + auth integration suites ────────────────┘
api#34 ───────────────────── rerun and RabbitMQ worker integration suites ────┘
ui#50 / PR#55 ────────────── UI auth tests ── UI CI required check
api contract ─────────────── UI Finding→ReviewComment test ────────────────────┘
ui PR#58 ──────────────────── TEST_PLAN migration/retirement from UI
```

### Phase 1 — contract and immediately testable paths

1. Finalize the api TEST_PLAN from the UI text, retaining Р-1…Р-9 and adding level/stand/staging/LLM sections; align §1–2 to current SD, cover Р-9/10/14/15, L0–L4, D4 auth and T3 rerun. Define §3 metrics and explicit case templates. Link it from SD, review README and both AGENTS files only after ownership checks.
2. Add case schema and a dataset validator that checks schema, base + `git apply --check` (including patch preservation after hooks), ground-truth anchors on added/new-side lines, verdict derivation, metadata/license fields, class/language/critical distribution, and absence of answer-hint comments. Run a small valid/invalid fixture set.
3. Retain completed C01–C04 and complete C06–C25 in one-case slices from the 24 active slots in TODO. C05/SEC-05 is omitted because two variants hit the automatic safety filter; do not retry that slot. Source real PRs before claiming those slots complete; use excerpts with explicit attribution and license verification. Move TC-07/08 to appropriate test levels and defer TC-09.
4. Add the scorer and replay command with deterministic tests for duplicate findings, missed line, clean-case FP, invalid validator outputs (including exit 2/RepoConventionsDraft), zero denominators, category and verdict calculations. Produce console and JSON reports.

### Checkpoint A

- Corpus validator passes all 24 active cases (security 4, each other class 5); each patch applies to its own committed pre-image and still applies after pre-commit. The omitted C05 is excluded from case and baseline denominators.
- Replay runs with network disabled, outputs all requested metrics and the same JSON twice, and unit tests pass.

### Phase 2 — execution and CI

5. Integrate replay into api PR CI with job summary, model/prompt provenance and warning on changed `review/prompts/` with stale responses. Do not make quality thresholds required.
6. Add the live adapter and recorded baseline only after #33/OQ-2 model choice, secret policy and adapter contract are known. Set up separate non-required `workflow_dispatch` workflow; avoid logging raw secrets or sensitive PR content. Commit raw responses for every case with model/version; put baseline numbers in dataset README.
7. Add PostgreSQL Run invariant integration tests in a disposable schema. Add Postgres and RabbitMQ to required api job and environment variables; prove `integration` skip count is zero. Keep same `Python lint / type / test` check name. Coordinate workflow/docker/pyproject edits with PR #10/#38.

### Checkpoint B

- PR summary shows schema/apply checks and replay metrics, including provenance warning test.
- Required api job runs DB and RabbitMQ tests with zero integration skips. Manual live workflow produces the same report shape; baseline is reproducible under recorded model/prompt version.

### Phase 3 — dependent vertical behavior and UI

8. After #11 lands, test the webhook 202/HMAC/delivery-dedup → queued Run path with a fake GitHub REST API and actual Postgres. Cover label+green CI in either event order, no label, pending/red CI, later green, push supersession/cancel, unlabeled/closed/reopened, same SHA dedup, `wait_for_ci` modes and no-CI sweep. Observe eventual Run after 202 rather than assuming synchronous enqueue.
9. Test D4 callback/refresh/me/logout, bad/expired/foreign/missing-or-malformed claim tokens, refresh reuse family revocation, GitHub exchange failure, two-Workspace isolation and valid `workspaces: []`. Add rerun T3 tests after #34: 202/new queued Run with `trigger=rerun` at current SHA independent of label/CI, 409 on active/closed, 401, and cross-Workspace 404.
10. After UI #55 lands, test callback failure and OAuth state, shared single refresh for concurrent 401s, one retry, refresh failure logout, and startup restore via refresh+me. Add Finding→ReviewComment anchor and enum contract tests under `src/**/*.test.{ts,tsx}`. Add UI PR job for lint, types, format, tests and set that check required in ruleset. Coordinate PR #55's workflow ownership and verify its resulting check name before ruleset change.
11. Remove obsolete UI `tests/golden_dataset/` only after API corpus is complete and UI #58's TEST_PLAN changes are preserved; update UI AGENTS and links. Review api PR #36's QA approval evidence. Finish with repository gates, cross-repo PRs, one approving review, resolved threads, rebase on main, and merge by 04.10; close issue after final PR merge.

### Checkpoint C

- API auth/webhook/rerun integration suites run without skips in required CI; UI auth/contract tests and required PR job pass.
- TEST_PLAN exists only in api; links resolve, UI old dataset is gone, and every issue acceptance item has evidence in the two PRs/CI reports.

## Blockers and risks

| Blocker/risk | Concrete next action |
| --- | --- |
| #11 webhook/auth not merged; PR #38 owns implementation and tests | Draft TEST_PLAN cases and independent DB/replay work now. Put integration tests in #38 with owner coordination or separate PR after merge; never edit its files without a comment. |
| #34 rerun/worker not merged | Document cases now; execute rerun/RabbitMQ integration slices after endpoint and broker adapter land. |
| UI #50 is open PR #55; UI PR #58 edits source TEST_PLAN | Read PR diffs first; schedule auth tests/workflow and TEST_PLAN move after merge or comment in those PRs before touching owned files. |
| Api PR #10 restructures service/CI/pyproject; #38 also touches pyproject | Keep corpus/scorer/docs in new paths first. Rebase and inspect ownership before CI/config edits. |
| #33 model choice, credential availability, usage cost and live CI secret policy | Replay does not wait. Record model+prompt digest; run live manually only with approved credentials, then snapshot baseline and enable dispatch. |
| Five licensed real PRs and known ground truth | Source candidates early, inspect license in source revision, obtain stable URL and commit, isolate minimal valid diff, manually verify defect/clean oracle. Replace any candidate lacking compatible license or clear truth. |
| C05/SEC-05 stopped twice by automatic safety filter | Document the omitted slot; make no further security-case attempts. The 24-case target remains within issue bounds, with four security cases meeting its per-class minimum. Obtain the remaining three or more defensible critical truths from resource/logic cases. |
| Issue's obsolete `review_requested` acceptance text | Ask issue owner to update/acknowledge it; test label-driven Р-10 from current main. |
| Old patches, fake secrets and formatting hooks | Regenerate valid patches from base; protect patch bytes before commit; use fake placeholders or narrow Gitleaks allowlist before history acquires a fixture. |
| Staging/ruleset permission | Record CI result and check name first, then authorized maintainer sets UI required check. Don't claim acceptance until ruleset visibly includes it. |

## Definition of done and review

Each vertical slice leaves tests passing. Before claiming the full task done: in api run `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy .`, `uv run pytest` with integration services configured; in UI run `pnpm lint`, `pnpm check-types`, `pnpm format:check`, `pnpm test`, `pnpm build`. Verify GitHub required checks, zero integration skips, complete corpus counts, replay/live reports and links. PRs use conventional title ≤72 characters and What / Why / How to verify / Refs; UI PR references `larchanka-training/dmc-268-api-t6#30`. Any new dependency needs `uv add`/`pnpm add`, lockfile and an explicit PR body line. Present this plan for human review before implementation as required by the planning skill.
