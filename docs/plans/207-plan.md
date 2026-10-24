# Implementation Plan: #207 — authoritative index policy and canonical rows

Status: **Approved by the user on 2026-10-23. Tasks 1–5 implemented; final review clear; all four final gates passed.**

## Overview and evidence

Correct the indexer's assumption that a missing archive entry proves that
`.review/rules.md` is absent. Keep source indexing archive based, read policy from the
same immutable commit through the provider-neutral VCS port, and establish a durable
row contract for #144. No public API, schema migration, dependency, prompt, retrieval,
or lifecycle-trigger change is needed.

References:

- [Issue #207](https://github.com/larchanka-training/dmc-268-api-t6/issues/207), including
  [RFS/ADR correction](https://github.com/larchanka-training/dmc-268-api-t6/issues/207#issuecomment-6275733234).
- [PR #176](https://github.com/larchanka-training/dmc-268-api-t6/pull/176), Hand-off to
  #144, TS default/aliased exports, Known limitation.
- [Retriever #144](https://github.com/larchanka-training/dmc-268-api-t6/issues/144).
- `build_index.py:_ignore_policy`, `vcs.py:RepositorySnapshotSource`,
  `github_snapshot_source.py`, `reviews/application/policy_documents.py`,
  `reviews/infrastructure/github_policy_source.py`.

Historical planning/baseline base: `66077f292bcd829d1807c520f48b48fd053891f2`.
Current base after rebase: `origin/main` / `5d183fe6cb3a0bb0ed68ebba062f1852f2d16bf9`.
Isolated checkout: `<isolated-checkout>`.
Do not use the unrelated changes in the original checkout.

## Recommended design

1. Add `rules_document(commit_sha: str) -> PolicyDocument` to
   `RepositorySnapshotSource`. Its contract is provider neutral: an authoritative,
   normalized document at that immutable commit, with `presence=missing` only when
   complete tree listings prove absence. Failures are `PolicyError`; no repository
   content or paths are logged. Returning the existing review application DTO avoids
   inventing a second policy representation.
2. In the GitHub adapter, supply a small counted `PolicyDocumentSource` using the
   snapshot source's request machinery. Reuse `LoadPolicyDocuments.load_document`,
   `policy_tree_entries`, policy HTTP error classification, and normalization rules.
   Read the nonrecursive root tree by commit SHA, then `.review` by tree OID, then
   `rules.md` by blob OID. Do not read root `AGENTS.md`. Read blobs with the existing
   65,536-byte limit and strict decoding/normalization; reject symlinks, submodules,
   invalid parent types, LFS pointers, malformed or truncated trees. A 404 or a partial
   listing is an error, never absence. Keep the existing review reader unchanged
   unless a narrowly justified shared helper is required.
3. After the existing `UNCHANGED` fast path, load policy once at `head.commit_sha`
   through `_network` before any index rows are written. Parse `document.text` with
   `parse_review_ignore`; missing documents produce the existing empty custom masks.
   Use this path for both full and incremental builds, even when the archive contains
   the document. Stop treating `_ignore_policy(scan)` as authoritative and stop
   requesting rules bytes in the archive scan (retain `go.mod`). The archive reader's
   generic document-capture feature remains useful and does not need redesign.
4. Policy errors use the existing `RELEASED` path, `last_error_code=policy_*`, metrics
   persisted, and no new publication. Preserve the previous publication. All provider
   reads remain outside DB transactions and inside the job deadline. Count successful
   and failed tree/blob requests through `request_count`; token exchange stays excluded.
5. Bump `INDEX_EXCLUSIONS_VERSION` from `index-exclusions/v1` to
   `index-exclusions/v2`. It already feeds `limits_profile_version`, so old indices
   fail the version check even at the same commit and rebuild full. The policy digest
   continues to represent parsed masks; a digest change still forces full.

Always reading authoritative policy also avoids applying `export-subst`-expanded
archive policy instead of the original blob. Other source files keep archive
semantics: `export-ignore` paths are absent, `export-subst` is expanded, and the graph
reflects the archive rather than the exact commit tree. Indexing hidden source files
is outside this issue.

## Alternatives and request cost

All counts below include GitHub API and codeload requests, exclude token exchange,
and assume a successful build with no retries. Existing full = 4 (two HEAD calls plus
two archive calls); incremental = 5 (plus compare); UNCHANGED = 2.

| Policy at pinned commit | Additional policy requests | Full total | Incremental total |
| --- | ---: | ---: | ---: |
| `.review` absent in complete root tree | 1 root tree | 5 | 6 |
| `.review` directory present, `rules.md` absent | 2 trees | 6 | 7 |
| regular `rules.md` present, archived or export-ignored | 2 trees + 1 blob | 7 | 8 |

UNCHANGED remains **2** because the current commit/profile check precedes the policy
read. The exclusions-version bump prevents old unsafe publications taking this fast
path. Empty repository HEAD behavior remains unchanged. Early failures count only
requests actually attempted; tests must assert those literal counts too. If policy
is read before archive download, policy errors avoid both archive requests.

| Alternative | Cost and tradeoff |
| --- | --- |
| **Recommended: authoritative tree/blob policy for every changed build** | +1 to +3 calls; identical review document semantics and proof of absence; simple single policy path. |
| Use archive policy when present; tree/blob fallback only when missing | 0 additional calls when present, +1 to +3 when absent; retains two policy paths and `export-subst` differences. Smaller rate-limit cost, weaker parity. |
| Release whenever policy is missing from archive | 0 extra calls; cannot preserve the required valid no-document case without an independent absence proof. |
| Parse `.gitattributes` to decide whether archive absence is safe | Local parsing does not reliably cover nested attributes/macros; substantial new semantics and risk for little benefit. |
| Contents-by-path or treating 404 as missing | Fewer calls but violates existing policy contract and confuses private-repository access loss with absence. Reject. |
| Fetch a recursive repository tree for absence proof | May truncate and is much larger; still needs blob read and does not improve the bounded two-directory walk. |

The additional calls share the installation's 5,000/hour budget with reviews. Record
this cost and the chosen design in the PR body. Update literal request-count fixtures
in `test_github_snapshot_source.py`, build use-case fakes, and PostgreSQL tests; do not
blindly add three to fakes that count method calls rather than HTTP requests. SD §7.4
and PIPELINE_SPEC must state the same conditional table.

## Canonical row contract and documentation

Add an explicit stable anchor `<a id="index-row-contract"></a>` in SD §7.4 with a
subsection for the following semantics. Link CVS §3.5 and ADR §3 to this canonical
section rather than duplicating divergent descriptions.

- The source path/blob set is the GitHub archive at `index_commit_sha`, subject to
  `export-ignore` and `export-subst`; policy alone is read from original Git objects
  at that same commit. Full and incremental must yield identical normalized sets.
- Python/TS `uses_imported` stores `(target_path, exported_name)` while symbol rows
  carry declared names. TS `default` and `export { x as y }` therefore require the
  target's `facts.exports` mapping at retrieval; barrels are not followed.
- Go import-record `target_path` is a package directory (`""` is module root).
  `imports` edges point to every member file; `uses_imported` points to the unique
  member declaring a name. Zero or multiple declarations produce no such edge.
- Wildcard imports, `import a.b`, shadowed aliases, and unreadable `__init__.py`
  produce the module record and `imports` edge only. Receiver calls, barrel
  re-exports, and dynamic name links do not gain speculative name edges.
- Unresolved import records preserve `ImportResolution` values `unsupported`,
  `ambiguous`, `dynamic`, `excluded`, `external`, `missing`. ADR's wording about
  unsupported/ambiguous/dynamic means no graph edge, not loss of the import record.

Correct RFS §5 (currently around lines 427–433) and ADR §9 (around 505–509): only
complete Git tree listings at the pinned commit prove document absence. An archive
alone does not. Unproven absence/read failures stop without publishing; no empty-policy
fallback. Remove the claim that the old archive loader already meets this contract.

Canonical handoff target after merge:
[SD index-row-contract](https://github.com/larchanka-training/dmc-268-api-t6/blob/main/docs/SYSTEM_DESIGN.md#index-row-contract).
Update #144's references/kickoff to this anchor (or post an authorized handoff comment)
so PR #176 is no longer the only row-contract source. This is a future link until the
implementation merges; do not claim it exists now. Do not alter #144 retrieval scope.

## Coordination before implementation

Snapshot of open PR overlap at planning time:

| PR | Planned files also owned there |
| --- | --- |
| [#228](https://github.com/larchanka-training/dmc-268-api-t6/pull/228) | `vcs.py`, SD, CVS, PIPELINE_SPEC, ADR |
| [#229](https://github.com/larchanka-training/dmc-268-api-t6/pull/229) | SD, CVS, PIPELINE_SPEC |
| [#230](https://github.com/larchanka-training/dmc-268-api-t6/pull/230) | RFS |
| [#227](https://github.com/larchanka-training/dmc-268-api-t6/pull/227) | CVS, PIPELINE_SPEC |
| [#220](https://github.com/larchanka-training/dmc-268-api-t6/pull/220) | SD, PIPELINE_SPEC |

Repo rules require a comment on an open PR before editing its owned files. The user
authorized coordination and #144 handoff on 2026-10-23; the coordinator posted comments
on #228, #229, #230, #227, and #220 before implementation. Planning artifacts themselves
do not overlap these PRs.

## Test strategy and baseline

TDD: add failing regressions before changing implementation. Unit tests separate the
VCS policy blob map from archive contents so an export-ignored document can exist at
the same commit while its archive entry is absent. Cover regular hidden rules and an
entire hidden `.review` directory; genuine absence; policy malformed/type/size/read
errors; exact commit pinning even when default HEAD moves; no publication on failure;
no network in transactions; exact request metrics on success and error. Add GitHub
MockTransport tests for trees, blob, missing versus 404, truncation, strict size limits,
and absence of unnecessary AGENTS reads. Test the version bump at the same commit.

PostgreSQL evidence must compare real normalized files, symbols, edges and import
records for incremental versus fresh full at one target commit with hidden rules.
Keep a scenario where policy is unchanged across the commit transition (so the first
build really is incremental), and a policy-change scenario proving full fallback.
Assert excluded files and storage counts do not enter the new publication. Include
no-document and error cases preserving the previous publication.

Executed before changes:

```text
uv run pytest tests/test_build_repository_index.py tests/test_github_snapshot_source.py tests/test_code_index_tar_archive.py
245 passed in 12.98s; 0 skipped
Python 3.13.4; pytest 9.1.1; uv 0.12.7
```

The first attempt failed opening the default uv cache; an authorized rerun
in a suitable environment created the worktree `.venv` and produced the result above. No source
changes were made. Dedicated PostgreSQL baseline is **pending**: the coordinator
confirmed that the local Docker daemon was unavailable.
Do not interpret optional integration skips as evidence. Do not start infrastructure
as part of this planning-only phase.

After approval, use a dedicated disposable database, never a database shared by
parallel runs (cross-run NOTIFY interference). Run before and after implementation:

```bash
TEST_DATABASE_URL=<dedicated-db> uv run pytest -m integration -rs tests/test_code_index_indexer_postgres.py
```

Also run code-index store/queue integration suites as appropriate; report exact pass
and skip counts, require zero skips for the dedicated code-index integration run.
Final mandatory gates (not run by this planning-only change):

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy .
uv run pytest
```

Run existing review policy tests to prove reused semantics stay intact and
`test_code_index_layers.py` to ensure no provider imports enter application code.
OpenAPI and Postman collection changes are unnecessary for this internal worker port.

## Approved decisions and remaining gates

- User approved authoritative reads (+1 to +3 calls), coordination comments, and the
  #144 handoff on 2026-10-23. All ownership comments are posted, including later PR #241.
- Keep existing RELEASED/`policy_*` behavior, without a new index retry policy.
- Dedicated PostgreSQL infrastructure is available; baseline and Task 4 evidence are
  complete. Final full-suite execution and independent full-diff review remain.
- Coordinator will post #144 handoff after the PR is published; the canonical main
  link becomes available only after merge. No external human approve or merge occurred.

Implementation order and acceptance checkpoints are in [207-todo.md](207-todo.md).


## Implementation evidence: first policy boundary slice (2026-10-23)

- User approval and ownership comments are complete. Dedicated PostgreSQL baseline:
  41 passed, 0 skipped in 46.86s at `66077f2` (coordinator run).
- Adapter RED: 25 regressions failed because `rules_document` was absent. Initial GREEN:
  110 passed. A subsequent malformed-listing slice produced 6 meaningful RED failures
  (recursive paths, dot paths, duplicate names, missing or inconsistent modes); the
  index adapter now rejects those listings without changing the review reader.
- Build RED: 8 regressions failed for hidden rules/parent in full and incremental builds,
  substituted archive rules, unreadable policy preserving publication, pinned true
  absence, and the unsafe v1 same-commit fast path. All now pass. Policy has its own
  fake object map and commit log; archive scan captures only `go.mod`.
- GREEN: 381 passed, 0 skipped in 11.44s across build, snapshot adapter, exclusions,
  architecture layers, archive, review document loader, GitHub policy reader, and ignore
  parser suites. `uv run ruff check .`, `uv run ruff format --check .`, and `uv run mypy .`
  pass (518 checked source files). `git diff --check` passes.
- Real HTTP evidence is provided by the separate network-proof runner; no provider
  credentials or external services are used by this development slice.
- PostgreSQL fake compatibility and literal HTTP counters were updated, but Task 4
  scenarios and post-change PostgreSQL execution are deliberately pending independent
  boundary review. Repository documentation Task 5 and final full-suite gate remain.
- No dependency, migration, commit, or push. No sandbox approval rejection occurred.


### First review corrections

- Standards: replaced the new hidden-policy fixture's production-derived digest with a
  literal SHA-256 of the documented canonical payload, independently calculated.
- Spec: HTTP `Retry-After` could raise `ValueError` for 5,000 digits or `OverflowError`
  for 30 digits during shared policy error classification. The narrowly scoped shared
  parser now validates ASCII decimal seconds, catches the integer digit limit, and
  caps a valid delay at one day, matching the existing snapshot adapter semantics.
  Ordinary HTTP classifications and delays remain unchanged within that bound.
- RED: 7 failed, 3 passed for tree/blob oversized headers and direct review-policy
  source cases. GREEN: 270 passed in 8.69s across snapshot, existing policy source,
  and index build suites. Ruff, format, and mypy remain green. Task 4/5 still pending.


## Task 4/5 completion evidence (2026-10-23)

The first boundary review and its correction review report Standards 0 / Spec 0.
The working branch was safely rebased onto `5d183fe` with all uncommitted artifacts
preserved. PostgreSQL now separates original commit trees from export-filtered archives.
New cases cover hidden rules and hidden parent, unchanged-policy true incremental
parity of files/symbols/edges/imports against fresh full, changed ignored source absent
from row/storage counts, changed-policy full fallback, genuine absence, error preservation,
and same-commit v1 rebuild. No unsafe implementation was temporarily restored.

Dedicated indexer result: **51 passed, 0 skipped in 59.51s**. The initial new-test run
had 49 passed / 2 failed because a test expected an unchanged ignored source to be
re-examined by incremental. The corrected scenario changes that source while keeping
policy bytes unchanged, so it proves both incremental mode and exclusion counter 1.

Canonical docs added at `docs/SYSTEM_DESIGN.md#index-row-contract`, linked from CVS
§3.5 and ADR §3. RFS §5/ADR §9 now require complete pinned Git tree evidence; SD and
PIPELINE share the conditional 5/6/7 full, 6/7/8 incremental, 2 unchanged request table.
Actual #144 comment is pending the coordinator's publication handoff.

Post-rebase lint, format (529 files), and mypy (526 source files) pass. Full code-index
integration and final full-suite validation are running serially on dedicated services.


## Final review and dedicated integration evidence

Independent full-feature Standards and Spec reviews both report **0 findings**, including
an incremental check of the queue HTTP-fake compatibility correction. The first complete
code-index integration run had 162 passed / 1 failed because the existing queue stand-in
lacked the new root-tree read. After adding a complete empty tree route and its literal
expected request, that exact queue case passed in 4.98s. No production change was needed.

Final dedicated integration command (PostgreSQL and RabbitMQ both configured):

```text
uv run pytest -m integration -rs tests/test_code_index* -q --tb=short
163 passed, 0 skipped, 298 deselected in 158.78s
```

Ruff lint, Ruff format check (529 files), and mypy (526 source files) pass after that
last test-fixture change. Final full `uv run pytest -ra` is running serially with both
dedicated services and private JUnit evidence.

Publication responsibilities: after the PR URL exists, the coordinator/publisher posts
the authorized #144 canonical-contract handoff. Human current-head approval, resolved
review threads, up-to-date main at merge, and the tech lead's issue acceptance/closing
are external pre-merge requirements. None is reported as an event that already occurred.


## Final development handoff proof (2026-10-23)

All required final gates pass on base `5d183fe` with the reviewed diff:

```text
uv run ruff check .                       All checks passed
uv run ruff format --check .              529 files already formatted
uv run mypy .                            526 source files, no issues
uv run pytest -ra --junitxml=<private-output>/pytest.xml
5136 passed, 0 skipped, 2 warnings in 1037.04s (17m17s)
```

The full pytest command used both dedicated `TEST_DATABASE_URL` and
`TEST_RABBITMQ_URL`; tests ran serially. XML verifies 461 code-index test cases,
zero failures/errors/skips, and zero skipped cases across the complete suite.
The two existing dependency deprecation warnings concern Starlette/httpx and AnyIO's
BlockingPortal alias; no dependency was added or changed by this feature.
Dedicated code-index integration separately passed 163 / 0 skipped in 158.78s;
the new indexer suite passed 51 / 0 skipped in 59.51s. Network-proof runner evidence
(coordinator): 7 cases / 14 actual HTTP requests passed after the policy retry fix.

Independent final full-feature review: Standards 0 / Spec 0, including the queue fake
compatibility correction. `git diff --check` passes. No commits or pushes by development.
Implementation is ready for publisher handoff. At this development handoff, external
#144 comment, human approve/thread resolution and merge have not occurred; they remain
the publication and pre-merge responsibilities described above.
