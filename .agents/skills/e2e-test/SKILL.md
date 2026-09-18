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
on `main`) and `postgres` with a healthcheck. Do not run the app locally with
`uv run uvicorn` for this workflow — the point is to exercise the same
container image and network path CI/prod use.

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

`httpx` is a dev dependency on `main` (`pyproject.toml`). Drive it with
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

**Webhook smoke** — once the webhook entrypoint exists (pending api #4/#6):
POST a sample GitHub payload with an `X-Hub-Signature-256` header (HMAC over
the raw body, per the stack rules file in `.agents/rules/`) and assert the
response status and the literal JSON fields the endpoint contracts to
return. Do not invent the path or payload shape before the endpoint lands —
cite `docs/SYSTEM_DESIGN.md` (ui repo) §"GitHub: events and permissions" for
the header and signing scheme, and treat the route itself as not yet built:

```bash
uv run python -c "
import hashlib
import hmac

import httpx

secret = b'test-secret'
body = b'{\"action\": \"opened\"}'
sig = 'sha256=' + hmac.new(secret, body, hashlib.sha256).hexdigest()

r = httpx.post(
    'http://localhost:8000/webhooks/github',  # pending api #4/#6
    content=body,
    headers={'X-Hub-Signature-256': sig, 'Content-Type': 'application/json'},
)
assert r.status_code == 202, r.status_code
"
```

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
