# Installed GitLab local network proof

This package runs the real installed FastAPI-service review worker, SQL, RabbitMQ,
shared model gateway and publication adapters against local consumed-contract GitLab
and model HTTP fixtures. It is a synthetic predecessor component proof. It cannot
establish integrated #171 behavior, deployed intake or live GitLab acceptance.

Source preparation and runtime are separate gates. Before explicit runtime authorization,
only this offline check is appropriate:

```sh
uv run python scripts/gitlab_network_proof_launcher.py --offline-check
```

After the complete ten-file source package passes independent reviews and the resource
owner explicitly releases runtime, use one new private directory outside every Git tree:

```sh
uv run python scripts/gitlab_network_proof_launcher.py \
  --scratch /private/tmp/CHOOSE_NEW_PRIVATE_DIRECTORY \
  --database-url-env TEST_DATABASE_URL --rabbitmq-url-env TEST_RABBITMQ_URL \
  --postman-port 49400 --tls-port 49443 --runtime-timeout 600 --audit-timeout 30 \
  --source-manifest /private/tmp/FINAL_REVIEWED_SOURCE_SCOPE.json
```

For a later **reviewed committed checkout**, create a private snapshot and matching
scope with the following recipe. Set `REVIEWED_COMMIT` to the approved commit and
`PRIVATE_SCOPE_DIR` to a new destination outside every Git tree. Do not execute this
preparation or the runtime command while runtime is held. Our current uncommitted
agent run uses the coordinator's final full scope instead, including all approved files.

```sh
uv run python - <<'PY_SCOPE'
import hashlib
import json
import os
from pathlib import Path
import subprocess


def git(*args):
    return subprocess.check_output(["git", *args])


checkout = Path(git("rev-parse", "--show-toplevel").decode().strip()).resolve()
reviewed = os.environ["REVIEWED_COMMIT"]
assert git("rev-parse", "HEAD").decode().strip() == reviewed
subprocess.run(["git", "diff", "--exit-code", "HEAD", "--"], check=True)
private = Path(os.environ["PRIVATE_SCOPE_DIR"]).resolve()
assert not any((path / ".git").exists() for path in (private, *private.parents))
private.mkdir(mode=0o700, exist_ok=False)
snapshot = private / "snapshot"
snapshot.mkdir(mode=0o700)
hashes = {}
for record in git("ls-files", "--stage", "-z").split(b"\0"):
    if not record:
        continue
    metadata, raw_name = record.split(b"\t", 1)
    mode, oid, stage = metadata.decode().split()
    assert stage == "0" and mode in {"100644", "100755"}, "no symlink/submodule copying"
    name = raw_name.decode()
    relative = Path(name)
    if relative.name.startswith(".env") or relative.suffix.lower() in {
        ".key", ".pem", ".p12", ".pfx", ".csr",
    }:
        continue
    destination = snapshot / relative
    assert destination.resolve().is_relative_to(snapshot)
    data = git("cat-file", "blob", oid)
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    destination.write_bytes(data)
    destination.chmod(0o600)
    hashes[name] = hashlib.sha256(data).hexdigest()
scope = private / "scope.json"
scope.write_text(json.dumps({
    "checkout": str(checkout), "head": reviewed, "snapshot": str(snapshot),
    "paths": sorted(hashes), "hashes": hashes,
}, indent=2) + "\n")
scope.chmod(0o600)
print(scope)
PY_SCOPE
```

Only indexed regular files at the reviewed commit enter this snapshot; untracked files,
.env files and key/certificate payloads are excluded. A tracked symlink/submodule rejects
the recipe instead of following it. The resulting scope path supplies `--source-manifest`;
choose a different new private directory for `--scratch`.

Prerequisites: synchronized existing uv environment; local Postman CLI at least 1.70
(previous contract evidence used 1.74), OpenSSL, Docker, the exclusively reserved
PostgreSQL at 127.0.0.1:49323 and RabbitMQ at 127.0.0.1:58447/vhost dmc169-test.
The retained local Rabbit container is `dmc-268-169-rabbitmq`; `--rabbitmq-container`
may identify the owner-verified equivalent. Database and broker values come from the
named environment variables; no credential belongs in the command or this repository.
Existing containers remain running. No login, hosted search, push, deploy, cloud or
real provider/model credential is used. Owner mock discovery was skipped without login;
contracts derive from the consumed GitLab REST routes, existing OpenAI-compatible
shared gateway and previously reviewed local T12/T10/T14b fixtures.

`manifest.json` is nonsecret and fixes exact bytes, real blob OIDs, distinct head/start/
mergebase refs, target/start and head trees, 300 Python/100 JavaScript/57 unknown/0 empty
bytes and literal 75/25 known-language shares. The nonempty model finding is on added
line 2 of app/proof.py, uses the installed Error Handling Standards rule and passes the
actual deterministic filters. Replies retain the working RepoConventionsDraft and
ReviewOutput envelopes. Later runs may reuse SQL conventions: ledger assertions compare
actual observed tasks, rather than requiring two model requests for every run.

The GitLab fixture accepts only the original generated token for its exact origin,
project and current generation; candidate management validation is scoped separately.
It retains discussion writes and markers for retry/readback. Unknown methods/routes
return 501. Postman header members are GitLab and model only. The TLS front forwards the
original method/path/query/body/token unchanged and adds fixture routing metadata.
Both localhost and 127.0.0.1 are certificate SANs and distinct origin identities.
Both generated synthetic CAs include critical keyCertSign/cRLSign key usage; CA constraints
and the server SAN are retained. Ordinary strict/hostname/CERT_REQUIRED verification stays on.
A/B bind target17/source29 at those different origins; C/D/E bind target18/19/20
and source30/31/32 at localhost. The manifest fixes these reservation-safe scopes;
management, canonical MR, target/start and source/head routes use the same IDs. The
configured GitHub route is an actual rejecting HTTPS endpoint; GL runs must never call
it. Python socket observation covers runner/worker processes, not every Postman/CLI
internal operation; it is not a claim of universal child-process egress interception.

The finite scenario groups cover trust/authority/canonical negatives, complete trees/
policy/old/new bytes, two incompleteness-before-model/cache cases, receipt-to-worker
success, configured-GH publication retry and redelivery, retirement with sibling tenant
success, and supported rotation followed by a legitimate new Run/new head. Component
source cases use labelled synthetic component Run data and a public lifecycle claim;
full pipeline cases use public receipt acceptance/replay and real confirmed Rabbit
messages. Supported CLI create/bind/activate proves synthetic verified-TLS activation;
no SQL intake-enable or revival/reset of an old Run is used. One sampled transaction
boundary observes the production worker's separate pool; it does not claim the entire
worker owns a one-connection pool. Existing T14b component pool-one proof remains separate.

All fresh credentials, keyrings, certificates, signing keys, DSNs, CLI inputs, model/
GitLab payloads, ndjson, private logs, runtime state, retained process identities and
results stay under the mode-0700 scratch directory. Parent/children preserve HOME,
home and CODEX_HOME and use a private cwd/explicit environment, avoiding repository
.env settings. The source manifest is supplied at final freeze, never a historical
fixed file count. No OS trust store is changed and certificate verification stays on. Postman may use its
own local CLI caches; this recipe is not a filesystem sandbox for CLI internals.

The supervisor reserves 25 seconds within its 600-second runtime ceiling for emergency teardown, owns/reaps recorded process groups,
then starts exactly one independent read-only audit with a 30-second ceiling (25 seconds work, five reserved
for synchronous emergency cleanup) after
confirmed runtime exit, even if peer cleanup failed. Timeout remains the primary result;
cleanup failures and actual signed child exits are retained. A surviving runtime child
withholds the audit and final-END claim. Retained group member birth identities are
checked before signals even after a leader exits. Spawn and every startup readiness poll
retain only descendants with a matching retained ancestry chain, including failed startup.
One absolute cleanup deadline bounds metadata/identity probes, signal phases and waits;
cleanup is synchronous, so no pending executor threads can overlap the independent audit.
Every explicit spawn registers a partial PID/expected-group record before identity probes;
startup faults preserve the primary error and trigger bounded owned cleanup. Audit ps/docker
commands inherit the already-owned audit group and persist private partial/birth records
before waiting. Its deadline/reserve arrive as immutable argv before spawn; it uses its
own PID/PGID, so startup does not depend on its parent publishing a last-child record.
Private JSON snapshots publish with atomic replacement, allowing readers only complete
old or new documents. Interruption catches all exit paths; final END requires confirmed empty
retained groups as well as actual leader exits. Unknown identities are never group-signalled;
a fallback can stop only the original unreaped direct Popen child. The
runtime closes clients/workers/listeners, ACKs only verified owned run pointers, deletes
only its empty 15 named queues/6 exchanges, and drops only its UUID schema. Admission
requires all six named exchanges and all queues absent plus no reserved-vhost connections;
observed attempt declarations are retained before exact cleanup. Redelivery passes only
after confirmed publication and an active worker's zero ready/unacknowledged deliveries,
with no retry/DLQ work, followed by terminal Run and unchanged marker checks. Cleanup
ACKs are never redelivery evidence. The retirement case captures the original pointer before
the supported revoke commits, records the committed wire boundary, then confirms/ACK-drains
that cancelled pointer and checks unchanged cancelled state/markers plus zero later A
provider requests through the remaining scenarios. Successful Runs compare both persisted
AGENTS/rules documents' literal bytes/text, raw/text hashes and blob OIDs with the manifest,
alongside repository and target/start policy snapshot provenance. The audit
checks retained groups, both listeners, schema/named SQL clients, exact broker topology/
reserved-vhost connections and final source bytes. It never performs global purge or
cleanup mutations. A failure stops further scenario groups and reports reached/unreached
work; it does not silently change source or retry the complete proof. Read private
summary.json, scenario-results.json, route-counts.json, command.json, proof.log and
final-independent-cleanup.json before reporting a runtime result. Source-only static
success establishes none of those future network outcomes.

A separately reviewed, authorized retirement-only follow-up can add
`--scenario retirement` to the same launcher command with a new private scratch
and current source manifest. It reuses supported CLI setup, the configured-GitHub
worker, retirement, original-pointer redelivery and the sibling-tenant assertions.
Only that scenario runs; the other groups remain `unreached`, so this is not a full
seven-group result or live acceptance. The scenario keeps its 75-second ceiling
and the launcher's 600-second runtime plus one 30-second independent audit.
The default `--scenario all` retains the complete proof. Safe retirement diagnostics
record CLI completion, cancellation error code and summary attempt count. A SQL
`cancelled` row alone does not establish that the operator CLI has returned.
