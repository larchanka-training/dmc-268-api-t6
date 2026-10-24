# Task list: #209 token-cache wiring and local fallback

Base: `22c621abc80abdd1b3daaa95bde73b6aaf81c32e`.
Plan: [209-plan.md](209-plan.md). Tasks 1-2 reviewed; local verification complete; remote CI/publication pending.

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
- Independent fresh-process mutation command:
  `uv run python /private/tmp/209-wiring-mutant.py <id>`.
  The disposable driver reads the original module, replaces exactly one occurrence,
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
- **Fallback-revert mutant:** `uv run python /private/tmp/209-fallback-mutant.py`.
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
- [ ] Required remote CI checks are green before publication is reported complete.

**Verification:**

- [x] `uv run ruff check .`
- [x] `uv run ruff format --check .`
- [x] `uv run mypy .`
- [x] `uv run pytest` (report unrelated infrastructure skips accurately).
- [x] Inspect `git diff --check`, `git diff --stat` and status; no mutant remains.
- [ ] Confirm `Python lint / type / test`, `OpenAPI lint`, `Webhook container smoke`.

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
| Full `uv run pytest` | PASS, exit 0; 5298 collected; 4337 passed, 961 skipped, 2 warnings in 384.85s (6:24) |
| Remote required CI | PENDING publication: `Python lint / type / test`, `OpenAPI lint`, `Webhook container smoke` |

CI discovery verified in `.github/workflows/ci-cd.yml`: the mandatory Python job runs
unrestricted `uv run pytest` with coverage, and `pyproject.toml` sets
`testpaths = ["tests"]`. New tests have no integration/skip marks or service condition.
The actual clean-miss fallback remains in the source diff; startup modules and existing
`tests/test_worker.py` have empty diffs. No mutation source was written to this worktree.
Status contains only the two modified implementation/test files and three untracked
approved files (new wiring test and both planning documents); no unrelated file appeared.

The coordinator's complete `uv run pytest` finished with exit 0: **5298 collected,
4337 passed, 961 skipped, 2 warnings in 384.85s (6:24)**. The skips belong to unrelated
infrastructure tests without `TEST_DATABASE_URL`/broker configuration; all **19 cache
and 6 wiring tests executed without skips**. Existing FastAPI/Starlette httpx and anyio
BlockingPortal deprecation warnings require no change for this issue. No duplicate full
suite or code-gate run was started after the verified source/tests were frozen.

All local Task 3 checks are complete. Remote required CI and publication remain pending;
publishing has not occurred and the issue remains open for the tech lead's acceptance
check. The publisher will append PR/CI evidence after publication.

## Publishing handoff (coordinator/publisher)

- [ ] Refresh main, rebase before review, preserve the approved minimal file scope.
- [ ] Publisher follows pull-request skill: `fix/209-token-cache-wiring`, Conventional
      Commit referencing #209, PR title <=72 characters, body What/Why/How to verify/Refs.
- [ ] No closing keyword or Development-panel link; tech lead closes the issue after AC.
- [ ] Push/open PR through authorized publisher, attach resulting PR to this chat, verify
      required CI, update board 12 as authorized; no merge without current-head approval
      and resolved review threads.
