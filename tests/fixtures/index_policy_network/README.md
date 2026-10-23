# Index policy over real HTTP (#207)

This opt-in proof calls the actual `GitHubRepositorySnapshotSource.rules_document`
through `httpx.AsyncClient` and a localhost Postman simulation. It does not replace
HTTP with `MockTransport`, use credentials, start the app, or need PostgreSQL.
The fake installation token is only accepted by this fixture. Unmodeled requests
answer 501, so an unexpected call cannot look like authoritative absence.

## Reproduce

Requires the repository's `uv` environment and Postman CLI >=1.70 (verified with
1.74.0). No Postman account or workspace is required. From the repository root,
start in one terminal, using an explicit free port:

```bash
postman simulation run tests/fixtures/index_policy_network/index-policy.sim.yaml --port 4907 --no-history --output ndjson > /tmp/207-index-policy-sim.ndjson 2> /tmp/207-index-policy-sim.err
```

The foreground server intentionally stays running. In a second terminal, confirm
the `listening` event and run the proof:

```bash
rg '"event":"listening"' /tmp/207-index-policy-sim.ndjson
uv run python -m scripts.verify_index_policy_network --base-url http://127.0.0.1:4907
```

The runner accepts only `http://127.0.0.1:<explicit-port>`. The simulation binds
localhost only. Header routing preserves the production `/repos/...` paths.
If 4907 is occupied, use the same other free port in both commands; an explicit
busy port fails instead of silently choosing another. Restart the simulation
after fixture edits; start each proof with a fresh log. Stop after the run:

```bash
pkill -INT -f 'simulation run tests/fixtures/index_policy_network/index-policy.sim.yaml'
```

Inspect `request` events and the final `summary` in the NDJSON log. Do not commit
runtime logs; all requests use fabricated repository names and object IDs.

## Expected proof

| Case | Outcome | Actual HTTP calls |
| --- | --- | ---: |
| hidden | present; `private/**` excludes `private/secret.py` | 3 |
| missing-root | missing, empty custom masks | 1 |
| missing-rules | missing, empty custom masks | 2 |
| blob-404 | `policy_revision_unavailable` | 3 |
| blob-500 | `policy_fetch_transient` | 3 |
| truncated | `policy_revision_unavailable` | 1 |
| root-404 | `policy_revision_unavailable` | 1 |

Success prints `PASS: 7 cases, 14 real HTTP requests`. Adapter counters and exact
request paths are asserted for every case, including failures. Trees are
nonrecursive and pinned to commit `cccc...`; subtrees and raw blobs use immutable
object IDs. Root `AGENTS.md` is deliberately visible; its blob must never be read.
There are no HEAD/branch, Contents API or archive-download calls in this proof.
The server log should contain 14 request events: 11 successful and 3 failed HTTP
responses. The successful but truncated tree still produces a policy error.

The runner constructs a synthetic gzip archive with `.review/rules.md` absent,
`.gitattributes` specifying `.review export-ignore`, and `private/secret.py`
present. The real archive reader confirms that shape. The real ignore parser and
matcher then apply the authoritative HTTP document and exclude that target.
This checks the adapter/parser seam; it does not establish actual Git archive
attribute processing, index publication preservation, DB transaction boundaries,
or full/incremental row parity. Those require the use-case and PostgreSQL tests.

## Contract provenance and skips

The hand-written fixture models the existing repository contracts in
`app/modules/reviews/infrastructure/github_policy_source.py`,
`app/modules/code_index/infrastructure/github_snapshot_source.py`, and
`tests/test_github_snapshot_source.py`. It uses GitHub's documented
[Get a tree](https://docs.github.com/en/rest/git/trees#get-a-tree) nonrecursive
`tree`/`truncated` shape and
[Get a blob](https://docs.github.com/en/rest/git/blobs#get-a-blob) raw bytes for
`Accept: application/vnd.github.raw+json`, API version `2022-11-28`.
The data and object IDs are fabricated; this is a bounded contract fixture,
not a full GitHub mock or a live GitHub integration check.

Owner-workspace mock discovery was skipped: `postman whoami` reported no account
for this CLI session. The fixture was built locally from the code and documented
contract, without cloud search, push, deploy, or start-history writes.
