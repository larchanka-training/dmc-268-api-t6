# Issue #110: isolated rollback acceptance evidence

The retained six-case run on 2026-10-12 used Docker 24.0.2 on arm64 and official Compose
**5.6.0**. The [sanitized machine report](110-verification.json) records script SHA-256 hashes,
actual revisions, service identities/health, and persistent file hashes/modes. Match the script
hashes to the intended checkout when evaluating this proof. It contains no credentials, raw
command output, personal paths, host checkout SHA, or process/gate counts.

Prepared image A was built from the repository Dockerfile and lockfile at application base
`92250a1fb3b65f88a2ea111d33613f9a4af1d1d2` and reused. This proves the updated host scripts
against that real historical application image; it does not prove a current application build.

- A: `sha256:81c69606c3cda7f35c20bb0de5f8dc97b20a24a134259491e6a05f4777bfc937`.
- B: `sha256:f7efa4394d55018896c7420a4439a9c6f33864603eafdfd13d2b0d732171c65d`.
- Actual A head M: `20261007_0028`.
- Synthetic child N: `20261010_0110`.
- Unique project: `rollback110-7ea752a5fd0849e2`; the offline network uses the `-offline` suffix.

M is derived from A's single validated migration head. B's fixture is rendered with M as
`down_revision`, a collision-safe N and matching filename; B's actual graph verifies the child
edge. Production migrations are untouched. The fixture source is readable by A's non-root
runtime despite the verifier's private umask.

The unchanged staging base uses pinned PostgreSQL 17.11, RabbitMQ 4.3.6 and Redis 8.10.2.
The verifier owns fresh volumes and an internal network, publishes no host ports, generates
synthetic credentials and a valid RSA PEM, and sends no GitHub/LLM requests. Normal A deploy
reached M and normal B deploy reached N with the actual bootstrap and a nonsecret fixture
sentinel. API literal HTTP 200 `{"status":"ok"}` and both worker heartbeats passed.

| Real-script case | Exit | Result |
| --- | --- | --- |
| compatible-manual | 0 | Healthy A; DB retains M; previous becomes B. |
| compatible-auto | 0 | Healthy A; DB retains M; previous remains A. |
| incompatible-manual | 1 | Controlled refusal; all rollback-entry invariants unchanged. |
| incompatible-auto | 1 | Controlled refusal; all rollback-entry invariants unchanged. |
| unreachable-manual | 1 | Controlled unreachable-DB refusal in 9.89s; offline files and primary B unchanged. |
| unreachable-auto | 1 | Controlled unreachable-DB refusal in 9.4s; offline files and primary B unchanged. |

Compatible B-at-M is a controlled setup: replace only application services using `up --no-deps`
and skip B bootstrap solely to retain M. Accepted rollback runs real `rollback.sh`, stdin checker,
Compose recreation, health wait and stdout-only environment diagnostics. Refused cases preserve
all application/store/bootstrap containers, database revision/sentinel and persistent file/mode
from rollback entry, with no completion or diagnostic output. Diagnostic key lists match the
expected role/image keys. All command output passed scans for synthetic secrets, complete PEMs
and every PEM line. An empty separate offline network tests unreachable DB without stopping the
primary project's PostgreSQL. Ephemeral probes were absent before cleanup; all owned resources,
B tag and successful private temporary root were removed. Prepared A and pinned store images remain.

Reproduce after building A from the intended checkout and resolving its ID:

```bash
docker build -t dmc268-rollback110-prepared:a .
rollback_a_id="$(docker image inspect --format '{{.Id}}' dmc268-rollback110-prepared:a)"
uv run python scripts/verify_rollback.py \
  --image-a dmc268-rollback110-prepared:a --expect-image-a "$rollback_a_id" \
  --report "$(mktemp -d)/report.json"
```

The report path must be new. CLI resource checks refuse preexisting project networks, volumes
or containers before mutation. The local-ID seam is **verifier-only after the pull boundary**:
a scoped wrapper substitutes pulls only for inspected A/B local IDs, adds `up --pull never`
only to fixture/forward commands without a pull flag, and preserves production rollback's
`up --pull missing`. Production rollback binds app/bootstrap to already-inspected immutable IDs
and permits absent pinned storage images to be pulled. Stores were already local in this run;
it does not simulate downloading them from an empty cache. Registry delivery, mutable-tag
resolution and matching repository digests have behavioral regression coverage, not live proof.
A plugin inside the user's selected Docker config is exposed by symlink in private configs only
when required; no credential config is copied. System-installed plugins need no exposure.

Current/previous metadata is recorded immediately after healthy rollback `up --wait`, before
fallible environment diagnostics. Diagnostic failure keeps the job failed and blocks promotion,
while preserving the actual release identity. This failure is covered behaviorally, including
valid stdout with secret-bearing stderr, nonzero exec exit and extra stdout lines; it is not one
of the six live cases above. Recover using the independent [SECRETS §5 exec recipe](../SECRETS.md#5-логи-ci)
after verifying actual images/health and fixing exec; never rerun rollback to retry diagnostics.
Guarantees begin at entry into `rollback.sh`; prior forward deploy or manual edge
upload/provision/proxy changes are outside that boundary. Graph admission is necessary, and
does not prove application/schema/data/secret compatibility or permit automatic downgrade.

Historical proof is labelled separately in the JSON: the earlier 2026-10-12 Compose 5.6.0 run
retains its actual images, revisions and script hashes, and the 2026-10-10 Compose 2.19.1 summary
points to evidence preserved in Git history. Those runs do not verify the latest stdout-only
exec correction. Old gate counts and host checkout references are omitted because they do not
identify this runtime proof.
