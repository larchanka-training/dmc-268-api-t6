# #214 — worker portal URL design

Issue: <https://github.com/larchanka-training/dmc-268-api-t6/issues/214>.
Implementation and review discussion: [PR #302](https://github.com/larchanka-training/dmc-268-api-t6/pull/302).
The written plan was approved before implementation. Review-round remediation
preserves its scope; current main base is `d08e6c9625ac5eb45eae45c229925fa776d82b80`.

## Goal and scope

Protect both real composed check-run publication paths against a lost run URL
argument. Reject malformed nonempty PORTAL_URL before worker resources start;
keep absent/empty configuration optional. No dependency, schema, transport,
GitLab delivery, check-run prose or deployment wiring changes.

## Configuration boundary

Validate in WorkerSettings.from_environment with a small worker-local helper
and standard-library urlsplit. Preserve valid input rather than rewriting it;
run_url_factory removes trailing slashes and appends `/runs/{UUID}`.

| Input | Result |
| --- | --- |
| Missing or exactly empty | None; worker starts without a link |
| `https://staging-ui.dmc268-t6.axyi.ru` | Accepted unchanged |
| HTTP/HTTPS, case variation, localhost, IPv4, bracketed IPv6 | Accepted with nonempty hostname |
| Explicit numeric port 1–65535 | Accepted |
| Base paths, percent-encoded paths, trailing slashes | Preserved; only trailing slashes removed before `/runs/` |
| Absent/unsupported scheme or absent host | RuntimeError naming PORTAL_URL |
| Whitespace/nonprintable characters, including U+200B; backslash | RuntimeError |
| Userinfo, query/fragment including bare delimiters | RuntimeError |
| Invalid brackets, nonnumeric/out-of-range/empty/zero port | RuntimeError |

This is a limited syntactic base-URL policy, not DNS, reachability or complete
RFC/path-encoding validation. The fixed generic error deliberately avoids
per-class diagnostic branches and never includes input or parser credentials.
Suppress the parser exception chain; the NFKC credential test protects that
privacy boundary. Keep canonical policy and recovery instructions in
[SECRETS.md](../SECRETS.md#portal_url); link CICD and architecture to it.

## Composition boundary

Execute real compose_worker_process, HandleReviewRun terminal recovery,
PublishRunReview success, URL factory, renderer and transactional trace. Replace
only infrastructure collaborators with local typed fakes. Fake gateways execute
actual checkpoints; tests independently inspect before/after transaction counts.
Failed reports have no verdict. Assert literal target UUID/details URL and the
summary URL suffix, with unset/empty cases containing no run link. Avoid locking
renderer prose. Shared fake extraction would couple independent feature tests
and is unnecessary for this narrow regression.

Independently remove each constructor's one run_url kwarg and prove its linked
case fails on details_url=None; restore exactly between mutations. Independently
remove exception suppression and prove the NFKC privacy case leaks the marker
in formatted traceback, then restore. No mutation is committed.

## Operator recovery

Startup failure breaks up --wait. Rollback retains current worker.env: a previous
release with the validator fails too; one predating #214 may start and resume
malformed links. Correct effective vars.PORTAL_URL (staging Environment overrides
repository), then deploy normally. CI rebuilds worker.env from the bundle;
host-only edits persist only for manual deployment without that bundle.

## Delivery and acceptance

Ownership coordination covers overlapping worker/operator files; the architecture
note was coordinated with #227 before editing. Keep upstream work during rebase.
Run focused tests, CI unit selection, independent mutations and all four exact
repository gates without exclusions, with dedicated services. Record actual
results in [214-verification.md](214-verification.md) and the
[checklist](214-todo.md). Full remediation gates passed with 6475 tests and
zero skips. Independent agent checks covered the review-round changes and
reported no Standards or Spec findings; this is a scoped checkpoint, not human
approval. Human current-head approval, thread resolution
by reviewers, merge and tech-lead issue closure follow the repository workflow;
no live deployment or durability verification is part of this change.
