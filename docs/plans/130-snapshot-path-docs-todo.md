# Task list: documentation of the snapshot review path (#130, #168 part 4, PR G)

The design is in [130-snapshot-path-docs-plan.md](130-snapshot-path-docs-plan.md); this list adds no scope.

- Each task is one Conventional commit whose subject ends with `(#130)`. Part-4 tasks add `Refs #168` to the
  body.
- Re-read every anchor before editing; line numbers are from `4f4a6dfd`.
- Docs only. Do not change code. Never read any `.env`.
- Gates for every task:
  - `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy . --exclude '^docs/reports/'`.
  - Once per PR: the full `uv run pytest` with an isolated DB and vhost.
- G1 and G2 open only after #332 merges, each rebased on main.

## G1

- **G1-0** `docs(plans): plan the snapshot path docs (#130)`
  - Files: these two files, staged with `git add -f`.
  - Done when only these two files are committed.
- **G1-1** `docs(erd): show the policy snapshot, manifest and run pin tables (#130)`
  - Items: E1, E2.
  - Files: `docs/erd/erd.mmd`, `docs/erd/erd.svg`, `docs/BACKEND_ARCHITECTURE.md`.
  - Regenerate the svg with `npx -y @mermaid-js/mermaid-cli@12.0.0 -i docs/erd/erd.mmd -o docs/erd/erd.svg -b transparent`.
    This needs network; ask before running it.
  - Done when:
    - the svg contains RUN_POLICY_SNAPSHOTS, RUN_POLICY_DOCUMENTS, CONTEXT_MANIFESTS and policy_key;
    - every new column exists in migrations 0029, 0030, 0038, 0039 or 0040.
- **G1-2** `docs(review): catalog review.system.v4 and its envelope (#130)`
  - Items: E3, E4.
  - Files: `review/README.md`, `docs/RULES_MD_TEST_REPORT.md`.
  - Done when no "v4 activation remains in F2" or "ждут v4" text is left without R15.
- **G1-3** `docs(api): describe run actions after the snapshot switch-on (#130)`
  - Item: E5.
  - Done when:
    - `uv run pytest tests/test_openapi_contract.py` is green;
    - `npx --yes @redocly/cli@2.57.0 lint contracts/openapi.yaml` is green;
    - the backticked tool set in both descriptions is unchanged.
- **G1-4** `docs(design): describe both review paths and the parser limits (#130)`
  - Items: S1, S2, S3, S5, and C1's SD :1500 line.
  - Done when SD:891 names base for both paths and the legacy and v2 keys.
- **G1-5** `docs(specs): record the conventions key, parser, retries and reserves as built (#130)`
  - Items: C1, C2, C3, C6, C7.
  - Done when every number cites its code anchor; deep is noted as #331.
- **G1-6** `docs(rules): commit messages, the v4 defaults deviation and pin ownership (#130)`
  - Items: R1, R2, R4.

## G2

Before the first commit, the coordinator posts the plan §5 line list in #283, #304, #330, #338 and #339.

- **G2-1** `docs(pipeline): trace catalog and order of the snapshot path (#130)`
  - Items: P1-P9.
  - Done when no "ни один боевой run" phrase is left and P6 cites TL-9.
- **G2-2** `docs(pipeline): no_changes, reserves, verifier codes and the v4 state (#130)`
  - Items: P10-P14.
- **G2-3** `docs(specs): the snapshot path is live in CVS §5.2, §8.3 and §12 (#130)`
  - Items: C4, C8, C9.
- **G2-4** `docs(rules): RFS §7.2 after F2 and policy_path_invalid in §9 (#130)`
  - Items: R3, R6.
  - Rebase over #338 if it merges first.
- **G2-5** `docs(design): SD §11 lists the policy and manifest tables as existing (#130)`
  - Item: S4.
- **G2-6** `docs(test-plan): map the #130 and #168 tests and the v4 state (#130)`
  - Items: T1-T6.
  - Done when every test file in T1 exists and the FakeRedis wording is exact.
- **G2-7** `docs(cicd): REDIS_URL at the workers, rollback row 0040 and the F1 floor (#130)`
  - Items: O1-O3.
- **G2-8** dropped: C5's text goes into #337 itself (plan §6 item 4).
- **X1** dropped: #283 is asked to carry the text (plan §6 item 6).
