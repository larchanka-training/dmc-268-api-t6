---
name: e2e-test
description: This skill should be used when verifying backend behavior against a real running stack, reproducing an API bug, or end-to-end testing the healthcheck and webhook entrypoints of this FastAPI service.
metadata:
  version: 1.0.0
  source: instructor-pack
  adapted-for: backend
---

# E2E Testing (FastAPI backend, API variant)

Verify real backend behavior against the containerized stack — no browser
involved. Prefer this over unit-test reasoning when a bug only reproduces
against a running Postgres and app process together (migrations, wiring,
container networking).

## 1. Bring the stack up

```bash
docker compose up -d --build
```

This starts `backend` (port `${BACKEND_PORT:-8000}`, from `docker-compose.yml`
on `main`), `worker`, `postgres`, `rabbitmq` and `redis`; every service but
`backend` has a healthcheck. `webhook-worker` starts only with
`--profile webhooks`. Do not run the app locally with `uv run uvicorn` for this
workflow — the point is to exercise the same container image and network path
CI/prod use.

`up` does not run migrations; apply them before any check that touches the
database (an unmigrated database answers the webhook with 500):

```bash
docker compose run --rm backend alembic upgrade head
```

## 2. Readiness loop

Poll the healthcheck endpoint instead of a fixed sleep (`app/main.py` on
`main`: `GET /healthcheck` → `{"status": "ok"}`):

```bash
for i in $(seq 1 30); do
  code=$(curl -s -o /dev/null -w "%{http_code}" http://localhost:8000/healthcheck)
  [ "$code" = "200" ] && break
  sleep 1
done
[ "$code" = "200" ] || { echo "backend not ready after 30s"; docker compose logs backend; exit 1; }
```

## 3. Smoke checks via `httpx`

`httpx` is a runtime dependency on `main` (`pyproject.toml`,
`[project].dependencies`). Drive it with
`uv run python -c` for quick, disposable smoke scripts — no new test file
needed for a manual smoke pass:

```bash
uv run python -c "
import httpx

r = httpx.get('http://localhost:8000/healthcheck')
assert r.status_code == 200, r.status_code
assert r.json() == {'status': 'ok'}, r.json()
print('healthcheck: ok')
"
```

**Webhook smoke** — `scripts/webhook_smoke.py` (stdlib only) signs deliveries
with `GITHUB_WEBHOOK_SECRET` (HMAC-SHA256 over the raw body,
`docs/SYSTEM_DESIGN.md` §8.3) and posts them to `POST /webhooks/github`. Set a
non-empty `GITHUB_WEBHOOK_SECRET` in `.env` before step 1: compose hands the
same value to `backend`, and the script reads it from the environment only
(empty: the route answers 503, the script exits 2 before sending).

Step 1 does not start webhook-worker (compose profile `webhooks`). If it runs,
stop it for the smoke (`docker compose stop webhook-worker`) or expect failed
projections: the deliveries name installation 17 and repository 101, so the
worker's GitHub calls fail and it marks each delivery failed after three tries.

```bash
uv run --env-file .env python scripts/webhook_smoke.py --count 100
```

One line per check: a fresh delivery → 202 `{"status": "pending"}`; the same
delivery again → 202 `{"status": "duplicate"}`; a corrupted signature → 401
`{"detail": "invalid GitHub webhook signature"}`; with `--count N`, N fresh
deliveries → 202 pending each, then p50/p95/max latency. Exit 0 — all passed;
1 — a response did not match (expected vs got printed); 2 — invalid arguments
(e.g. a `--url` without `http://`), no secret, or no valid HTTP answer. A 500
usually means the migrations were not applied. `--url` overrides
`http://localhost:8000/webhooks/github` (non-default `BACKEND_PORT`). CI runs
the same script against the built image in the `Webhook container smoke` job
(`.github/workflows/ci-cd.yml`).

## 4. Assertions

Assert on status codes and literal JSON fields (`r.json() == {...}` with a
known-good literal), never on prose or partial matches — a smoke check that
"looks reasonable" hides regressions.

## 5. Cleanup

Always tear the stack down after the run, including the volume so the next
run starts from a clean database:

```bash
docker compose down -v
```

Do not leave the stack running between smoke passes — a stale container from
a previous run masks whether the current build actually works.

## 6. What to Report

For each check: pass/fail, the exact `curl`/`httpx` command run, the actual
vs. expected status code and JSON, and the last 20 lines of
`docker compose logs backend` on any failure.

## Known Pitfalls

- Omitting `--build` silently reuses a stale image after a code change.
- The Postgres healthcheck can pass before the app finishes its own startup
  — always poll `/healthcheck` too, never assume readiness from Postgres.
- Port `8000` may already be bound by a local `uv run uvicorn` process —
  stop it first, or the container's port mapping fails silently.
