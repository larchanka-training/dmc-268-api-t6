# Issue #110: isolated rollback acceptance evidence

Latest proof rerun on 2026-10-10 after binding rollback admission/up/state to immutable image
identity, using Docker 24 on arm64 and the installed Compose 2.19.1 plugin. The B fixture
includes the required future-annotations import.
The redacted machine report is [110-verification.json](110-verification.json); it retains
all service IDs/images/status/health and host file hashes/modes before and after each case.
No environment values, credentials, raw logs or PEM lines are included.

Prepared image A was built from the actual repository Dockerfile and uv.lock at base
`92250a1fb3b65f88a2ea111d33613f9a4af1d1d2`; deployment-script changes do not change that image.

- A: `sha256:81c69606c3cda7f35c20bb0de5f8dc97b20a24a134259491e6a05f4777bfc937`.
- B: `sha256:45bdccc6965cf85027e2b7a7be7ca8c5b07386736e7975554574c6af30ab1057`.
- A's actual image Alembic head M: `20261007_0028`.
- B's test-only child head N: `20261010_0110`, creating `rollback110_fixture`.
- Unique project: `rollback110-26092e3b17624e53`; offline network has suffix `-offline`.

The verifier copies the production staging base unchanged, including its pinned PostgreSQL
17.11, RabbitMQ 4.3.6 and Redis 8.10.2 images. Its local override publishes no host ports and
makes the default network internal. Synthetic App credentials, a generated RSA PEM and long
secret canaries exercise real worker startup and name-only diagnostics. `LLM_MODEL` is absent.
No messages, installations, review requests, webhook deliveries or provider calls are created.

| Real-script case | Exit | Observed result |
| --- | --- | --- |
| Normal A deploy | 0 | Actual bootstrap reached M; API literal HTTP 200 `{"status":"ok"}` and both worker heartbeats healthy. |
| Compatible manual B→A at M | 0 | A healthy, DB remains M, synthetic N table absent; previous becomes B. |
| Compatible auto B→A at M | 0 | A healthy, DB remains M; previous remains A. |
| Normal B deploy | 0 | Actual bootstrap reached N; nonsecret sentinel inserted in the synthetic table. |
| Incompatible manual B→A at N | 1 | Controlled unknown-revision refusal; all files/modes, application/store/bootstrap IDs/images/health, N and sentinel unchanged. |
| Incompatible auto B→A at N | 1 | Same refusal and unchanged invariants. |
| Unreachable DB, manual | 1 | Controlled graph/database inspection refusal in 9.45 seconds; offline files and healthy primary B unchanged. |
| Unreachable DB, auto | 1 | Same refusal in 9.12 seconds; offline files and primary B unchanged. |

The compatible fixture deliberately replaces only application services with B using
`up --no-deps`, retaining M and skipping B bootstrap solely for that controlled setup. Normal
B deployment subsequently runs its bootstrap and reaches N. Both accepted rollback cases run
the real host `rollback.sh`, checker stdin probe, Compose recreation and diagnostics. Runtime
key lists match exact expected image/role names; all captured command output passes checks for
each long synthetic secret, complete PEM and every PEM line. Refusals have no success or key
diagnostics. The offline fixture has its own empty internal network; primary PostgreSQL is
never stopped to simulate failure.

Reproduce after building A from the intended Dockerfile/lockfile inputs and resolving its ID:

```bash
uv run python scripts/verify_rollback.py \
  --image-a dmc268-rollback110-prepared:a \
  --expect-image-a sha256:81c69606c3cda7f35c20bb0de5f8dc97b20a24a134259491e6a05f4777bfc937 \
  --report /private/tmp/rollback110-new-report.json
```

Choose a new report path; existing files are refused. The CLI generates a unique project and
refuses existing network/volume/container resources before mutations. Cleanup removed only
that invocation's projects, volumes, offline network, B tag and successful private temp root.
Ephemeral probes were absent before cleanup. Prepared A, pinned store images and the separate
pytest database/broker services are preserved.

Local artifact/runtime substitutions: the scoped Docker wrapper treats pulls of only the
verified A/B immutable IDs as successful after real image inspection, and adds `--pull never`
to actual Compose `up` only when the command does not already supply it. Registry pulling is therefore outside this proof. All probes, migrations,
service operations, health checks and SQL queries run against real Docker. The same installed
Compose plugin is exposed by a symlink inside private `DOCKER_CONFIG` directories, because
this machine installs it only under the user's original config. No original credential config
is copied, and the verifier uses its own private config for rollback registry cleanup.

First attempt `rollback110-706f25e9315342a2` failed during normal A deployment because a private
registry config hid that local Compose plugin (`unknown shorthand flag: 'p' in -p`). It ran no
acceptance cases; its owned Docker resources and B tag were cleaned. The fixture plugin exposure
resolved this without a production-script change or relaxed probe validation. A separate RED
regression showed timeout/secret-output cleanup failures could interrupt later removals; cleanup
now attempts every owned resource and retains a controlled failed outcome. Safety tests cover
isolation, invariant corruption, unexpected refusal/success output, exact key evidence, real
wrapper forwarding/plugin exposure and best-effort cleanup. Required gate results are recorded
in [110-todo.md](110-todo.md).

The latest host rollback resolves a strict local ID after pull, runs both the checker and all
application/bootstrap up roles with that ID, and carries `--pull never` itself. This real proof
uses local immutable IDs and confirms actual runtime image identity and canonical local state.
Registry-tag movement and matching-repository digest resolution are modeled separately in
behavioral tests, including extracted promotion pulling/tagging the immutable digest. No private
registry canonicalization is claimed by this live report. Prepared A was reused; B was derived
from the unchanged corrected fixture bytes, with its actual resulting ID recorded above.
