# Rollback verifier follow-up evidence (#110, #106)

Verified on 2026-10-16 in a separate worktree based on main
`7546e539e1c1f565f65b9a5ca4cdc5dd1fbfb3fa`. The only production change is adding
`REDIS_URL` to the verifier's independent worker key expectation. Production
Compose, rollback admission, diagnostic strictness and secret scanning remain
unchanged. Historical evidence in `110-verification.*` is preserved.

The [stock sanitized report](110-verifier-live.json) is the verifier's output,
retained byte-for-byte. The separate [verification metadata](110-verifier-verification.json)
records provenance, gates and final resource checks; its fields are not claimed
to be stock-verifier output.

## Regression and gates

Before changing the verifier, the new real-Compose contract regression produced
**1 failed, 2 passed**: only worker lacked `REDIS_URL`. After the minimal fix,
all three role cases passed. The focused verifier/revision/deployment suite
passed **232 tests, zero skips**. Unit-test Docker fakes are limited to those
regressions; the live cases below execute the checked-in CLI unchanged.

The four required gates use a separate disposable PostgreSQL 17 / RabbitMQ 4
Compose project `rollback110-gates-ec19d9950bfd4204`, with fresh storage and
dynamic loopback-only host ports. `jq` is available. No shared database or broker
is used.

| Command | Result |
| --- | --- |
| `uv run ruff check .` | PASS |
| `uv run ruff format --check .` | PASS, 457 files already formatted |
| `uv run mypy .` | PASS, 454 source files |
| `uv run pytest --junitxml=<private-output>/pytest.xml` | PASS, 3790 passed; zero failures/errors/skips; 2 dependency deprecation warnings; 840.40s |

## Fresh images and real rollback

A was built successfully from this checkout's Dockerfile and locked dependencies;
the application source is main `7546e539e1c1f565f65b9a5ca4cdc5dd1fbfb3fa`.
The verifier/tests/docs follow-up does not change files copied into A. No
historical application image was reused. B is the stock verifier's synthetic
child of A with a fixture migration whose actual `down_revision` is M.

- A: `sha256:f6d47fe1bf650893bf66bc81b923812b110a987629c4f0a204c1d9a914ca3fe0`.
- B: `sha256:50ed9c67e24a3aed60e5ab94c9a07ca01a00d3cf9ef8bc673eede8ae63a19f19`.
- Actual A head M: `20261015_0032`; synthetic child N: `20261010_0110`.
- Real project: `rollback110-bd2e6fa84ce149d0`; offline network/project suffix: `-offline`.
- Runtime: Docker 24.0.2, Linux arm64; official Compose 5.6.0 in task-private config.

The executed command was:

```bash
DOCKER_CONFIG=<task-private-config> UV_CACHE_DIR=<task-private-cache> \
  uv run python scripts/verify_rollback.py \
  --project rollback110-bd2e6fa84ce149d0 \
  --image-a dmc268-rollback110-20261016:a \
  --expect-image-a sha256:f6d47fe1bf650893bf66bc81b923812b110a987629c4f0a204c1d9a914ca3fe0 \
  --report <new-private-output>/live-verifier.json
```

| Real stock-verifier case | Exit | Result |
| --- | --- | --- |
| compatible-manual | 0 | Healthy A; DB retains M; previous becomes B |
| compatible-auto | 0 | Healthy A; DB retains M; previous remains A |
| incompatible-manual | 1 | Controlled refusal; before/after snapshots equal |
| incompatible-auto | 1 | Controlled refusal; before/after snapshots equal |
| unreachable-manual | 1 | Controlled refusal in 10.14s; snapshots equal; primary B unchanged |
| unreachable-auto | 1 | Controlled refusal in 9.43s; snapshots equal; primary B unchanged |

Normal A bootstrap reached M and normal B bootstrap reached N. Successful
rollbacks checked exact sorted diagnostic key lists for api, worker and
webhook-worker, literal HTTP 200 `{"status":"ok"}` and both worker heartbeats.
All command output passed the stock secret/PEM scan, including each PEM line.
No process-local patch, source rewrite or expectation override was used.

## Cleanup and limits

The stock verifier reported cleanup PASS, including its primary/offline project
resources, ephemeral probes, B tag and successful private temporary root. The
fresh task-owned A tag/image was also removed. The gate project was torn down
with `down -v --remove-orphans`. Independent final checks found zero containers,
networks or volumes for either project and the initial preflight project, and
confirmed the exact anonymous gate volume IDs, offline network names, A/B tags
and A/B image IDs are absent. The gate credential env file, copied private Docker
config and initial preflight temporary root were removed. The separate metadata
retains the sanitized resource inventory and checks.

The host's default Compose is 2.19.1. The task used an existing official 5.6.0
binary, with its retained checksum verified, in a separate Docker config. An
initial preflight attempt used a symlink resolving outside that config; the
verifier's plugin-discovery boundary could not expose it and stopped before any
case setup, reporting cleanup PASS. Copying the same binary physically inside
the private config resolved this prerequisite. User Docker configuration and
verifier source were unchanged.

The stock verifier substitutes inspected local A/B IDs only at its documented
pull seam. Registry delivery, mutable-tag resolution, matching repository
digests and storage downloads from an empty image cache are not live-proven.
Compatible B-at-M is a deliberate fixture that replaces application services
with `up --no-deps` while skipping B bootstrap to retain M. Synthetic B proves
migration-graph refusal, not production application/schema/data compatibility;
it neither performs nor authorizes a downgrade. GitHub, LLM providers, production
secrets, edge proxy/provisioning and production deployment are outside this proof.
Build caches and preexisting shared images are not globally pruned.
