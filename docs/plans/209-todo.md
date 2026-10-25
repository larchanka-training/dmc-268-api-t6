# Task list: #209 token-cache wiring and local fallback

Base: `22c621abc80abdd1b3daaa95bde73b6aaf81c32e`.
Plan: [209-plan.md](209-plan.md). Initial implementation and publication evidence is recorded below; review round 1 code/evidence reviews and local gates pass; publisher handoff is ready.

## Planning gate

- [x] Read issue, current composition/cache/tests, repository rules and required CI.
- [x] Record clean baseline: exact issue command, 50 passed / 0 skipped / 8.22 seconds.
- [x] Check open PR overlaps; select files without an active overlap.
- [x] Human reviews and approves the saved plan before development starts (2026-10-24).

## Task 1: Protect production worker cache selection

**Description:** Add deterministic tests that execute production startup through cache
construction, covering configured Redis and absent/empty URLs in both worker entrypoints.

**Acceptance criteria:**

- [x] Real `run_worker` -> real `github_adapters` and real webhook `run_forever` reach their
      provider constructor; configured cases select Redis cache with the exact fake client.
- [x] Absent and empty URLs never call the recording Redis constructor and select memory;
      startup stops only after construction, with owned resources cleaned up.
- [x] e1, e2, b3 and b4 each cause their corresponding assertion to fail; the original
      code passes all new tests, without infrastructure and without skips.

**Verification:**

- [x] `uv run pytest tests/test_worker_token_cache_wiring.py`.
- [x] Run four independent mutations as designed in the plan; record failed nodes and
      contract-assertion reasons, then restore/rerun clean tests. Never edit overlapping
      tracked entrypoint files for mutation proof.
- [x] Check the harness does not patch `installation_token_cache` or `github_adapters`.

**Dependencies:** Planning approval; refresh open PR overlaps.

**Files likely touched:** `tests/test_worker_token_cache_wiring.py` (new).

**Estimated scope:** Small (1 file).

### Task 1 execution evidence (2026-10-24)

- Refreshed overlap check: `gh pr list --state open --json number,url,files`;
  #220, #227, #230 and #254 still own the entrypoint/existing worker-test overlaps.
  No open PR owns the new wiring test or these plan files. No external comment posted.
- Added six deterministic startup cases: actual `run_worker` -> actual `github_adapters`
  and actual webhook `run_forever`, each with configured, absent and empty environment
  URLs. Forwarding constructor capture delegates to the real token provider and observes
  the public cache argument; neither cache selection nor `github_adapters` is patched.
  Recorded Redis URL/key activity verifies the returned client, and all owned resources
  and in-process HTTP clients close after the composition sentinel. No external request.
- `uv run pytest tests/test_worker_token_cache_wiring.py`: **6 passed, 0 skipped, 0.87s**.
- Independent fresh-process mutations used the exact substitutions below. The
  self-contained runnable reproduction is in the R1-C section; no temporary file is needed.
  The execution reads the original module, replaces exactly one occurrence,
  executes the compiled replacement in that module's namespace and calls
  `pytest.main(['-q', '--tb=short', 'tests/test_worker_token_cache_wiring.py', '-k',
  '<worker> and (<case>)'])`. It verifies exit 1 and the expected assertion text/count.
  No tracked production source is written; each next process imports the original source.

| ID | Exact runtime-only replacement | Failed node(s) and contract |
| --- | --- | --- |
| e1 | `cache=installation_token_cache(redis),` -> `cache=installation_token_cache(None),` in `app.worker` | `test_review_worker_startup_selects_installation_token_cache[redis]`; Redis cache type assertion fails; **1 failed / 5 deselected** |
| e2 | `cache=installation_token_cache(cast(RedisLike \| None, redis)),` -> `cache=installation_token_cache(None),` in `app.webhook_worker` | `test_webhook_worker_startup_selects_installation_token_cache[redis]`; Redis cache type assertion fails; **1 failed / 5 deselected** |
| b3 | `redis = Redis.from_url(config.redis_url) if config.redis_url is not None else None` -> `redis = Redis.from_url(config.redis_url)` in `app.webhook_worker` | `test_webhook_worker_startup_selects_installation_token_cache[absent]` and `[empty]`; `assert redis.urls == []` sees `[None]`; **2 failed / 4 deselected** |
| b4 | `if settings.redis_url is not None:` -> `if True:` in `app.worker` | `test_review_worker_startup_selects_installation_token_cache[absent]` and `[empty]`; `assert redis.urls == []` sees `[None]`; **2 failed / 4 deselected** |

All nodes above are in `tests/test_worker_token_cache_wiring.py`. The e1/e2 selections
were `review and (redis)` / `webhook and (redis)`; b3/b4 selected `webhook and
(absent or empty)` / `review and (absent or empty)`.

- Immediately after **each** mutant, `uv run pytest
  tests/test_worker_token_cache_wiring.py -q`: **6 passed / 0 skipped** (0.67s, 0.68s,
  0.66s, 0.67s in e1/e2/b3/b4 order). No import, fixture or unrelated startup error
  counted as mutation evidence. Webhook mutant drivers emitted only an anyio
  assert-rewrite warning from importing the production module before pytest initialization.
- `uv run pytest tests/test_worker_token_cache_wiring.py
  tests/test_redis_installation_token_cache.py tests/test_github_installation_adapters.py
  tests/test_worker.py tests/test_webhook_worker_notifications.py`:
  **56 passed / 0 skipped / 2.49s**.
- `uv run ruff check .`: **passed**; `uv run ruff format --check .`:
  **534 files already formatted**; `uv run mypy .`: **531 source files, no issues**.
  Full `uv run pytest` and final repository gates remain Task 3.
- Task 1 independent two-axis review: **Standards 0 / Spec 0 findings, PASS**.
  Reviewer independently reran all four mutants and the clean suite; coordinator
  authorized Task 2 after this review.

## Task 2: Reuse valid local tokens on clean Redis miss

**Description:** Add provider-level regression/edge tests, then change the Redis cache's
clean-miss branch to consult the existing expiring local cache.

**Acceptance criteria:**

- [x] RED first: with Redis reads returning `None` and writes raising, three sequential
      real-provider calls for one installation must issue exactly one GitHub token.
- [x] A cold process-local miss still issues a token; expired/inside-skew local tokens
      require a fresh issuance; valid Redis hits still win over a different local value.
- [x] The minimal fix passes these cases; new assertions/log checks cannot disclose token
      values, including when the fake Redis exception text contains the token.

**Verification:**

- [x] Observe failing three-request regression before changing production code.
- [x] `uv run pytest tests/test_redis_installation_token_cache.py
      tests/test_github_installation_adapters.py tests/test_worker_token_cache_wiring.py`.
- [x] Revert only the new clean-miss behavior in a disposable experiment: observe the
      issuance-count assertion fail, then restore GREEN. Record evidence without tokens.

**Dependencies:** Task 1 (sequential development/review loop).

**Files likely touched:**

- `tests/test_redis_installation_token_cache.py`
- `app/modules/integrations/webhooks/infrastructure/redis_installation_token_cache.py`

**Estimated scope:** Small (2 files).

### Task 2 execution evidence (2026-10-24)

- Read-only overlap refresh: `gh pr list --state open --json number,files` filtered to
  the cache source/test, wiring test and plan files returned **no overlaps**.
- Extended the existing Redis fake with `write_broken`; reads can succeed and return a
  clean miss while writes independently fail. Its write exception deliberately includes
  the fake credential, exercising existing production sanitization.
- **RED before production change:** `uv run pytest
  tests/test_redis_installation_token_cache.py::test_clean_redis_misses_reuse_local_token_when_writes_fail
  -q --tb=short`: **1 failed / 0 skipped / 1.00s**. The contract assertion
  `assert github.posts == [17]` observed **[17, 17, 17]**, proving three sequential calls
  minted three tokens. Only issuance IDs and sanitized warning metadata appeared.
- Minimal production fix: the clean `value is None` branch now returns
  `await self._local.get(installation_external_id)`, exactly as the exceptional-read
  branch already does. Existing expiry, Redis-hit precedence and logging stay in use.
- **GREEN:** the same regression command with `-q`: **1 passed / 0 skipped / 0.63s**.
- Added separate provider controls for a cold miss, valid local fallback one second
  before skew, exact skew boundary, inside skew, expired local data and Redis-hit
  precedence over a different valid local credential. The mutable cache/provider/HTTP
  endpoint clock is deterministic. All new HTTP clients use MockTransport and close.
  New token comparisons and warning-content checks assert precomputed booleans;
  collection assertions expose only counts, installation IDs, cache keys or lifetimes.
  Secrecy checks cover rendered logs, complete saved LogRecord metadata and absent
  exception traceback/text. Existing secrecy tests remain intact.
- **Fallback-revert mutant:** reproduced by the R1-C inline recipe
  (`fallback` case; the initial historical run selected only the write-failure node).
  A fresh process reads the cache module and replaces only
  `if value is None: return await self._local.get(installation_external_id)` with
  `if value is None: return None` (preserving the actual newline/indentation), then
  compiles/executes it in the module namespace without writing tracked source.
  It runs the exact regression node above and verifies pytest exit 1 with the
  issuance assertion observing **[17, 17, 17]**: **1 failed / 0 skipped / 0.60s**.
- Immediate clean rerun: `uv run pytest tests/test_redis_installation_token_cache.py
  tests/test_github_installation_adapters.py tests/test_worker_token_cache_wiring.py`:
  **46 passed / 0 skipped / 0.71s** (19 cache tests, 21 adapter tests, 6 wiring tests).
- `uv run ruff check .`: **passed**; `uv run ruff format --check .`:
  **534 files already formatted**; `uv run mypy .`: **531 source files, no issues**.
  `git diff --check`: **passed**. No worker/webhook composition edit, dependency,
  commit, push or external comment. Full pytest remains Task 3.
- Task 2 independent final-code review: **Standards 0 / Spec 0 findings, PASS**.
  Reviewer independently reproduced fallback-only RED and reran all **25 cache/wiring
  tests, passed**, with no source/test changes requested. Source and tests are frozen.

## Checkpoint after Tasks 1-2

- [x] Focused tests pass without Redis/PostgreSQL/RabbitMQ and none skip.
- [x] All four wiring mutants plus fallback revert have meaningful recorded RED evidence.
- [x] Task review reports zero Standards/Spec findings or feedback has been fixed/reviewed.

## Task 3: Complete verification and review evidence

**Description:** Validate the final minimal diff, document exact RED/GREEN outcomes and
run all repository gates before handing the change to review/publishing.

**Acceptance criteria:**

- [x] All required local gates pass, including the complete pytest run below.
- [x] No new dependency, source composition refactor or uncoordinated overlap entered the diff.
- [x] New tests are discovered by the mandatory Python CI job and require no infrastructure.
- [x] All five mutation results and clean baseline/focused final result are recorded.
- [x] Independent Standards/Spec review has no unresolved findings.
- [x] Required remote CI checks are green before publication is reported complete.

**Verification:**

- [x] `uv run ruff check .`
- [x] `uv run ruff format --check .`
- [x] `uv run mypy .`
- [x] `uv run pytest` (report unrelated infrastructure skips accurately).
- [x] Inspect `git diff --check`, `git diff --stat` and status; no mutant remains.
- [x] Confirm `Python lint / type / test`, `OpenAPI lint`, `Webhook container smoke`.

**Dependencies:** Tasks 1-2, including fixes from the independent review loop.

**Files likely touched:** `docs/plans/209-plan.md`, `docs/plans/209-todo.md` (evidence only).

**Estimated scope:** Small (2 documentation files).

### Final verification record for publishing (2026-10-24)

| Check | Result |
| --- | --- |
| `uv run ruff check .` | PASS on final source/tests |
| `uv run ruff format --check .` | PASS, 534 files already formatted |
| `uv run mypy .` | PASS, 531 source files |
| Planned focused suite (cache + adapters + wiring) | PASS, 46 tests / 0 skips / 0.71s |
| Task 1 independent Standards / Spec review | PASS, 0 / 0 findings; four mutants independently reproduced |
| Task 2 independent Standards / Spec review | PASS, 0 / 0 findings; fallback revert independently reproduced; 25 cache/wiring tests passed |
| All five independent mutation contracts | PASS: e1/e2 cache-type RED; b3/b4 Redis-construction RED; fallback revert issuance-count RED; each followed clean GREEN |
| `git diff --check` and final scope inspection | PASS; one-line production cache fix, cache tests, new wiring tests and plan/evidence docs only |
| Full `uv run pytest` on the reviewed feature diff based on `22c621a` | PASS, exit 0; 5298 collected; 4337 passed, 961 skipped, 2 warnings in 384.85s (6:24) |
| Remote required CI at initial code head `c9919dd` | PASS: `Python lint / type / test`, `OpenAPI lint`, `Webhook container smoke`; [run 40084814523](https://github.com/larchanka-training/dmc-268-api-t6/actions/runs/40084814523) |

CI discovery verified in `.github/workflows/ci-cd.yml`: the mandatory Python job runs
unrestricted `uv run pytest` with coverage, and `pyproject.toml` sets
`testpaths = ["tests"]`. New tests have no integration/skip marks or service condition.
The actual clean-miss fallback remains in the source diff; startup modules and existing
`tests/test_worker.py` have empty diffs. No mutation source was written to this worktree.
Before publication, status contained only the two modified implementation/test files
and three untracked approved files (new wiring test and both planning documents); no
unrelated file appeared.

The coordinator's complete `uv run pytest` on the feature diff based on `22c621a`
finished with exit 0: **5298 collected,
4337 passed, 961 skipped, 2 warnings in 384.85s (6:24)**. The skips belong to unrelated
infrastructure tests without `TEST_DATABASE_URL`/broker configuration; all **19 cache
and 6 wiring tests executed without skips**. Existing FastAPI/Starlette httpx and anyio
BlockingPortal deprecation warnings require no change for this issue. No duplicate full
local suite was started; quick gates were rerun after the clean rebase, as recorded below.

All local Task 3 checks and initial required remote CI checks are complete. Draft
publication and its verified CI evidence are recorded below. The issue remains open for
the tech lead's acceptance check; no merge or review request was made.

## Publishing handoff (coordinator/publisher)

- [x] Refresh main, rebase before review, preserve the approved minimal file scope.
- [x] Publisher follows pull-request skill: `fix/209-token-cache-wiring`, Conventional
      Commit referencing #209, PR title <=72 characters, body What/Why/How to verify/Refs.
- [x] No closing keyword or Development-panel link; tech lead closes the issue after AC.
- [x] Push/open PR through authorized publisher, attach resulting PR to this chat, verify
      required CI, update board 12 as authorized; no merge without current-head approval
      and resolved review threads.

### Publication and CI evidence (2026-10-24)

- Refreshed `main` advanced to `25c83fe0d024270e9a744850baa9721cb0ede617` via the
  disjoint #168 review diff/prompt changes. Rebased cleanly with no conflict or change
  to the approved five-file scope. Initial published code commit:
  `c9919dd0319b062cad439d722e79dca57f5b0de3`.
- On that rebased checkout, `uv run ruff check .` passed; `uv run ruff format --check .`
  reported **535 files already formatted**; `uv run mypy .` reported **532 source
  files, no issues**. The focused cache/adapters/wiring suite passed **46 tests,
  0 skipped in 0.99s**. Commit hooks passed without bypass.
- Opened and attached [Draft PR #259](https://github.com/larchanka-training/dmc-268-api-t6/pull/259)
  from `fix/209-token-cache-wiring` to `main`, using the required PR sections/checklist.
  No reviewer was chosen or requested; the PR has no closing issue references, and
  issue #209 remains open. Course board #12 stays **In Progress**, consistent with Draft.
- [Initial code-head CI run 40084814523](https://github.com/larchanka-training/dmc-268-api-t6/actions/runs/40084814523)
  completed successfully. All three required jobs passed: **Python lint / type / test**,
  **OpenAPI lint**, **Webhook container smoke**. All other validation/security/build
  jobs passed; image publishing and deployment jobs were skipped for this PR event.
  The Python job completed at **10:47:18 UTC**: its full suite had **5314 passed,
  0 skipped, 2 warnings in 1247.55s**, with the required **100% coverage**; Gold corpus
  and recorded replay passed; the explicit integration suite had **961 passed,
  0 skipped, 4353 deselected, 2 warnings in 934.08s**.
- This documentation-only followup records completed work and pinned initial code-head
  evidence; source and tests are unchanged. Final publication is gated on the latest
  head's required checks, available in [PR #259 checks](https://github.com/larchanka-training/dmc-268-api-t6/pull/259/checks).
  No merge is authorized here; current-head approval and resolved review threads remain
  prerequisites for a future merge.


## Review round 1 tasks (authorized continuation)

Start: clean local `44b53ed610093e99dd2b6d608afcd5bce25f327b`, rebased onto
main `1ae74b1ae8fb48aed9dc4d9e913bf4c916d5a813`. Remote reviewed head: `b465b59`.
The earlier PASS/frozen/draft notes are historical; PR #259 is now Ready and requested
changes from verified reviewer `axyi` remain to be addressed.

### R1-A: Document the current fallback contract (P2, P3.2)

- [x] Update exactly the cache module/class docstrings and SD §8.3 token row for clean
      misses, eviction/rejected writes, valid local data and expiry minus safety skew.
- [x] Class docstring explains why Redis DEL cannot invalidate every process-local copy
      for future 401 handling; leave implementation and code-index source unchanged.
- [x] Verify the three descriptions agree; SD overlap comments were posted before edits
      on #283/#277/#254/#229/#227/#220. No new approval or comment is needed here.

Files: cache source, `docs/SYSTEM_DESIGN.md`. Dependency: none. Scope: Small.

R1-A evidence (2026-10-24, local base `44b53ed`): updated exactly the cache module
and class docstrings plus the Installation-token row in SD §8.3. All three now describe
Redis-hit precedence, valid local fallback on read errors or clean misses after eviction /
rejected writes, and a miss without valid local data at expiry minus the 60-second default
skew. The class additionally explains that future 401 invalidation must clear Redis and
every process-local token copy; Redis DEL alone is insufficient. No invalidation feature
or runtime behavior changed. AST comparison to HEAD, excluding only those two docstrings,
confirmed identical runtime code; the SD diff contains only its requested token row.

- `uv run pytest tests/test_redis_installation_token_cache.py
  tests/test_github_installation_adapters.py tests/test_worker_token_cache_wiring.py`:
  **46 passed / 0 skipped / 1.11s**.
- `uv run ruff check .`: **passed**; `uv run ruff format --check .`:
  **545 files already formatted**; `uv run mypy .`: **540 source files, no issues**;
  `git diff --check`: **passed**.
- R1-A independent review: **PASS, Standards 0 / Spec 0 findings**. Full pytest is
  deferred until the final review-round verification. No test/code-index/worker edit, commit,
  push or external reply in this slice.

### R1-B: Tighten regression tests and fixtures (P3.1, P3.3, P3.4)

- [x] Add provider eviction regression: first request successfully stores `token:17`, remove
      that fake Redis key, make two more requests; one issuance and boolean token reuse.
- [x] Remove both `len(redis.values) == 0` assertions in write-failing scenarios; keep
      meaningful issuance/expiry checks and existing sanitized warning-count checks.
- [x] Reuse an async-context-managed `_process(..., now=...)` for every cache provider
      test, including old two-provider scenarios, so all clients close deterministically.
- [x] Redis factory and fake httpx client tolerate keyword options; webhook composition
      uses explicitly named fake parameters instead of positional indexes.
- [x] Focused tests pass with zero skips; repeat e1/e2/b3/b4/fallback-revert RED and clean
      GREEN checks, including the new eviction case for fallback revert. Token values
      never appear in new assertion messages or mutation evidence.

Files: cache tests, wiring tests. Dependency: R1-A may run first; no source refactor.
Scope: Small. Verify: `uv run pytest tests/test_redis_installation_token_cache.py
 tests/test_github_installation_adapters.py tests/test_worker_token_cache_wiring.py`.

R1-B evidence (2026-10-24, local base `44b53ed`): added
`test_clean_redis_misses_after_eviction_reuse_local_token` before fixture cleanup. Its
successful first write is checked with precomputed keys and TTL `3540`; after deleting
`token:17`, two sequential real-provider reads must reuse the valid local credential.
The fallback-only runtime revert made `assert github.posts == [17]` fail with **[17, 17]**:
**1 failed / 0 skipped / 0.82s**. The clean node then passed: **1 passed / 0.71s**.
Only installation IDs and key/lifetime metadata appeared in failure evidence.

- All cache provider tests now use `_process(..., now=...)` as an async context manager
  inside their owning `asyncio.run` scenario. Its single MockTransport-backed client
  closes through `async with`, including both providers in sharing/isolation scenarios.
  Expiry tests keep the same mutable clock for provider/cache/token endpoint. Updated
  credential comparisons use precomputed booleans; no token-valued assertion diffs.
- Removed both tautological dictionary-length assertions in write-failing scenarios;
  retained issuance, reuse, expiry/precedence and sanitized warning-count/metadata checks.
- Wiring fakes accept `**options: object` for Redis/client construction; the client fake
  always supplies MockTransport itself and ignores any supplied transport. The webhook
  stop fake names `resources`, `client`, `tokens`, `config`, `run_publisher` and checks
  the named resources/provider/publisher bindings. All six startup cases and cleanup
  assertions remain; no cache factory or `github_adapters` is patched.
- After fixture changes, the focused command from this task passed **47 tests / 0 skips
  / 0.79s** (20 cache, 21 adapters, 6 wiring).
- Repeated all five substitutions with inline Python in a fresh process for each:
  import target module; read its `__file__`; require one exact old snippet; compile/exec
  only the substituted source in its namespace; run targeted `pytest.main` with
  `-q --tb=short`; require exit 1 and the specific contract assertion/count. No tracked
  production file was written. Complete reproducible instructions are in the R1-C
  evidence below; no permanent mutation helper was added.

| Mutant | Failed nodes | Contract assertion and result | Clean focused rerun |
| --- | --- | --- | --- |
| e1 | `test_review_worker_startup_selects_installation_token_cache[redis]` | Redis cache type; 1 failed / 5 deselected / 0.30s | 47 passed / 0 skipped / 0.92s |
| e2 | `test_webhook_worker_startup_selects_installation_token_cache[redis]` | Redis cache type; 1 failed / 5 deselected / 0.10s | 47 passed / 0 skipped / 0.74s |
| b3 | `test_webhook_worker_startup_selects_installation_token_cache[absent]`, `[empty]` | zero Redis constructions; actual `[None]`; 2 failed / 4 deselected / 0.11s | 47 passed / 0 skipped / 0.75s |
| b4 | `test_review_worker_startup_selects_installation_token_cache[absent]`, `[empty]` | zero Redis constructions; actual `[None]`; 2 failed / 4 deselected / 0.25s | 47 passed / 0 skipped / 0.77s |
| fallback | `test_clean_redis_misses_reuse_local_token_when_writes_fail` and `test_clean_redis_misses_after_eviction_reuse_local_token` | issuance IDs `[17, 17, 17]` and `[17, 17]` instead of `[17]`; 2 failed / 18 deselected / 0.61s | 47 passed / 0 skipped / 0.73s |

Startup nodes above are in `tests/test_worker_token_cache_wiring.py`; fallback nodes
are in `tests/test_redis_installation_token_cache.py`. A final clean focused rerun after
precomputing the eviction key-list assertion passed **47 tests / 0 skips / 0.77s**.

The e2/b3 inline drivers each emitted one known anyio assert-rewrite warning because
production module import precedes pytest initialization. No TypeError, import, fixture
or unrelated startup failure counted as RED. The cache runtime stays identical to HEAD;
only the already reviewed module/class docstrings differ.

- `uv run ruff check .`: **passed**; `uv run ruff format --check .`:
  **545 files already formatted**; `uv run mypy .`: **540 source files, no issues**;
  `git diff --check`: **passed**. Full pytest remains deferred to R1-C.
- R1-B independent review: **PASS, Standards 0 / Spec 0 findings**. Reviewer independently
  reverted the fallback for both regressions: expected 3/2 issuances observed, then
  **2 clean tests passed**. No worker/indexer runtime edit, dependency, commit, push
  or public reply. R1-C final verification remains in progress.

### R1-C: Reproducible evidence and publication (P3.5, P3.6)

- [x] Replace numeric source-line references in the plan with named symbols.
- [x] Replace disposable-driver commands with self-contained reproduction instructions or a concise inline
      recipe for all five exact source substitutions in memory in fresh processes,
      selects the intended test nodes and records assertion failures plus clean reruns.
      No permanent mutation script/framework or overlapping tracked entrypoint edits.
- [x] All gates: `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy .`,
      `uv run pytest`; record exact results/skips and `git diff --check`.
- [x] Independent Standards/Spec review passes; all local implementation/evidence tasks
      are complete before the single review-fix push.

Files: both plan/evidence files. Dependencies: R1-A/R1-B. Scope: Small.
No new plan approval required.

#### Self-contained mutation reproduction

Run this block from the repository root with the normal uv environment. Each mutant
runs in a new Python process; source overrides exist only in memory. The fallback
strings preserve indentation/newlines and target only the clean-miss branch. The hook
requires the intended call-phase assertion, so import/fixture/TypeError failures do not
count as RED. The clean focused command runs after each proved mutant.

```sh
set -e
for mutant in e1 e2 b3 b4 fallback; do
  uv run python - "$mutant" <<'PYTHON'
import importlib
from pathlib import Path
import sys
import pytest

wiring = 'tests/test_worker_token_cache_wiring.py'
cache = 'tests/test_redis_installation_token_cache.py'
specs = {
    'e1': ('app.worker',
           'cache=installation_token_cache(redis),',
           'cache=installation_token_cache(None),',
           [wiring, '-k', 'review and redis'], 1),
    'e2': ('app.webhook_worker',
           'cache=installation_token_cache(cast(RedisLike | None, redis)),',
           'cache=installation_token_cache(None),',
           [wiring, '-k', 'webhook and redis'], 1),
    'b3': ('app.webhook_worker',
           'redis = Redis.from_url(config.redis_url) if config.redis_url is not None else None',
           'redis = Redis.from_url(config.redis_url)',
           [wiring, '-k', 'webhook and (absent or empty)'], 2),
    'b4': ('app.worker',
           'if settings.redis_url is not None:', 'if True:',
           [wiring, '-k', 'review and (absent or empty)'], 2),
    'fallback': (
        'app.modules.integrations.webhooks.infrastructure.redis_installation_token_cache',
        '        if value is None:\n            return await self._local.get(installation_external_id)\n',
        '        if value is None:\n            return None\n',
        [cache, '-k', 'clean_redis_misses'], 2),
}
name = sys.argv[1]
module_name, old, new, arguments, count = specs[name]
module = importlib.import_module(module_name)
source = Path(module.__file__).read_text()
assert source.count(old) == 1
exec(compile(source.replace(old, new, 1), module.__file__, 'exec'), module.__dict__)

class Proof:
    def __init__(self):
        self.failures = []

    def pytest_runtest_logreport(self, report):
        if report.failed:
            assert report.when == 'call'
            text = report.longreprtext
            if name.startswith('e'):
                assert 'assert isinstance(capture.caches[0], RedisInstallationAccessTokenCache)' in text
            elif name.startswith('b'):
                assert 'assert redis.urls == []' in text
            else:
                assert 'assert github.posts == [17]' in text
                expected = '[17, 17] == [17]' if 'after_eviction' in report.nodeid else '[17, 17, 17] == [17]'
                assert expected in text
            self.failures.append(report.nodeid)

proof = Proof()
assert pytest.main(['-q', '--tb=short', *arguments], plugins=[proof]) == 1
assert len(proof.failures) == count
print(f'CONTRACT MUTANT {name}: {count} expected assertion failures')
for node in proof.failures:
    print(node)
PYTHON
  uv run pytest tests/test_redis_installation_token_cache.py tests/test_github_installation_adapters.py tests/test_worker_token_cache_wiring.py -q
done
```

The exact recipe above is verified as part of R1-C; original result counts above remain
historical evidence for their original heads. This is a documentation recipe rather
than a permanent mutation script, framework or dependency.

R1-C local evidence (2026-10-24, local base `44b53ed`): removed every stale numeric
source reference from the plan in favor of module/symbol names. Historical disposable
commands above now point to the complete inline recipe, without changing their original
counts/head context. No temporary driver or transient brief is needed to reproduce it.

Executed the **exact shell block extracted from this Markdown**, ending with exit 0.
Mutants e1/e2/b3/b4/fallback produced **1/1/2/2/2 expected assertion failures** in
**0.29/0.10/0.11/0.25/0.61s**, respectively. Their intended nodes and assertion reasons
match the R1-B table; fallback again produced 3 issuances on rejected writes and 2 after
successful-write eviction. Each was followed by **47 passed / 0 skipped**, in
**0.73/0.79/0.78/0.76/0.77s**. The e2/b3 processes each emitted the known anyio
assert-rewrite warning; no unrelated failures counted as RED. All source overrides were
in memory; no mutant or permanent script entered the diff.

| Final review-round check | Result |
| --- | --- |
| R1-A independent Standards / Spec review | PASS, 0 / 0 findings |
| R1-B independent Standards / Spec review | PASS, 0 / 0 findings; both fallback regressions independently reproduced |
| Focused suite after final test changes | PASS, 47 tests / 0 skips / 0.77s |
| Exact durable inline recipe | PASS, five proved mutants and five clean 47-test reruns |
| `uv run ruff check .` | PASS on frozen final source/tests |
| `uv run ruff format --check .` | PASS, 545 files already formatted |
| `uv run mypy .` | PASS, 540 source files |
| `git diff --check` / final scope | PASS, six authorized cache/doc/test/plan files only; no worker/indexer runtime change |
| Full `uv run pytest` | PASS, exit 0; 5678 collected; 4693 passed, 985 skipped, 2 warnings in 314.47s (5:14) |
| R1-C final independent review | PASS, Standards 0 / Spec 0 findings |

Full-suite provenance: the coordinator ran `uv run pytest` once on the final frozen
review-round source/tests based on local `44b53ed610093e99dd2b6d608afcd5bce25f327b`,
rebased onto main `1ae74b1ae8fb48aed9dc4d9e913bf4c916d5a813`. Result: **exit 0;
5678 collected, 4693 passed, 985 skipped, 2 warnings in 314.47s (5:14)**. Skips are
unrelated infrastructure cases without disposable database/broker configuration, including
cases added on current main. All **20 cache + 21 adapter + 6 wiring tests executed with
zero skips**. The warnings are existing FastAPI/Starlette httpx and anyio BlockingPortal
deprecations; no new warning was introduced by this review round.

Source and test code are frozen after R1-B. Only documentation/evidence changed in R1-C;
previously passed quick gates are reused, and no duplicate full suite was started. All
local gates pass. Final R1-C independent review: **PASS, Standards 0 / Spec 0 findings**,
including direct cross-check against the original review and the exact full-suite counts
on local base `44b53ed610093e99dd2b6d608afcd5bce25f327b` / main `1ae74b1`. New-head remote
CI and external dispositions are publisher work below; no new-head success is claimed
here and no post-push repository checkbox requires a second evidence-only push.

Publisher handoff (external live state; record results in PR metadata, not a second docs push):

- Commit with Refs #209 and push the rebased branch once after local verification/review.
- Title: `fix(webhooks): test token cache wiring, reuse local token on miss`.
  Keep Ready; do not split/rewrite prior commits solely for P3.5.
- Refresh PR body and verify required checks on the actual pushed head; link live
  [PR #259 checks](https://github.com/larchanka-training/dmc-268-api-t6/pull/259/checks).
  Do not claim new-head CI before it completes.
- Reply with fix SHA in P2 thread `discussion_r4367504971`, summarize all six P3 groups,
  and re-request `axyi`. Author never resolves `PRRT_kwDOUOEIOM6v5QKd`.
- Record final CI/reply/request outcomes in PR body/replies metadata only; historical
  initial-round evidence above stays pinned to its original head.
