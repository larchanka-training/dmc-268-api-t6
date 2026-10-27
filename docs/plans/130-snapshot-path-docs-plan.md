# Plan: documentation of the snapshot review path after F1/F2 (#130 DoD4, #168 part 4, PR G)

## 0. Status and baseline

- Planned 2026-10-27 on `feat/130-snapshot-switch` `4f4a6dfd` (#332, F2) over `origin/main` `f160ce4c`.
  F1 #328, F4 #329, C #324, T3 #301, #220 and #279 are in main. #337 (empty-document attribution) is open
  and not in this base. Worktree `dmc-268-api-t6-130g`, branch `docs/130-snapshot-path-docs`.
- Line numbers are read at `4f4a6dfd`. The spec docs are identical at `88bbc073` (#283's base); CICD,
  SECRETS and MONITORING differ (#279).
- Scope: docs, `contracts/openapi.yaml` descriptions, `review/README.md`. Code is not changed. Code-comment
  drift is listed in §7.
- Out of scope: F3 (`context.build` once per attempt, MONITORING/dashboard #246), F5, #331, R15.

## 1. Moved references from the #168 part-4 AC

| AC reference | Now |
| --- | --- |
| SD §11 :1277-1298 | SD §11 :1674-1700 |
| BACKEND_ARCHITECTURE :413 | :447 (model table), :500-503 (ERD paragraph) |
| SD §7.3 :792 | SD §7.3 :876-878 and the bullet at :891 |
| SD :1165 / SD :1432 / CVS :411 | SD :1500 / CVS :420 |
| SECRETS :93, :287; CICD :235 | SECRETS :95 (no :287 counterpart now); CICD :368 |
| PIPELINE_SPEC :129 (`policy.snapshot`) | done by #194; no occurrence in docs, app or openapi |
| PIPELINE_SPEC :323 (`no_changes`) | :448 vs `check_runs.py:37` |

## 2. Item table

Columns: ID | doc: section (line) | what the text must say (code anchor) | source | open PR on those lines | PR

PIPELINE_SPEC (PS)
| P1 | PS :9 contracts row | v4 has been active since F2 (#332), and new runs pin it and take the snapshot path (`review_path.py:25-29`); the v1 schema stays for runs pinned to v3 or lower; remove "активация v4 остаётся в F2" | drift | #283 :6-12 | G2 |
| P2 | PS §2 :104, stage `context.build` row :110 | on the snapshot path the stage's L2-L4 blocks go into `<context_excerpts>` and ignored files are counted in `<excluded_files>` (`execute_review.py:64,265-275`); on legacy the blocks reach the trace only; per TL-9 this record stays (before conventions) and F3 removes the prompt-read record (`run_trace.py:131-175`) | AC p4 / TL-9 / F1 | #283 :106-120 | G2 |
| P3 | PS :114 and :177 `context.manifest` | remove "ни один боевой run пока не попадает": since F2 every new GitHub and GitLab run pins v4 (`review_path.py:25-29`) | F1 | #283 :106-120; #330 :170-180 | G2 |
| P4 | PS :118 verifier row | not a #107 target any more: the verifier `llm.call` (`operation=verification`) runs in `running`; `llm.verify` is written in the publishing transaction before `review.postprocess` (`run_repository.py:875-907`); move the row above `review.postprocess` | E, F1 | #283 :106-120 | G2 |
| P5 | PS order bullets :125-131 | add the snapshot-path order: `context.manifest` before every `llm.call` that is not a retry; publishing writes `llm.review_output`, `llm.verify`, `review.postprocess` (`tests/test_llm_verify_trace_postgres.py`) | E, F1 | #283 :126-150 | G2 |
| P6 | PS :135 canon | TL-9 (2026-10-25) supersedes #165 comment 6239860585: the single remaining `context.build` is the stage's, before conventions; F3 delivers "once per attempt" | TL-9, F1 | #283 :126-150 | G2 |
| P7 | PS `vcs.fetch_diff` row :171, §5.2 :318-330, §6 :423 | `pr_base_changed`: a retarget before the diff is stored fails `vcs.fetch_diff` terminally, without retry; a rerun pins the new base (`vcs_diff.py:186-195`); `process_run.py:226-229` passes the pinned base for every provider, so GitLab MRs too (the §6 source column says only GitHub) | program plan (review of C) | #304 :167-175; #283 :169-188 | G2 |
| P8 | PS :174 `llm.call` row | remove "на который ни один боевой run пока не попадает (#130 F1)" | F1 | #330 :170-180; #283 | G2 |
| P9 | PS :180 `llm.verify` row | written: request `{candidate_ids, accepted_call_id, manifest_id}`, response `{decisions}` without `reason` (`execute_review.py:595-605`) | E, F1 | #330; #283 | G2 |
| P10 | PS §3 :212, §4.5 :263 | as built, snapshot path only: the review reserves 1 slot + 100 s (verifier 90 s + 10 s); conventions reserve 2 slots + the review call timeout + 100 s; the verifier keeps 10 s (`llm/models.py:66-67,136-158,284-289`; `llm/gateway.py:107-113`); legacy reserves nothing; there is no money reserve for the verifier (TL-6 follow-up); deep is refused before the call on a conventions cache miss (#331) | E, program plan | #304 :205 (neighbouring row) | G2 |
| P11 | PS §5.1 :314 verifier codes | 402 → `llm_payment_required` without a run retry; 429 → `verification_unavailable` with Retry-After; overflow → `verification_context_overflow` (`reflection_pass.py:724-760`) | E | #304 :303-317 | G2 |
| P12 | PS §6 :448 `no_changes` | «В PR нет изменённых файлов. Ревью не выполнялось.» (`check_runs.py:37`) | AC p4 | none | G2 |
| P13 | PS §9 :604-623 and :626 | no `execute_review_legacy_run_without_snapshot` warning exists; a reflection pass without a snapshot is skipped silently (`execute_review.py:331`); v4 has been live since F2 and v3 stays for runs pinned earlier; v2 and the verifier schema are live, the live check is phase 3 | F1, F2, E | #283 :621-650 | G2 |
| P14 | PS §13 :747 | the verifier is wired in the worker (`app/worker.py:587-589`) and live for v4 runs | drift | #283 :744-750 | G2 |

CONTEXT_AND_VERIFICATION_SPEC (CVS)
| C1 | CVS §3.4 :420 and SD L4 :1500 | parser isolation as built: a separate process with a minimal env (`PYTHONPATH`, `PYTHONDONTWRITEBYTECODE`) and stderr to DEVNULL (`subprocess_source_parser.py:106-122`); `RLIMIT_AS` (`ast_worker.py:55`); a soft per-request `RLIMIT_CPU` (`:83`); `RLIMIT_CORE` 0 (`:40`); the parent kills on wall clock; no OS network isolation, so "without network" is a property of the parser code (tech lead, #168 comment 6387947446) | AC p4 / #168 p6 / parser-cpu plan | none | G1 |
| C2 | CVS §4.2 :655 and key :662-670 | deviation TL-7: the v2 key covers the namespace, the ignore key (with tainted rename/copy paths), the rules.md and AGENTS.md text digests, `defaults_checksum`, the conventions prompt id and checksum, and the counter version (`conventions.py:243-275`); it leaves out `policy_base_sha`, selector/truncator versions and rendered-input, tree and source digests; up to 100 changed paths are rendered as `<changed_files>` (`conventions_prompt.py:42-45`, `MAX_TRACED_FILES` `conventions.py:44`) but not keyed; the legacy key is the ignore key; this closes AC9 "repo-conventions-v2" | TL-7, F1, program plan | none | G1 |
| C3 | CVS §5.1 :711-713 | a transport retry reuses its call's `call_id` and manifest row (`llm/gateway.py:220`) | E | none | G1 |
| C4 | CVS §5.2 "Текущее состояние" :766-775 | v4 runs carry the snapshot, so the guard checks the ignore deny-set and the policy documents; legacy runs carry none | F1, F2 | #283 :769-779; #330 :768-779 | G2 |
| C5 | CVS §6.2 :853, RFS §8.2 :863-898, a TEST_PLAN row | (#337) attribution to a missing, empty or whitespace-only rules.md/AGENTS.md → `evidence_source_disallowed`, finding dropped; `allowed_v2_rules` (`execute_review.py:374-378`) still checks `is_present` | program plan | #334 :876 (near) | conditional |
| C6 | CVS §7.3 :963-990 (example :982) | `reserved_calls`: review 1, conventions 2, verifier 0, snapshot path only (`llm/models.py:66-67`); with a fallback configured the miss chain is C1 → R1 invalid → R2 fallback → V1 (TL-2); bounded repair sends `<previous_model_output>` of at most 24 000 chars (`llm/gateway.py:98-105,1036`); legacy is unchanged | E, F1 | none | G1 |
| C7 | CVS §7.4 :1023-1029 | the time reserves of P10; no money reserve for the verifier yet (TL-6); the deep limitation is #331 | E, program plan | #330 :1037 (end of §7.4) | G1 |
| C8 | CVS §8.3 :1171 | `run_policy_documents` and `run_policy_snapshots` (0029) and `context_manifests` (0040) exist; the other tables stay targets | drift | #283 :1168-1173 | G2 |
| C9 | CVS §12 :1858 | the verifier has been wired since F1 and live since F2 | drift | #283 :1855-1861 | G2 |

RULES_FORMAT_SPEC (RFS)
| R1 | RFS §5.3 :499-502 | PR commit messages are author text, like the PR description; excluded names in them are not filtered (tech lead, 6387947446, #82) | #168 p6 / program plan | none | G1 |
| R2 | RFS §6.1 :527-555 | deviation (decision Q1, 2026-10-26): v4 keeps v3's analysis order in the system message; standard findings get `rule_name: null` (`review.system.v4.md:495`); `<service_defaults>` renders the v1 rule sets; there are no `SEC1`/`COR1`/`PERF1`/`READ1`; service-defaults/v2 comes later (v5+) | F2, program plan | none | G1 |
| R3 | RFS §7.2 :718-720, :743-753 | drop "#130 F этим изменением не выполняется"; on the snapshot path the stage counts with the pinned counter (`review_context_stage.py:601-606`), so "передана в #168" is done; `verifier_model`/`fallback_verifier_model` come from `LLM_VERIFIER_MODEL`/`LLM_VERIFIER_FALLBACK_MODEL`, else from the review model (`llm/settings.py:124-133`, `worker.py:587-589`); recovery/v1 is the bounded repair with `previous_model_output`; the answer schema follows the bound review prompt (v4 → review-output/v2) | D, E, F1, program plan | #338 +5 after :724 | G2 |
| R4 | RFS §7.4 :803-806 | prompt bindings are pinned at run creation (#130 D, 0039); the execution profile at the first snapshot (accepted deviation, :718) | D | none | G1 |
| R6 | RFS §9 table, after :973; RULES_MD_TEST_REPORT :293 (part-4 line) | add `policy_path_invalid`: `failed`, no retry; a defensive code of `IgnoreMatcher.match` (`review_ignore.py:265`) that the pipeline never reaches, because callers check `is_supported_path` first (`review_context_stage.py:433,449`; `review_context.py:447`; `prompt_budget.py:333`) and give `unsupported_path`; check-run text `check_runs.py:56` | AC p4 | #304 :948-989 | G2 |

SYSTEM_DESIGN (SD)
| S1 | SD §7.3 :876-878 and :891 | the runtime per path. Legacy (v3 or lower): conventions read at base since #136 (`worker.py:756`, `bootstrap/gitlab_reviews.py:122`, `github_run_source.py:107-110`); key (repository_id, agents_md_sha, prompt_version_id, policy_key = ignore key) (`repositories/infrastructure/models.py:178-186`); the stage context reaches the trace only; 4 chars per token. Snapshot path (v4): conventions at base, the v2 key, L2-L4 in the prompt, the pinned counter/v2, the verifier and the reserves | AC p4 + tech-lead note | none | G1 |
| S2 | SD L0 :1290-1297 | up to 100 traced changed paths are rendered but not keyed (pointer to CVS §4.2) | TL-7 | #283 :1299-1312 (below) | G1 |
| S3 | SD §10 :1585-1592 | the key per the CVS §4.2 deviation (C2) | TL-7 | none | G1 |
| S4 | SD §11 :1676-1680, :1699-1700, plus a new RepoConventions row | `run_policy_documents`, `run_policy_snapshots`, `repo_conventions.policy_key` and `context_manifests` exist (0029, 0030, 0040); `uq_repo_conventions_snapshot` is NULLS NOT DISTINCT; `context_evidence_blobs` and the rest stay targets; drop "#130 F wiring остаётся отдельной работой" | AC p4, program plan | #283 :1673 (+99) | G2 |
| S5 | SD §15 OQ-2 :1956 | the verifier pair `LLM_VERIFIER_MODEL` / `LLM_VERIFIER_FALLBACK_MODEL`, which default to `LLM_MODEL` / `LLM_FALLBACK_MODEL` | E | none | G1 |

ERD, BACKEND_ARCHITECTURE, review/README, report, openapi
| E1 | docs/erd/erd.mmd + erd.svg | add RUN_POLICY_DOCUMENTS, RUN_POLICY_SNAPSHOTS (FKs runs, repositories, rule_versions; `snapshot_id` UK) and CONTEXT_MANIFESTS (FK runs, `policy_snapshot_id` → snapshot); add REPO_CONVENTIONS.policy_key; add the RUNS prompt-pin FKs and severity_threshold/max_comments (migrations 0029, 0030, 0038, 0039, 0040) | AC p4, D, program plan | none | G1 |
| E2 | BACKEND_ARCHITECTURE :447, :500-503 | the reviews row gains RunPolicyDocument, RunPolicySnapshot and ContextManifest (`reviews/infrastructure/models.py:296,329,466`); the ERD paragraph names them | AC p4 | none | G1 |
| E3 | review/README.md :17, :33-36, :85-108, :192-204 | a Files row for v4; new runs pin v4 and v3 stays for earlier runs; the manual gateway check defaults to v3 until R15 (`app/bootstrap/llm_gateway.py:90`); the v4 tags `<context_excerpts>` and `<excluded_files count>` (`prompt_builder.py:273-278`); remove "v4 activation remains in F2" | F2, program plan | none | G1 |
| E4 | RULES_MD_TEST_REPORT :124-126, :192 | v4 is live; paid AC8 runs wait for R15 and the budget; on v4 runs rules.md reaches the model, and legacy keeps `allowed_v2_rules=None` (`run_repository.py:779`) while the verified path supplies its own allowlist (`execute_review.py:374-378`); the part-4 `policy_path_invalid` line at :293 moves to G2 with R6 (§6 item 5) | F2, AC | none | G1 |
| E5 | contracts/openapi.yaml :917-922, :1335-1340 | drop "which no live run takes yet"; the backticked tool set stays equal to `RUN_ACTION_TOOLS` (`tests/test_openapi_contract.py:1361-1379`) | drift | #283 (other lines) | G1 |

TEST_PLAN (TP)
| T1 | TP §2.7 :82 (a map right after the intro, away from #283's :89 hunk) | the coverage map. Policy loader: `test_policy_documents.py`, `test_github_policy_source.py`, `test_prepare_policy_snapshot.py`. review-ignore: `test_review_ignore.py`. DiffMap: `test_diff_map.py`, `test_diff_map_pinned_base.py`, `test_post_processing_diff_map.py`. AST: `test_ast_resolution.py`, `test_tree_sitter_facts.py`, `test_ast_worker.py`, `test_subprocess_source_parser.py`. Context: `test_review_context.py`, `test_review_context_stage.py`, `test_run_processor_context_stage.py`, `test_withheld_files_in_prompts.py`. Snapshot on PostgreSQL: `test_policy_snapshot_postgres.py`, `test_policy_snapshot_integrity_postgres.py`, `test_run_policy_snapshot_origin_postgres.py`, `test_rules_md_e2e_postgres.py`, `test_snapshot_path_e2e_postgres.py`. The Redis caches are tested only against the in-test `FakeRedis` (`test_redis_context_cache.py:22`); CI has no Redis service | AC p4, DoD4, withheld/ast plans | #283 :89+ | G2 |
| T2 | TP :197 (+ :119) integrity rows | add `test_policy_snapshot_integrity.py`, `test_policy_snapshot_integrity_postgres.py`, `test_diff_map_pinned_base.py` (C #324) | program plan | #283 :183-198 | G2 |
| T3 | TP :140, :203, :208, :634 | rules.md reaches the model on v4 runs; live AC8 waits for R15 (eval defaults to v3) and the budget | F2 | #283 :135-141 | G2 |
| T4 | TP :204 repair row | covered on the snapshot path (`test_gateway_repair_envelope.py:142,161`); legacy keeps the assistant turn (`:248`) | E | none | G2 |
| T5 | TP :211; RULES_MD_TEST_REPORT :294 | the part-4 divergence is resolved (RFS §9); `policy_prompt_budget_exceeded` is reachable on v4 runs (`test_review_path.py:640,685`) | AC, drift | #304 :208-211 | G2 |
| T6 | TP §3.4 :484-500 | the verifier is wired in both worker compositions for v4 runs; the legacy warning is gone | F1 | #283 :481-507 | G2 |

SECRETS / CICD
| O1 | SECRETS :95, CICD :368 | `REDIS_URL` is optional at worker and webhook-worker (`app/worker.py:539`, `app/webhook_worker.py:119`); neither has `depends_on` on redis (`docker-compose.yml:72-78`, `deploy/compose/staging.yml:78-84,119-125`); an unreachable Redis reads as a cache miss (`redis_context_cache.py:1-4`) | AC p4 | #339 CICD :363-369 (next paragraph) | G2 |
| O2 | CICD rollback table :333 | add `20261025_0040` (F1): downgrade drops `context_manifests`; re-upgrade gives an empty table | drift | #283 inserts 0043 at the same spot | G2 |
| O3 | CICD §5 :402+ | the F1 rollback floor: the seed never deactivates v4; never deploy an image older than F1 (#328) after the F2 seed, because it runs v4 on the legacy path with the v1 validator; the revision guard refuses such an image while the DB is at 0040 or above (`deploy/scripts/check-rollback-revision.py:94`) | F2 hazards, F1 plan | #339 §5 | G2 |

Test docstring
| X1 | tests/test_snapshot_path_live_pin.py:1-8 | "a run bound to v3 or lower, created before the F2 seed or with pre-0039 pins, keeps this path" | F2 | #283 :512 (owns the file) | conditional |

## 3. Count by source

#168 part 4: 9 (T1, S4, E1, E2, S1, C1, O1, P12, R6), plus 1 already done (#194). #130 DoD4: T1-T6. The 8 plans:
24. The program plan: 8 (P7, C2, S2, S3, C5, C7, R1, T2). Drift: 7 (P1, P14, C8, C9, E5, O2, T5). Total 48.

## 4. Split

- **G1** (this branch, 17 items): no open PR touches these lines, so it merges as soon as #332 is in main.
- **G2** (29 items): every section other PRs edit. It rebases after whichever lands first. The coordinator
  posts the line list in #283, #304, #330, #338 and #339 first.
- A per-family split (specs vs SD/rest) does not avoid conflicts, because #283 edits both families.
- Conditional work:
  - C5: in #337 itself (§6 item 4).
  - X1: in #283 (§6 item 6).

## 5. Coordination (line numbers from each PR's base)

| PR | Lines that meet G |
| --- | --- |
| #283 ndovnar, CHANGES_REQUESTED, behind main | PIPELINE_SPEC :6-12, :106-188, :621-650, :744; CVS :769-779, :1168-1173, :1855; SD :1299, :1673 (+99); TEST_PLAN :89-103, :135-141 (+238), :183-198, :481-507; CICD :321-350 (offset by #279); openapi :959, :1176, :1649; live-pin :512 |
| #304 axyi | PIPELINE_SPEC :167-175, :205, :229-239, :303-317; RFS :948 (+37), :976; TEST_PLAN :149, :208-211 |
| #330 axyi | PIPELINE_SPEC :170-180; CVS :768-779, :1037; SD :1517 |
| #338 axyi | RFS +5 after :724, :1172; TEST_PLAN :198 |
| #339 ndovnar, draft | CICD :363 (the api paragraph next to O1), §5 :418, :475; SECRETS hunks avoid :95 |
| #334 skvertl | CVS :876 (near C5), SD :29, :1047, :1862 |
| #337 | no docs; the source of C5 |

## 6. Decisions of the tech lead

1. **AC wording.** Decided by the tech lead on 2026-10-27 (per the coordinator's brief): the note is posted in #168 (6436545320); the part-4 AC closes by describing both paths (legacy ≤v3, snapshot v4).
2. **Wait for #332?** Decided by the tech lead on 2026-10-27 (per the coordinator's brief): yes; G1 is written now, stacked on F2, and opens as a PR only after #332 merges; G2 comes later.
3. **G2 vs #283.** Decided by the tech lead on 2026-10-27 (per the coordinator's brief): yes, G2 goes before #283, with narrow hunks and comments in #283.
4. **#337 doc lines.** Decided by the tech lead on 2026-10-27 (per the coordinator's brief): they go into #337 itself, not into G.
5. **`policy_path_invalid`.** Decided by the tech lead on 2026-10-27 (per the coordinator's brief): RFS §9 documents it in G2 (R6), not in G1.
6. **Live-pin docstring.** Decided by the tech lead on 2026-10-27 (per the coordinator's brief): #283 is asked to carry X1's text.

## 7. Already done or wrong in the sources, and drift outside G

- Already done:
  - `policy.snapshot` (#194).
  - The SD Run row lists the pin columns (:1689).
  - SECRETS lists `LLM_VERIFIER_*` (#329).
  - RFS :718 records the profile deviation.
- Superseded: TL-9 over #165 comment 6239860585; #220 over TL-13 ("GitLab stays legacy").
- Literal corrections:
  - FakeRedis is an in-test class, not the fakeredis library.
  - The parser env is minimal, not empty.
  - The legacy warning named in the F1 plan no longer exists in `app/`.
- Not G (code or tests):
  - The `github_run_source.py:3` docstring says the tree is read at head; it has been read at base since #136.
  - The `tests/test_llm_verify_trace.py:6` docstring cites PIPELINE_SPEC:177 (now :180).
  - The ERD lacks repo_convention_drafts, code_change_diffs, cached_file_blobs, run_action_response_bodies
    and the GitLab tables (older drift).
  - CICD has no rollback rows for 0041/0042 (#220; owner #335/#339).
  - MONITORING still says `context.build` is written twice (F3).

## 8. Review round 1 (2026-10-27)

Eight P3 findings, docs only, fixed in the G1 commits that own the lines (line numbers after the fix).

1. Plan labels now carry a reference a reader can resolve: TL-7 (SD :1600, CVS :692), TL-2 (CVS :1019)
   and TL-6 (CVS :1079) name `docs/plans/130-snapshot-path-plan.md`; Q1 (RFS :558, the F2 analysis-order
   decision) and R15 (README :41, RULES_MD_TEST_REPORT :124, :192) name `docs/plans/130-snapshot-switch-plan.md`.
   Kept: the CVS call notation `C1 → R1 … → V1` (:1013 predates G1), L2-L4, D7, OQ-2, AC8, AC10 and the
   `#130 F1`/`F2` stage names.
2. Wrapping: README :34-51 re-filled; openapi `RunStatus` :920-922 and `RunAction.tool` :1339-1344
   re-flowed, text unchanged. `RunStatus` :923-924 stay as they are because #340 edits :924.
3. README `<context_excerpts>` (:215): `path`, `revision` and `side` only when the source has them
   (`prompt_builder.py:654-663`).
4. README Pass 1 (:221): v4 runs take the snapshot path and the worker runs the Reflection Pass on the
   reviewer's answer (`execute_review.py:331-338`); a run on v3 or lower keeps v1 without it. The
   verifier-output line (:224) now says the same of a legacy run that has a snapshot
   (`review_path.py:1-9`).
5. README "Assembly order" (:95-97): the order and the review tags are the legacy path's; the
   `review.conventions` tags are shared by both paths (`conventions_prompt.py:46-63`).
6. README :82-89: the conventions key includes `policy_key`, the attempt's ignore key on the legacy path
   (`review_context_stage.py:411-425`; NULL without a snapshot, `:427-429`) and the `repo-conventions-v2`
   digest on the snapshot path (`conventions.py:243-272`).
7. RULES_MD_TEST_REPORT :67-68: stage-1 qualifier ("этапа 1 (до #130 F2)"), past tense.
8. SD :879-881: a v4 run never falls back to legacy (`review_path.py:5-9,57-79,99-106`). SD :896: the
   stage counts with counter/v2, while the prompt-read `context.build` record still writes `budget.used`
   as chars//4 (`run_trace.py:151-174`, wired at `worker.py:295`) until F3.

Scope added by this round, all in README, which no open PR touches: :34-51, :82-89, :95-97 and :224.
