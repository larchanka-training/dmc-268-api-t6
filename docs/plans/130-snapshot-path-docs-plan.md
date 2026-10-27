# Plan: documentation of the snapshot review path after F1/F2 (#130 DoD4, #168 part 4, PR G)

## 0. Status and baseline

- Refreshed for G2 on 2026-10-27 at origin/main `f8b09818` (#348, F5). Worktree `dmc-268-api-t6-130g2`,
  branch `docs/130-snapshot-path-docs-g2`.
- G1 merged as #343 (`3d77cd6b`).
- Merged after the original base: #304, #332, #338, #337, #283, #343, #344, #345, #346, #347, #330, #348.
- Open: #334, #339, #340, #342.
- G2 line numbers are read at `f8b09818`; G1 rows keep their `4f4a6dfd` numbers.
- Line numbers are read at `4f4a6dfd`. The spec docs are identical at `88bbc073` (#283's base); CICD,
  SECRETS and MONITORING differ (#279).
- Scope: docs, `contracts/openapi.yaml` descriptions, `review/README.md`. Code is not changed, with two G2
  exceptions: `.agents/rules/backend.md` and `.agents/skills/tdd/SKILL.md` (A1), and one docstring outside eval
  `_REVIEW_CODE` (X2). Other code-comment drift is listed in §7.
- Out of scope: the F3 and F5 code (both merged; G2 only describes F5's none route in the trace order, P5),
  MONITORING/dashboard #246, #331, R15.

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
| P1 | PS :9 contracts row; :125 order paragraph | new runs pin the active v4 unless the repository pins its own review prompt (`repositories.prompt_version_id`, `webhook_runs.py:113-119`, both providers). The shared #213 block (PS :187, CVS :1451, RFS :1438, SD :1845, TP :242) says "активирует", is still true, and stays byte-identical | program plan :282 | none | G2 |
| P2 | PS :111 `context.rag` row | drop «а не после `context.build` входа промпта»: since F3 there is one `context.build` per attempt (PS :110) | F3 plan :215-219 | none | G2 |
| P4 | PS §2 first table :117; second table :296-298 | split the row. The verifier `llm.call` (`operation: verification`, state `running`) belongs under the «ревью моделью» generation row. Only `llm.verify` moves between :115 `llm.review_output` and :116 `review.postprocess`, state `publishing` (T8), written once per run (`run_repository.py:866-985`). Drop «цель #107, код пока не пишет». In «Кто», `VerifyCandidates` becomes `ReflectionPass` (`reflection_pass.py:719`). The second table lists `llm.verify` before `review.postprocess` | E, F1 | none | G2 |
| P5 | PS order bullets :125-134 | snapshot cache miss: `context.manifest` before every `llm.call` that is not a retry, then conventions → review → the verifier `llm.call`; then, in the publishing transaction, `llm.review_output` → `llm.verify` → `review.postprocess`. Add a pointer bullet for the zero-LLM none route (PS :266-274, F5): `policy.load → vcs.fetch_diff → context.build → review.postprocess → publish` | E, F1, F5 plan :276 | none | G2 |
| P7 | PS :109 `vcs.fetch_diff` row; §5.2 GitHub table :517-527 (new row) | a retarget before the diff is stored: live base ≠ pinned `base_sha` → `failed`/`pr_base_changed`, no retry and no diff read; a rerun pins the new base (`vcs_diff.py:192-198`; not retryable, `run_failures.py:111-128`). GitHub only, because the GitLab provider reports the pinned base (`gitlab_vcs.py:377-381`) and reads the pinned version triple (`:303-307`), so the check cannot fire there. §6 :623 «GitHub» stays. The old plan's "GitLab too" was wrong | program plan :222 | none | G2 |
| P10 | PS §3 :405, §4.5 :462-463 | as built, snapshot path only: the review keeps 1 slot + 100 s; conventions keep 2 slots + the review call timeout + 100 s; the verifier keeps 10 s (`llm/models.py:70-71,145-167,296-297`; `llm/gateway.py:116-119`). Legacy reserves nothing. There is no money reserve for the verifier (TL-6). Point to CVS §7.4 :1229-1234 | E | none | G2 |
| P11 | PS §5.1 :513 | verifier 402 → `llm_payment_required`, no run retry; overflow → `verification_context_overflow`; 429 → `verification_unavailable` with Retry-After capped (`reflection_pass.py:814-832`) | E | none | G2 |
| P12 | PS §6 :648 | «В PR нет изменённых файлов. Ревью не выполнялось.» (`check_runs.py:37`) | #168 p4 | none | G2 |
| P13 | PS §9 :819-820; TP §3.4 :761 | no `execute_review_legacy_run_without_snapshot` warning exists in `app/`; a Reflection Pass without a snapshot is skipped silently (`execute_review.py:351`) | F1 | none | G2 |

CONTEXT_AND_VERIFICATION_SPEC (CVS)
| C1 | CVS §3.4 :420 and SD L4 :1500 | parser isolation as built: a separate process with a minimal env (`PYTHONPATH`, `PYTHONDONTWRITEBYTECODE`) and stderr to DEVNULL (`subprocess_source_parser.py:106-122`); `RLIMIT_AS` (`ast_worker.py:55`); a soft per-request `RLIMIT_CPU` (`:83`); `RLIMIT_CORE` 0 (`:40`); the parent kills on wall clock; no OS network isolation, so "without network" is a property of the parser code (tech lead, #168 comment 6387947446) | AC p4 / #168 p6 / parser-cpu plan | none | G1 #343 |
| C2 | CVS §4.2 :655 and key :662-670 | deviation TL-7: the v2 key covers the namespace, the ignore key (with tainted rename/copy paths), the rules.md and AGENTS.md text digests, `defaults_checksum`, the conventions prompt id and checksum, and the counter version (`conventions.py:243-275`); it leaves out `policy_base_sha`, selector/truncator versions and rendered-input, tree and source digests; up to 100 changed paths are rendered as `<changed_files>` (`conventions_prompt.py:42-45`, `MAX_TRACED_FILES` `conventions.py:44`) but not keyed; the legacy key is the ignore key; this closes AC9 "repo-conventions-v2" | TL-7, F1, program plan | none | G1 #343 |
| C3 | CVS §5.1 :711-713 | a transport retry reuses its call's `call_id` and manifest row (`llm/gateway.py:220`) | E | none | G1 #343 |
| C6 | CVS §7.3 :963-990 (example :982) | `reserved_calls`: review 1, conventions 2, verifier 0, snapshot path only (`llm/models.py:66-67`); with a fallback configured the miss chain is C1 → R1 invalid → R2 fallback → V1 (TL-2); bounded repair sends `<previous_model_output>` of at most 24 000 chars (`llm/gateway.py:98-105,1036`); legacy is unchanged | E, F1 | none | G1 #343 |
| C7 | CVS §7.4 :1023-1029 | the time reserves of P10; no money reserve for the verifier yet (TL-6); the deep limitation is #331 | E, program plan | #330 :1037 (end of §7.4) | G1 #343 |
| C8 | CVS §8.3 :1497 | `run_policy_snapshots` plus staging `run_policy_documents` (`20261011_0029`) and `context_manifests` (`20261025_0040`) exist; the other names stay targets | drift | none | G2 |
| C10 | CVS §6.2, after :1042 | the publication allowlist `allowed_v2_rules` admits a document by `is_present` (`execute_review.py:419-424`), which is wider than pass-3 citability (`review_output.py:316`); pass 3 has already dropped such candidates. Code follow-up candidate: one shared predicate | program plan :262 | #334 :1062-1067 (20 lines below) | G2 |

RULES_FORMAT_SPEC (RFS)
| R1 | RFS §5.3 :499-502 | PR commit messages are author text, like the PR description; excluded names in them are not filtered (tech lead, 6387947446, #82) | #168 p6 / program plan | none | G1 #343 |
| R2 | RFS §6.1 :527-555 | deviation (decision Q1, 2026-10-26): v4 keeps v3's analysis order in the system message; standard findings get `rule_name: null` (`review.system.v4.md:495`); `<service_defaults>` renders the v1 rule sets; there are no `SEC1`/`COR1`/`PERF1`/`READ1`; service-defaults/v2 comes later (v5+) | F2, program plan | none | G1 #343 |
| R3 | RFS §7.2 :831, :864-867 | drop «#130 F этим изменением не выполняется» and «передана в #168». The stage counts with the pinned counter on the snapshot path and with 4 chars on legacy (`review_context_stage.py:95-100,636-641`). `verifier_model`/`fallback_verifier_model` come from `LLM_VERIFIER_MODEL`/`LLM_VERIFIER_FALLBACK_MODEL`, else the review pair (`llm/settings.py:124-133`, `worker.py:571-583`) | D, E, F1 | none | G2 |
| R4 | RFS §7.4 :803-806 | prompt bindings are pinned at run creation (#130 D, 0039); the execution profile at the first snapshot (accepted deviation, :718) | D | none | G1 #343 |
| R6 | RFS §9 table, after :1162 | new row `policy_path_invalid`: `failed`, no retry (`run_failures.py:76-95`). It is a defensive code of `IgnoreMatcher.match` (`review_ignore.py:334-339`) that is never reached, because callers check `is_supported_path` first and give `unsupported_path` (`review_context_stage.py:438,454`; `review_context.py:506`; `prompt_budget.py:342`). Check-run text: `check_runs.py:58` | #168 p4, TL decision 5 | none (#342 at :1321) | G2 |
| R7 | RFS §5.3 :594-597 | excluded names are hidden on both paths (`execute_review.py:258-262`). The `<excluded_files count>` counter appears only on the snapshot path with a built context (`:284-291`, `prompt_builder.py:282-283`). Legacy and a caught build failure have none (`test_review_context_blocks.py:658,667`) | F3 Q3 (plan :248) | none | G2 |

SYSTEM_DESIGN (SD)
| S1 | SD §7.3 :876-878 and :891 | the runtime per path. Legacy (v3 or lower): conventions read at base since #136 (`worker.py:756`, `bootstrap/gitlab_reviews.py:122`, `github_run_source.py:107-110`); key (repository_id, agents_md_sha, prompt_version_id, policy_key = ignore key) (`repositories/infrastructure/models.py:178-186`); the stage context reaches the trace only; 4 chars per token. Snapshot path (v4): conventions at base, the v2 key, L2-L4 in the prompt, the pinned counter/v2, the verifier and the reserves | AC p4 + tech-lead note | none | G1 #343 |
| S2 | SD L0 :1290-1297 | up to 100 traced changed paths are rendered but not keyed (pointer to CVS §4.2) | TL-7 | #283 :1299-1312 (below) | G1 #343 |
| S3 | SD §10 :1585-1592 | the key per the CVS §4.2 deviation (C2) | TL-7 | none | G1 #343 |
| S4 | SD §11 :1917-1918, plus a new RepoConventions row | `run_policy_documents` and `run_policy_snapshots` (0029), `repo_conventions.policy_key` (0030) with `uq_repo_conventions_snapshot` NULLS NOT DISTINCT (`repositories/infrastructure/models.py:175-186`), and `context_manifests` (0040) exist. `context_evidence_blobs` stays a target. Drop «#130 F wiring остаётся отдельной работой» | #168 p4 | none (#334 :2128, #340 :2317) | G2 |
| S5 | SD §15 OQ-2 :1956 | the verifier pair `LLM_VERIFIER_MODEL` / `LLM_VERIFIER_FALLBACK_MODEL`, which default to `LLM_MODEL` / `LLM_FALLBACK_MODEL` | E | none | G1 #343 |

ERD, BACKEND_ARCHITECTURE, review/README, report, openapi
| E1 | docs/erd/erd.mmd + erd.svg | add RUN_POLICY_DOCUMENTS, RUN_POLICY_SNAPSHOTS (FKs runs, repositories, rule_versions; `snapshot_id` UK) and CONTEXT_MANIFESTS (FK runs, `policy_snapshot_id` → snapshot); add REPO_CONVENTIONS.policy_key; add the RUNS prompt-pin FKs and severity_threshold/max_comments (migrations 0029, 0030, 0038, 0039, 0040) | AC p4, D, program plan | none | G1 #343 |
| E2 | BACKEND_ARCHITECTURE :447, :500-503 | the reviews row gains RunPolicyDocument, RunPolicySnapshot and ContextManifest (`reviews/infrastructure/models.py:296,329,466`); the ERD paragraph names them | AC p4 | none | G1 #343 |
| E3 | review/README.md :17, :33-36, :85-108, :192-204 | a Files row for v4; new runs pin v4 and v3 stays for earlier runs; the manual gateway check defaults to v3 until R15 (`app/bootstrap/llm_gateway.py:90`); the v4 tags `<context_excerpts>` and `<excluded_files count>` (`prompt_builder.py:273-278`); remove "v4 activation remains in F2" | F2, program plan | none | G1 #343 |
| E4 | RULES_MD_TEST_REPORT :124-126, :192 | v4 is live; paid AC8 runs wait for R15 and the budget; on v4 runs rules.md reaches the model, and legacy keeps `allowed_v2_rules=None` (`run_repository.py:779`) while the verified path supplies its own allowlist (`execute_review.py:374-378`); the part-4 `policy_path_invalid` line at :293 moves to G2 with R6 (§6 item 5) | F2, AC | none | G1 #343 |
| E5 | contracts/openapi.yaml :917-922, :1335-1340 | drop "which no live run takes yet"; the backticked tool set stays equal to `RUN_ACTION_TOOLS` (`tests/test_openapi_contract.py:1361-1379`) | drift | #283 (other lines) | G1 #343 |
| B1 | BACKEND_ARCHITECTURE :448 | add CodeChangeDiff, CachedFileBlob and RunActionResponseBody (`reviews/infrastructure/models.py:131,171,454`; 13 classes in total). The ERD is unchanged, since it claims only key relations (:505-510) | G1 drift list | none | G2 |

TEST_PLAN (TP)
| T1 | TP §2.7, new list after the #213 inventory (:401, before §2.8 :403) | a test map in seven groups: (1) policy loader; (2) review-ignore; (3) DiffMap; (4) AST; (5) context, with the wiring guard `test_context_stage_wiring.py`; (6) the snapshot path: `test_review_path.py`, `test_snapshot_path_live_pin.py`, `test_review_prompt_v4.py`, `test_review_context_blocks.py`, `test_reflection_pass_snapshot_path.py`, `test_gateway_verifier_snapshot_path.py`, `test_llm_gateway_snapshot_reserve.py`, `test_gateway_repair_envelope.py`, `test_llm_verify_trace.py`, `test_gitlab_snapshot_path.py`, `test_review_output_policy_attribution.py` (row :478), the five `test_coverage_none_*` files (rows :452-454) and `test_retry_after.py`; (7) snapshot on PostgreSQL: `test_policy_snapshot_postgres.py`, `test_policy_snapshot_integrity_postgres.py`, `test_run_policy_snapshot_origin_postgres.py`, `test_context_manifests_postgres.py`, `test_llm_verify_trace_postgres.py`, `test_rules_md_e2e_postgres.py`, `test_snapshot_path_e2e_postgres.py`. Groups 1-5 keep the old T1 file list plus `test_github_snapshot_source.py`, `test_policy_budget.py`, `test_policy_snapshot.py` and `test_policy_snapshot_integrity.py`. FakeRedis sentence: the Redis caches are tested only against in-test `FakeRedis` classes (`test_redis_context_cache.py:22`, `test_redis_installation_token_cache.py:33`); CI has only PostgreSQL and RabbitMQ (`.github/workflows/ci-cd.yml:268,281`) | #168 p4, DoD4, F5 plan :278 | none | G2 |
| T2 | TP §2.8 :464 integrity row | add `test_policy_snapshot_integrity.py`, `test_policy_snapshot_integrity_postgres.py` and `test_diff_map_pinned_base.py::test_a_pr_retargeted_before_the_diff_is_stored_fails_without_reading_the_diff` (:240) | program plan :222 | none | G2 |
| T3 | TP §2.8 intro :405; §3.6 :908 | rules.md reaches the model on v4 runs since F2. Live AC8 runs wait for R15 (the eval defaults to v3, `app/bootstrap/llm_gateway.py:90`) and for the budget | F2 | none | G2 |
| T4 | TP §2.8 :472 repair row | covered on the snapshot path (`test_gateway_repair_envelope.py:142,161`); legacy keeps the assistant turn (`:248`) | E | none | G2 |
| T5 | TP §2.8 :480; RULES_MD_TEST_REPORT :293-294 | #168 ч.4 is resolved by R6 and ч.6 is covered by #347. #130 remainder: `policy_prompt_budget_exceeded` is reachable on v4 runs (`test_review_path.py:640,699`), and the RFS §11 row for `unsupported_path` + `**/*` is none/0 LLM on the snapshot path (F5, TP :452); legacy (≤ v3) still calls the model. Leave the #247 entries to #247 | #168 p4, G1 report | none | G2 |

SECRETS / CICD
| O1 | SECRETS :95; CICD :370 workers paragraph | the app treats `REDIS_URL` as optional (`env.get("REDIS_URL") or None`, `app/worker.py:532`, `app/webhook_worker.py:119`; `test_worker_token_cache_wiring.py:127`). Staging compose still requires `REDIS_PASSWORD` to build it (`deploy/compose/staging.yml:20,75,116`). Neither worker has `depends_on: redis` (`docker-compose.yml:72-78,160-166`; staging `:78-84,119-125`). An unreachable Redis reads as a miss (`redis_context_cache.py:1-5`) | #168 p4 | #339 CICD :368 (same hunk) | G2 |
| O2 | CICD rollback table, between :335 and :336 | row `20261025_0040` (#130 F1): downgrade drops `context_manifests` and `ix_context_manifests_run_attempt` (`alembic/versions/20261025_0040_context_manifests.py:70-72`); re-upgrade gives an empty table | drift | none | G2 |
| O3 | CICD §5, after :453 | the F1 floor: the F2 seed never deactivates v4. An image older than F1 (#328) would run v4 runs on legacy with the v1 validator. The revision guard refuses it while the DB is at or above 0040 (`deploy/scripts/check-rollback-revision.py:87-97`) | F2 hazards | none | G2 |

Agent rules
| A1 | `.agents/rules/backend.md:102`; `.agents/skills/tdd/SKILL.md:15` | `tests/test_<feature>.py`, or `tests/test_<feature>_<aspect>.py` files sharing one prefix; PostgreSQL tests in `_postgres` files. Examples: 21 `test_clean_without_text*` files (6 `_postgres`, #213) and 5 `test_coverage_none_*` (2 `_postgres`). Not changed: `review/rules/default-backend.v1.json:56` and RFS :695 B5.1, which are reviewer defaults and eval-digest inputs | #348 thread 4394867365 | none | G2 |

Code docstring
| X2 | `app/modules/reviews/infrastructure/ast_worker.py:3` | "a minimal environment (`PYTHONPATH`, `PYTHONDONTWRITEBYTECODE`)" (`subprocess_source_parser.py:116`); the file is not in `_REVIEW_CODE` (`review/scripts/eval_provenance.py:19-42`). Optional: `tests/test_llm_verify_trace.py:6` `PIPELINE_SPEC.md:177` → §2 :298 | G1 drift list | none | G2 |

## 3. Count by source

- G1: 17 items, merged in #343.
- G2: 26 items.
  - #168 part 4: T1, S4, O1, P12, R6.
  - Program plan and handovers: P1, P7, C10, R7, T2, A1.
  - Drift: C8, B1, O2, X2.
- Already true: 13 items (the dropped list in §7).

## 4. Split

- **G2**: one PR of 10 commits. Optionally split G2-8 and G2-9 into their own PR.

## 5. Coordination (line numbers at `f8b09818`; each PR's own base line in parentheses)

| PR | Lines that meet G |
| --- | --- |
| #339 ndovnar (base 33183d90) | CICD :368 (same) is adjacent to G2's :370. The table rows :335-336 and the §5 paragraph after :453 are outside its hunks (-365,7; -420,8; -477,9). SECRETS :95 is outside its hunks (-68,6; -115,6). Also ask about the missing 0041/0042 rows. |
| #340 ndovnar (base 430d6604) | PS :947-953 (:744), :1011-1013 (:808); SD :35, :1050-1076 (:1041/:1061), :2317 (:2095); TP :930 (:658); CICD :663 (:661); SECRETS :529. No shared lines. G2 words the provider statements without a provider list, so adding bitbucket to `SNAPSHOT_REVIEW_PROVIDERS` keeps them true. |
| #334 skvertl (base 850e49b8) | CVS :1010 (:832), :1062-1067 (:876), :2031 (:1673); SD :29, :1044, :1096-1120 (:1087), :2128 (:1906). No shared lines. |
| #342 createunix (base 8cecac11) | RFS :1321-1331 (:1297). FYI only. |

## 6. Decisions of the tech lead

1. **AC wording.** Decided by the tech lead on 2026-10-27 (per the coordinator's brief): the note is posted in #168 (6436545320); the part-4 AC closes by describing both paths (legacy ≤v3, snapshot v4).
2. **Wait for #332?** Decided by the tech lead on 2026-10-27 (per the coordinator's brief): yes; G1 is written now, stacked on F2, and opens as a PR only after #332 merges; G2 comes later.
3. **G2 vs #283.** Decided by the tech lead on 2026-10-27 (per the coordinator's brief): yes, G2 goes before #283, with narrow hunks and comments in #283.
4. **#337 doc lines.** Decided by the tech lead on 2026-10-27 (per the coordinator's brief): they go into #337 itself, not into G. Fulfilled: `430d6604`.
5. **`policy_path_invalid`.** Decided by the tech lead on 2026-10-27 (per the coordinator's brief): RFS §9 documents it in G2 (R6), not in G1.
6. **Live-pin docstring.** Decided by the tech lead on 2026-10-27 (per the coordinator's brief): #283 is asked to carry X1's text. Fulfilled: #283.

## 7. Already done or wrong in the sources, and drift outside G

- Dropped:
  - P3, P8, P9, P14, C4, C9: #283 already wrote them (the "ни один боевой run" phrase is gone).
  - The rest of P2, and P6: F3 (#346) wrote them (one `context.build` per attempt, TL-9).
  - C5: #337's own docs commit 430d6604 covers it (CVS :1036-1042, RFS :1020-1024, TP :478).
  - T6: TP :755-757 is already true; its warning line moved into P13.
  - X1: the docstring is done in #283 (`test_snapshot_path_live_pin.py:1-8`).
  - README :204 «legacy worker response contract»: removed by G1.
  - `review_path.py` docstring: still true. TL-13's refusal rule stands; #220 lifted only its GitHub-only
    part. #340 also edits that file at :22-31.
  - MONITORING `context.build` written twice: fixed by F3.
  - ERD: done in G1. G2 does not change the ERD, so no svg rebuild. Command for reference:
    `npx -y @mermaid-js/mermaid-cli@12.0.0 -i docs/erd/erd.mmd -o docs/erd/erd.svg -b transparent`.
- GitLab `pr_base_changed`: wrong in the old P7.
- Not G (code or tests):
  - The `github_run_source.py:3` docstring says the tree is read at head; it has been read at base since #136.
  - The ERD lacks repo_convention_drafts, code_change_diffs, cached_file_blobs, run_action_response_bodies
    and the GitLab tables (older drift).

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

## 9. Open questions

- **Q1.** The CICD rollback table lacks 0041/0042 (#220, GitLab). Recommendation: G2 adds 0040 only, #339 is
  asked for 0041/0042, and G2-7 adds them if #339 declines.
