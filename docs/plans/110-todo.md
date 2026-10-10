# Task List: rollback database revision guard (#110)

Plan: [110-plan.md](110-plan.md). Status: approved by the user (“делай”, 2026-10-10).
Use the isolated worktree; the original checkout contains unrelated dirty #111 work.
For each task record RED/GREEN evidence and run the relevant focused tests. A checked task must
also have all project gates green: `uv run ruff check .`, `uv run ruff format --check .`,
`uv run mypy .`, `uv run pytest`.

## Task 1: target-image revision admission checker

**Description:** Supply a typed read-only checker through stdin to historical target-image
Python, independent of application imports; inspect the target Alembic graph and current DB.

**Acceptance criteria:**

- [x] Tests first fail for same-head/ancestor admission and unknown/unrelated/multi-head or
      untracked DB refusal, using literal graph IDs and fake ports/connection objects.
- [x] Exactly one DB revision and target head are required; recognized ancestor path is
      established; missing/empty version table, graph/query/connect errors fail closed.
- [x] Connection is read-only and bounded, no migration/DDL is executed, and errors emit
      controlled text without secret values, URLs or tracebacks.

**Verification:** `uv run pytest tests/test_rollback_revision.py`; test secrets in error paths;
run all four uv gates before marking done.

**Dependencies:** Human approval. **Estimated scope:** Small, 2 files.
**Files likely touched:** `deploy/scripts/check-rollback-revision.py`,
`tests/test_rollback_revision.py`.

**Task 1 evidence (2026-10-10):** Approved public entry point `main` accepts injected graph
and revision-reader boundaries; production defaults inspect the target's `/srv/alembic.ini`
and database, independent of application imports.

| Slice | RED | GREEN |
| --- | --- | --- |
| Same target head | 1 failed: checker source absent | 1 passed |
| Recognized ancestor | 1 failed, 1 passed: ancestor refused | 2 passed |
| Unknown/unrelated, zero/multiple target heads, zero/multiple DB revisions | 6 failed, 2 passed | 8 passed |
| Graph construction/traversal and query failures with multiline URL/PEM canaries | 5 failed, 8 passed: exceptions/output escaped | 13 passed |
| Default DB reader: read-only/time bounds, closure/disposal, connect/query failure, absent/empty table | 5 failed, 13 passed: default reader absent | 18 passed |
| Actual Alembic fixtures: same/ancestor/unknown, multihead, missing parent, cycle, duplicate, empty graph | 8 failed, 18 passed: default graph absent | 26 passed |
| Source on stdin and third-party logging containment | 6 failed, 25 passed: CLI returned success without probing and log canaries escaped | 31 passed |

The DB probe uses libpq `default_transaction_read_only=on`, five-second connection,
statement and lock timeouts, a ten-second idle-transaction timeout, and `NullPool`.
Connection context closure rolls back the read transaction; engine disposal runs in `finally`.
The Alembic fixtures make `env.py`, upgrade and downgrade raise if executed. No application
module, bootstrap or migration command is called. Config/driver errors, missing historical
prerequisites and malformed graphs return controlled failure without raw exception text,
URLs, stdout/stderr diagnostics or logging canaries. Invalid/absent DB configuration also
fails closed. No safe identifiers need to be printed, so revision strings never enter output.

Verification: focused `uv run pytest tests/test_rollback_revision.py`: **31 passed**.
`uv run ruff check .`: pass (initial import sorting findings fixed only in Task 1 files).
`uv run ruff format --check .`: pass, 348 files. `uv run mypy .`: pass, 345 source files.
`uv run pytest`: **2288 passed, 128 skipped, 2 existing third-party deprecation warnings**
in 74.42 seconds. Default integration/environment-dependent tests skipped without configured
`TEST_DATABASE_URL`/`TEST_RABBITMQ_URL` and optional repository checks; Task 1 adds no skipped
tests. No live PostgreSQL/two-image verification claimed at this stage. Task 1 implementation
was complete at the Task 1 checkpoint; review outcome is recorded below.

Independent Task 1 review: **Standards 0 findings; Spec 0 findings**. Coordinator authorized
Task 2 to proceed after that review.

## Task 2: rollback preflight before mutations

**Description:** Pull and probe the target before rollback changes persistent files or services.
Keep explicit/default target selection, manual/auto state rules and bootstrap fallback.

**Acceptance criteria:**

- [x] RED tests execute real rollback script with controllable fake Docker, in manual/auto
      modes and explicit/default targets; checker failure preserves `.env`, all env files,
      state and previous bytes and makes no compose rm/up/down call.
- [x] GREEN uses target IMAGE override plus `run --rm --no-deps -T bootstrap python -` with
      source stdin; no dependencies start; checker precedes writes and recreation; pull/probe
      failures return nonzero with safe errors, no success message and unconditional cleanup.
- [x] Compatible rollback retains existing password/secret/state semantics; no-previous
      bootstrap restoration still works and bypasses application probe/diagnostics.

**Verification:** `uv run pytest tests/test_deploy_staging_services.py tests/test_rollback_revision.py`;
inspect ordered fake call log and file snapshots; run all four uv gates.

**Dependencies:** Task 1. **Estimated scope:** Medium, 3 files.
**Files likely touched:** `deploy/scripts/rollback.sh`, `tests/test_deploy_staging_services.py`,
`tests/test_rollback_revision.py` if behavior tests need additional fixtures.

**Task 2 evidence (2026-10-10):** Initial refusal matrix was RED: **16 failed** for
manual/auto × previous/explicit target × pull/probe/noisy-probe/false-success failures.
The old script wrote `.env`, recreated missing role files, or accepted a failed probe.
After implementation, the refusal matrix plus existing missing-role-file recovery were
GREEN: **17 passed**. Full focused deployment/checker regression: **93 passed**, including
four additional accepted-path regressions proving target/source stdin, pull→probe→up order,
old `.env` and absent worker file at probe time, secret preservation and manual/auto state rules.
A temporary test assertion accidentally matched the permitted `--rm` probe flag as an `rm`
service action; corrected it to compare command tokens before recording GREEN.

Pull/probe output is captured; only exact controlled checker refusals are displayed, and
unexpected output (including zero-exit noise) fails closed. Refusals preserve every host file
byte, missing-file state and permissions, make no service up/rm/down/exec call, emit no success
or secret/PEM canary, provide operator next action, and always clean registry/probe temp files.
Existing no-previous bootstrap restoration bypasses application admission as before.

Real Compose v2.19.1 evidence: `config bootstrap` on the full staging config with role files
missing failed (exit 14), confirming filtering cannot safely avoid early env-file creation.
The implemented temporary bootstrap-only configuration rendered successfully without those
files and joined only the existing external project network. An actual stdin checker probe
with image A (`sha256:81c69606c3cda7f35c20bb0de5f8dc97b20a24a134259491e6a05f4777bfc937`)
ran alongside a current dummy API and PostgreSQL in unique project
`rollback110-probe-e563df2e`: exact compatible stdout, empty stderr, API/PostgreSQL IDs unchanged.
`COMPOSE_IGNORE_ORPHANS=true` suppresses warnings from current services without removing them.
The isolated version-table fixture used target head `20261007_0028`; this is a Task 2 probe
check, not the two-image/full-schema acceptance verifier of Task 5. Its project, network and
volumes were cleaned. Two preceding harness setup attempts (dummy API missing a healthcheck;
overly strict rendered-network dict comparison) also cleaned their own projects.

Task 2 gates: `uv run ruff check .`, `uv run ruff format --check .` (348 files), and
`uv run mypy .` (345 source files) pass. Full `uv run pytest` against the coordinator-created
disposable PostgreSQL/RabbitMQ services with both test URLs set: **2436 passed, zero skipped,
2 existing third-party deprecation warnings**, 160.03 seconds. No shared staging resources
used. Task 2 implementation is complete and awaits independent review; Tasks 3–6 remain
untouched. The coordinator retains ownership of disposable gate-service cleanup.

Independent Task 2 review: **Standards 0 findings; Spec 0 findings**. The coordinator approved
the Tasks 1–2 checkpoint and authorized Task 3.

## Checkpoint after Tasks 1–2

- [x] Refusal boundary and graph restriction reviewed independently.
- [x] All four uv gates pass; no application/production-migration scope drift.

## Task 3: successful rollback runtime environment names

**Description:** Print safe key-only diagnostics from running API and both workers in the
shared success path so automatic and manual jobs receive identical evidence.

**Acceptance criteria:**

- [x] RED tests execute the diagnostic payload using multiline PEM and secret canaries,
      dollar signs, backslashes and unusual/newline keys; every value and PEM line is absent.
- [x] GREEN collects `os.environ.keys()` with validated names and sorted JSON output from
      `compose exec -T` for `api`, `worker`, `webhook-worker`; includes each service label.
- [x] Diagnostics happen only after successful application `up --wait`, before completion
      metadata/success message; failed admission and bootstrap fallback do not run them;
      missing expected service is a clear failure, never raw env output.

**Verification:** `uv run pytest tests/test_deploy_staging_services.py`; inspect captured
stdout/stderr from both modes and explicit diagnostics failures; run all four uv gates.

**Dependencies:** Task 2. **Estimated scope:** Small, 2 files.
**Files likely touched:** `deploy/scripts/rollback.sh`, `tests/test_deploy_staging_services.py`.

**Task 3 evidence (2026-10-10):** Diagnostics run the exact embedded Python payload through
fake Docker into a real Python process with a synthetic container environment. Names are read
from `os.environ.keys()`, validated as ASCII shell identifiers and emitted as sorted JSON for
`api`, `worker`, `webhook-worker`. Values are never read by the payload. Invalid names containing
newlines, quotes, hyphens or Unicode are omitted; ordinary lowercase names remain valid.

| Slice | RED | GREEN |
| --- | --- | --- |
| Running-container names, sorted JSON, labels and order in both modes | 2 failed: no diagnostics | Included in combined 26 passed |
| Exec failure/missing service, noisy zero-exit output, malformed name list, empty output × all three roles × both modes | 24 failed: unsafe/unvalidated output or missing controlled error | Combined 26 passed |
| Completion failure operator guidance after stack recreation | 2 failed: preflight recovery hint wrongly reused | Full 24-case failure matrix passed |
| Failed up and no-previous bootstrap avoid diagnostics/success metadata | Bootstrap assertion initially matched legitimate `exec nginx` inside `docker run`; corrected to inspect Compose actions only | 4 passed |

Hostile test values include multiline PEMs, `$`, backslashes, secret canaries and invalid keys.
Captured stdout/stderr contains no value or PEM line. Shell validation accepts only a strict
JSON list of valid names; failed exec or unexpected output emits a fixed service-specific
completion failure and explains that the target stack may already be running. Operators are
directed to inspect running services and retry diagnostics. This is distinct from the preflight
refusal's compatible-image/database-recovery guidance. Current/previous state and success output
are withheld on diagnostics failure. All diagnostics follow successful `up --wait` and precede
metadata; refused admission and no-previous bootstrap restoration never execute them.

The test helper sets its fake process environment immediately before executing the payload to
avoid macOS inserting `__CF_USER_TEXT_ENCODING` during `execve`. The expected ASCII order was
corrected to put `AUTH_JWT_PRIVATE_KEY` before `A_KEY`. These were fixture/expected-value fixes.
The first focused run had 89 passes and two bootstrap-assertion failures; the first full gate
was interrupted after the same known failures. Final exact gates on corrected files all pass:
`uv run ruff check .`; `uv run ruff format --check .` (348 files); `uv run mypy .` (345 source
files); `uv run pytest` with authorized disposable test URLs: **2465 passed, zero skipped,
2 existing third-party deprecation warnings**, 199.48 seconds. All 91 deployment regressions
passed within that final full gate, in addition to the focused 26/24/4-case successful runs.
`bash -n deploy/scripts/rollback.sh` and `git diff --check` also pass. Task 3 implementation
is complete. Independent Task 3 review: **Standards 0 findings; Spec 0 findings**. The
coordinator authorized Task 4 after that review.

## Task 4: distribute the preflight with workflows

**Description:** Ship host checker source wherever the rollback script is uploaded and prove
manual and automatic workflow routes retain failure/promotion behavior.

**Acceptance criteria:**

- [x] RED upload-contract tests require checker in both CI/CD and Rollback staging manifests;
      fixture host copies checker as the real deployment does.
- [x] GREEN adds checker distribution without changing secret bundles, privileges, shared
      concurrency, first-deploy bootstrap fallback, or unrelated host provisioning.
- [x] Failed manual rollback cannot advance read-image/health/promotion; failed auto rollback
      remains visible while forward deployment job fails and staging tag is not promoted.

**Verification:** `uv run pytest tests/test_deploy_staging_services.py`; review ports and edge
workflow paths plus both automatic entry points; run all four uv gates.

**Dependencies:** Tasks 1–3. **Estimated scope:** Medium, 3 files.
**Files likely touched:** `.github/workflows/ci-cd.yml`, `.github/workflows/rollback.yml`,
`tests/test_deploy_staging_services.py`.

**Task 4 evidence (2026-10-10):** Upload-contract tests were RED: **2 failed** because
CI/CD and the manual edge manifest omitted the checker; adding its source made both GREEN.
A Terraform/ports stale-host regression was RED: **1 failed** because the manual workflow
had no script refresh on that path. A ports-only upload of the current rollback script,
checker and unchanged env-file helper, plus restoring the executable bit in the shared SSH
step, made the combined three tests GREEN. The regression then covered both accepted and
refused admission, executing the workflow's actual SSH payload against the host fixture.
It replaces an old unguarded script and absent checker without refreshing Compose files,
secrets or provisioning. The fixture now copies the ports overlay as the real host does.

Additional workflow regressions execute the actual promotion/failure shell payloads for
failed deploy/health combinations, with either failed or successful automatic rollback.
They require `promote=false` and a failed pipeline, retaining an explicit rollback-failed
message when appropriate. Manual read-image/health steps retain implicit success gating and
the promotion job depends on rollback success. Main-only execution, shared concurrency,
SSH authentication/fingerprint fields and existing edge Caddy validation remain intact.
The focused workflow/Caddy selection is GREEN: **9 passed**, 90 deselected. Required shared
file ownership coordination was posted on PR #125 before edits; its original checkout and
env-file helper source remain unchanged. No dependencies were added.

Task 4 exact gates: `uv run ruff check .` passes; `uv run ruff format --check .` passes
(348 files); `uv run mypy .` passes (345 source files). Full `uv run pytest` with both
authorized disposable integration URLs: **2473 passed, zero skipped, 2 existing third-party
deprecation warnings**, 189.10 seconds. All 99 deployment tests pass within that full gate.
`git diff --check` passes. The coordinator rechecked open PR ownership after implementation:
only still-open #125 overlaps, and its existing coordination comment applies. Task 4 is
complete and awaits independent review. Task 5 has not started; the coordinator owns cleanup
of the disposable gate services.

Independent Task 4 review: **Standards 0 findings; Spec 0 findings**. The coordinator approved
the Tasks 3–4 checkpoint and authorized Task 5.

## Checkpoint after Tasks 3–4

- [x] Independent Standards/Spec review finds no rollback-order or value-leak defect.
- [x] All four uv gates pass; open-PR ownership checked again before shared-file edits.

## Task 5: reproduce compatible/incompatible behavior with real images

**Description:** Add and execute an opt-in Docker verifier using unique disposable resources,
A baseline image and B derived with synthetic child migration N; use the approved plan recipe.

**Acceptance criteria:**

- [x] Add test-first assertions for resource isolation and refusal invariants; fixture child
      migration exists only in test image B, with no edit to merged revisions.
- [x] Real B-at-M fixture skips only B bootstrap; actual rollback to A succeeds in manual
      and auto modes with healthy API/worker/webhook-worker and safe names. B is then deployed
      normally to N, and A is rejected in both modes preserving B IDs/files/revision/data.
- [x] Successful auto mode preserves previous-state rules; logs contain no synthetic secrets;
      report records immutable images, DB revisions, IDs/hashes, outcomes and cleanup.

**Verification:** Run the verifier via `uv run` if Python, or `bash` if shell with any host
Python invoked through `uv run`; use escalated Docker only for isolated local resources.
Never use shared staging DB, volumes or network. Retain a redacted evidence report;
run all four uv gates.

**Dependencies:** Task 4. **Estimated scope:** Medium, 3–5 files.
**Files likely touched:** `scripts/verify_rollback.sh` (or typed Python equivalent),
`tests/fixtures/rollback/compose.yml`, `tests/fixtures/rollback/new_revision.py`,
`tests/test_rollback_verifier.py` if meaningful isolated safety tests are needed,
`docs/plans/110-verification.md`.

**Task 5 evidence (2026-10-10):** Opt-in `scripts/verify_rollback.py` runs actual deploy and
rollback scripts against the unmodified staging base plus a local internal-network/no-port
override. It builds only derived B with `tests/fixtures/rollback/new_revision.py`; production
Alembic files remain untouched. Existing prepared A and pinned store images are reused.

| Slice | RED | GREEN |
| --- | --- | --- |
| CLI collision/daemon-error isolation, no Docker mutation | 4 failed: verifier absent | 4 passed |
| File/container/revision/sentinel corruption and unexpected refusal/success/key evidence | 9 failed: invariant/evidence boundaries absent | 13 combined passed |
| Verified local-image pulls, real Compose forwarding and stdin | 1 failed: wrapper absent | 14 combined passed |
| Cleanup timeout or secret-output failure must not interrupt other owned removals | 2 failed: cleanup raised before remaining operations | 16 combined passed |

First live attempt failed at normal A deploy because this machine's Compose plugin exists only
under its original Docker config; the script's private registry config hid it. Its owned
resources/B tag were cleaned, and the private fixture/transcript was removed after sanitized
diagnosis. The wrapper now exposes that same plugin in private configs without copying login
credentials; tests verify the symlink and unchanged real-operation/stdin forwarding. The
production rollback checker/output validation was not weakened, and no production fix was
needed. Fixture failures and this local substitution are documented in the retained evidence.

Fresh project `rollback110-5c4d8263f4564562` passed all real cases with Compose 2.19.1. A's
actual head M=`20261007_0028`; B's test-only head N=`20261010_0110`. Both compatible B-at-M
manual/auto rollbacks return 0, preserve M, report exact sorted role keys, and restore healthy
API/worker/webhook-worker; manual previous becomes B, auto previous remains A. Normal B deploy
runs bootstrap to N, then both modes refuse A with exit 1 and controlled unknown-revision
reason. File hashes/modes, all application/store/bootstrap IDs/images/status/health, revision N
and nonsecret sentinel are identical after each refusal. A separate empty internal network
proves unreachable DB refusals in 9.82/9.34 seconds without stopping primary PostgreSQL; offline
files and healthy primary B remain unchanged. Literal API HTTP 200 JSON and worker heartbeat
age ≤30 seconds are checked throughout. All captured output passes synthetic-secret/full-PEM/
individual-PEM-line scans. No provider calls or synthetic webhook deliveries occur.

Redacted full IDs, snapshots, key lists and cleanup results are retained in
[110-verification.json](110-verification.json), with recipe/limitations in
[110-verification.md](110-verification.md). Cleanup passed for invocation projects/volumes,
offline network, probes, B tag and successful private temp root. Prepared A and coordinator
gate services remain available. Local immutable-image pull substitution is explicitly scoped
and recorded; this does not claim registry verification.

Task 5 exact gates all pass: `uv run ruff check .`; `uv run ruff format --check .`
(351 files); `uv run mypy .` (348 source files); full `uv run pytest` with the separate
authorized disposable database/broker URLs: **2489 passed, zero skipped, 2 existing
third-party deprecation warnings**, 202.68 seconds. Focused verifier tests: **16 passed**.
`git diff --check` passes. Task 5 is complete and awaits independent review; Task 6 remains
untouched. No commit or push has occurred.

Independent Task 5 review: **Standards 0 findings; Spec 0 findings**. The coordinator also
confirmed invocation containers/networks/volumes were gone, and authorized Task 6.

## Task 6: document runbook and finish delivery

**Description:** Explain admission, refusal recovery, guarantee limits and diagnostics; collect
final gates/review evidence and hand the full change to the publishing agent.

**Acceptance criteria:**

- [x] CICD §5 explains known-ancestor/single-head admission and safe refusal before rollback
      mutation, distinction from already-failed forward deploy, compatible image selection,
      forward corrective release, backup/manual DB recovery procedure when necessary, and
      explicitly no automatic downgrade or guaranteed application compatibility.
- [x] SECRETS logging section demonstrates running-container key JSON for all three roles,
      safe PEM handling and verified absence of values; document unique verifier invocation,
      two-image controlled B-at-M case and any local-pull wrapper limitations.
- [x] All AC evidence is recorded and final four uv gates pass.
- [x] Final independent Standards/Spec review has zero findings, branch rebased on main,
      draft PR published with required sections and
      `Refs #110`, without closing keyword or Development-panel link.

**Verification:** Run all four exact uv gates and relevant disposable integration suite;
review complete diff and evidence report against issue #110; publisher records PR URL and
attaches it to this chat. Do not merge or close the issue.

**Dependencies:** Task 5. **Estimated scope:** Medium, 4 files.
**Files likely touched:** `docs/CICD.md`, `docs/SECRETS.md`, `docs/plans/110-todo.md`,
`docs/plans/110-verification.md`.

**Task 6 evidence (2026-10-10):** CICD §5 now describes target-image read-only admission,
supported equal/ancestor state and fail-closed cases, pre-mutation refusal, manual/auto delivery,
the separate bootstrap fallback, concurrent-migration and application-compatibility limits,
no downgrade, compatible/forward-release selection and coordinated backup/manual DB recovery.
It distinguishes post-up completion/diagnostics failure from preflight refusal and documents
the opt-in isolated two-image recipe with local pull/plugin substitutions and actual evidence.
SECRETS §5 shows the three exact runtime key lists from the retained report, validates names/
JSON, explains multiline PEM safety and controlled failures, and forbids env/config/full-inspect
logging. Existing #111-related configuration and helper source remain untouched; prior PR #125
ownership coordination covers these docs. Documentation changes required no new tests or live
rerun; source and previously reviewed acceptance evidence are unchanged.

All four final exact gates pass: `uv run ruff check .`; `uv run ruff format --check .`
(351 files); `uv run mypy .` (348 source files); `uv run pytest` with authorized separate
disposable database/broker URLs: **2489 passed, zero skipped, 2 existing third-party deprecation
warnings**, 186.54 seconds. `git diff --check` passes. Task 6 implementation/verification is
complete. Fresh full-issue independent review, rebase confirmation and publisher delivery remain
pending; no commit/push was made by the development agent. The coordinator owns gate-service
cleanup and the next review/publishing steps.

Final full-issue review: **Standards 1 P3 finding; Spec 0 findings**. The new B fixture lacked
the backend rule's module-wide `from __future__ import annotations`. Added that import after
its docstring; no production migration or dependency changed. This review failure is the RED
evidence for the correction; no tautological import test was added. Because the fixture bytes
are B image input, the six-case live verifier and all four gates were rerun before fresh
full-issue review. Prior live proof remains recorded above as history; the retained report is
refreshed with the corrected B's actual IDs and snapshots.

Correction GREEN evidence: fresh project `rollback110-e89710f7208d4dcc`, B
`sha256:0529cbdd23ae63e956230c67818e8dd1cdf32707d7defa5cc19f5650cb21a68c`, same prepared A and
M/N, passed all six actual manual/auto compatible/incompatible/unreachable cases. Snapshot
invariants, exact runtime keys, secret/PEM scans and scoped cleanup passed. Unreachable cases
completed in 10.78/9.11 seconds; JSON/Markdown evidence now describes this latest corrected
fixture run. All four exact gates pass again: lint; format (351 files); mypy (348 source files);
full pytest with separate authorized disposable test URLs: **2489 passed, zero skipped,
2 existing third-party deprecation warnings**, 193.20 seconds. No new test mirroring the import
was added. The P3 finding is corrected; fresh final review and publishing remain pending.

Final repeat review found a **P2 mutable-tag race**: the admitted selector could move before
Compose's second pull or later registry promotion. Necessary AC correction resolves a strict
local image ID after the single pull, selects only one matching-repository immutable digest
for tags and verifies its local ID, pins probe/all four application roles to that ID with
rollback up `--pull never`, and persists immutable RELEASE_REF in env/current state. Explicit
repo@digest strings and manual/auto history rules are preserved; local IDs remain supported
for the verifier. No host Python, dependency, shared pull-policy or workflow permission change.

Correction RED/GREEN: modeled tag movement × manual/auto × default/explicit target was
**4 failed → 4 passed**. Strict metadata coverage exposed tagged RepoDigest output acceptance
in two cases (**2 failed, 31 passed → all 33 supported/refusal cases pass**); candidates now
require plain matching repo@digest, ambiguity/missing/malformed/wrong-repository/mismatched IDs
fail before files or services change, without secret output. Wrapper duplicate `--pull` was
**1 failed → 1 passed**. Legacy fixtures now use valid full digest inputs and distinct config
IDs. Extracted manual promotion pulls/tags the canonical A digest even after the original tag
moves. Full focused deploy/verifier/revision suite: **183 passed**, 104.68 seconds. Live proof
and the exact four gates were rerun with the new host rollback before fresh final review.

P2 correction live GREEN: fresh project `rollback110-26092e3b17624e53`, prepared A reused and
B derived again from the unchanged corrected fixture, actual ID
`sha256:45bdccc6965cf85027e2b7a7be7ca8c5b07386736e7975554574c6af30ab1057`. All six real cases,
runtime image IDs/health, mode history, refusal snapshots/revision/sentinel, exact role keys,
secret/PEM scans and cleanup pass. Offline cases complete in 9.45/9.12 seconds. The retained
JSON/Markdown now contains this latest proof. This live run validates local ID pinning; registry
tag movement/canonicalization/promotion is behavioral coverage, without a private registry claim.
All exact gates pass: `uv run ruff check .`; `uv run ruff format --check .` (351 files);
`uv run mypy .` (348 source files); full `uv run pytest` with authorized separate disposable
integration services: **2526 passed, zero skipped, 2 existing third-party deprecation warnings**,
220.45 seconds. Bash syntax and `git diff --check` pass. The P2 finding is corrected. Fresh final full-issue independent review completed with
**Standards 0 findings; Spec 0 findings** across source, docs and retained proof. The coordinator
independently audited the latest redacted JSON and confirmed invocation Docker resources were
removed. Publication remains pending at this checkpoint; no commit or push was made.

## Final checkpoint

- [x] Every task above completed with RED/GREEN evidence and no unresolved review findings.
- [x] `uv run ruff check .`
- [x] `uv run ruff format --check .`
- [x] `uv run mypy .`
- [x] `uv run pytest`
- [x] Real isolated two-image/PostgreSQL acceptance results and secret scan retained.
- [x] Draft PR published and attached; verified URL recorded for user delivery.
      Tech-lead acceptance remains external.

## Publishing preflight (2026-10-10)

The publisher fetched `origin/main`; it remains the reviewed base
`92250a1fb3b65f88a2ea111d33613f9a4af1d1d2`. Open-PR file ownership was rechecked: only #125
overlaps and its prior coordination comment applies; no implementation changes were made.
The draft body is prepared with What / Why / How to verify / Refs and the required checklist.
Final independent Standards/Spec review is 0/0; final gates and six real cases above apply.
Commit, rebase confirmation, push, publication and attachment are pending.

## Published draft (2026-10-10)

Draft PR [#126](https://github.com/larchanka-training/dmc-268-api-t6/pull/126) is open on
`fix/110-rollback-db-revision` against `main` and attached to this chat. Implementation commit:
`303499d2f9813ea137d59d6434f844ddb03ff474`. `git rebase origin/main` confirmed the branch was
already up to date with fetched base `92250a1fb3b65f88a2ea111d33613f9a4af1d1d2`. The published
body has all required sections/checklist and `Refs #110`; GitHub reports no closing issue refs.
No issue Development-panel link was added. No merge or issue closure was performed.

The coordinator confirmed the separate disposable pytest PostgreSQL/RabbitMQ containers were
removed after final gates; the live verifier invocation resources were independently confirmed
absent. Prepared A and pinned store images were preserved. Final source, test and retained proof
bytes are unchanged after their 0/0 review and passing gates; this follow-up updates only delivery
metadata. Current-head approving review and tech-lead acceptance remain external requirements.
