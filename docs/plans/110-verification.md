# Issue #110: isolated rollback acceptance evidence

The latest run on 2026-10-12 used Docker 24.0.2 on arm64 and official Compose **5.6.0**.
The host scripts include review corrections applied atop rebased head
`d5e5ec5bdb47f763958fa38f8df0762f80f98c2b`, base
`46a90772f65c04aa3cc4db11b198e6d052bf1207`. Script hashes are recorded in the
[redacted machine report](110-verification.json), along with actual revisions, service
identities/health and host file hashes/modes. It contains no credentials, raw command output
or personal filesystem paths.

Prepared image A was reused from the repository Dockerfile and lockfile at application base
`92250a1fb3b65f88a2ea111d33613f9a4af1d1d2`. This is evidence for the corrected host scripts
against that real historical application image; it is not a freshly built current application.

- A: `sha256:81c69606c3cda7f35c20bb0de5f8dc97b20a24a134259491e6a05f4777bfc937`.
- B: `sha256:dba1432cea684508b5af6fc145660c882c10a823a78e4b96547f93a4ccda7253`.
- Actual A head M: `20261007_0028`.
- Synthetic child N: `20261010_0110`.
- Unique project: `rollback110-4e855f368aff46dd`; its offline network uses the `-offline` suffix.

M is derived from A's single validated migration head. B's fixture is rendered with M as
`down_revision` and a collision-safe N; B's actual graph verifies the child edge. Production
migrations are untouched. The fixture source is readable by A's non-root runtime despite the
verifier's private umask.

The unchanged staging base uses pinned PostgreSQL 17.11, RabbitMQ 4.3.6 and Redis 8.10.2.
The verifier owns fresh volumes and an internal network, publishes no host ports, generates
synthetic credentials and a valid RSA PEM, and sends no GitHub/LLM requests. Normal A deploy
reached M and normal B deploy reached N with the actual bootstrap and a nonsecret fixture
sentinel. API literal HTTP 200 `{"status":"ok"}` and both worker heartbeats passed.

| Real-script case | Exit | Result |
| --- | --- | --- |
| compatible-manual | 0 | Healthy A; DB retains M; previous becomes B. |
| compatible-auto | 0 | Healthy A; DB retains M; previous remains A. |
| incompatible-manual | 1 | Controlled refusal; all entry invariants unchanged. |
| incompatible-auto | 1 | Controlled refusal; all entry invariants unchanged. |
| unreachable-manual | 1 | Controlled unreachable-DB refusal in 10.93s; offline files and primary B unchanged. |
| unreachable-auto | 1 | Controlled unreachable-DB refusal in 9.84s; offline files and primary B unchanged. |

Compatible B-at-M is a controlled setup: replace only application services using `up --no-deps`
and skip B bootstrap solely to retain M. The accepted rollback itself runs real `rollback.sh`,
stdin checker, Compose recreation, health wait and diagnostics. Refused cases retain every
application/store/bootstrap container, database revision/sentinel and persistent file/mode from
rollback entry, with no completion or diagnostic output. Each diagnostic key list matches the
expected literal role/image keys. All command output passed scans for synthetic secrets,
complete PEMs and every PEM line. An empty separate offline network tests unreachable DB
without stopping the primary project's PostgreSQL.

Reproduce after building A from the intended checkout and resolving its ID:

```bash
docker build -t dmc268-rollback110-prepared:a .
rollback_a_id="$(docker image inspect --format '{{.Id}}' dmc268-rollback110-prepared:a)"
uv run python scripts/verify_rollback.py \
  --image-a dmc268-rollback110-prepared:a --expect-image-a "$rollback_a_id" \
  --report "$(mktemp -d)/report.json"
```

The report path must be new. CLI resource checks refuse preexisting project networks, volumes
or containers before mutation. All owned resources, B tag and successful private temporary root
were removed; ephemeral probes were absent before cleanup. Prepared A and pinned store images
remain. Separate pytest integration services belong to this review's coordinator and are
cleaned after all gates/reviews finish.

The local-ID seam is **verifier-only after the pull boundary**: a scoped wrapper substitutes
pulls only for inspected A/B local IDs, adds `up --pull never` only to fixture/forward commands
without a pull flag, and preserves production rollback's `up --pull missing`. Production
rollback binds app/bootstrap to already-inspected immutable IDs and permits absent pinned
storage images to be pulled. The pinned stores were already local in this live run; it does
not simulate their download from an empty cache. Registry delivery, mutable-tag resolution and matching repository
digests remain behavioral regression coverage, not claims made by this live proof. A plugin
installed inside the user's selected Docker config is exposed by symlink in private configs
only when required; no credential config is copied. System-installed plugins need no exposure.

After this live run, one additional test-first verifier correction makes a collision-suffixed N
also select a collision-suffixed migration filename, preserving an existing fixture in A.
The actual run used unsuffixed N, so its generated Dockerfile/migration bytes remain identical;
host rollback/checker scripts are unchanged. Final gates are rerun after this correction.

The historical 2026-10-10 Compose 2.19.1 evidence is preserved in Git history and summarized in
JSON metadata; it does not verify the modern Compose output correction. The first modern attempt
ran no acceptance cases because generated source inherited mode 0600 and A's non-root user
could not read it. Its controlled failure and cleanup passed. A new failing umask regression
preceded the fixture permission correction and this successful repeat run.

Current/previous metadata is recorded immediately after healthy rollback `up --wait`, before
fallible environment diagnostics. Diagnostic failure leaves the job failed and prevents
promotion while preserving the truthful release for the next forward deploy. Behavioral tests
cover this in both modes and preserve state after failed up/admission. Guarantees begin at
entry into `rollback.sh`; prior forward deploy or manual edge upload/provision/proxy changes
are outside that boundary. The ancestor check is retained as defense for malformed or alternate
graph adapters even though ordinary single-head Alembic reachability makes it redundant.

Both formerly surviving configuration mutations were rejected in a separate temporary source
copy: `external: false` produced **4 failures** and `@db:5432` produced **4 failures**. The
literal YAML assertion verifies the existing external network and `@postgres:5432` DSN.
Focused deployment/checker/verifier coverage totals **206 passing tests**. All four final
exact gates passed: Ruff lint, **358** files formatted, mypy **355** source files, and pytest
**2608 passed, 0 skipped, 2 warnings** in **336.75 seconds** with separate owned PostgreSQL
and RabbitMQ test infrastructure. Full gate records are in [110-todo.md](110-todo.md).
