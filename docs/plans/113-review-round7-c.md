# Round-seven documentation proof — PR #132 / #113

## Scope and source inspection

Addresses P3-3/P3-4 in the
[2026-10-13 review](https://github.com/larchanka-training/dmc-268-api-t6/pull/132#pullrequestreview-5503707715).
Inspection used immutable starting head
`075004e996db907b19787c91322ff8b62e43825e` on main
`2251b5936822827fc75b38cfc6b352c6a5e89a6b`, as the production-source verification boundary.

`Run.origin_provider_installation_id` is nullable with no default. Both SQL and
object origin guards require its match to the physical installation and current
Repository installation, an undeleted installation and available Repository.
Publication terminalizes inactive origin as `cancelled/installation_unavailable`.
PIPELINE_SPEC's current message for that code names the GitHub App. SYSTEM_DESIGN
§8.4 now requires future GitLab ingestion writers to set origin at creation and
calls for non-GitHub wording when ingestion is implemented. GitLab publication
remains implemented; webhook/diff/blob ingestion remains future work. No runtime
behavior, public code or dependency changed for these documentation findings.

## Historical evidence and prepared PR body

The round-five final, documentation and plan files now identify themselves as
historical evidence for `b5e0cec92c24a8e702a90e75de5a10350f89cdaa` on `11ddfab`.
Their original SHAs, counts and timings remain. Present/future verification claims
were replaced with completed round-five source checks, gates, hooks and
[CI run 38373267208](https://github.com/larchanka-training/dmc-268-api-t6/actions/runs/38373267208).
The round-four checklist marks merged #121/#126, typed trigger/original-installation
preservation and combined-head receipt/gate verification complete, citing the
round-five/six proofs. Human approval, writer shutdown and merge timing remain
external prerequisites. Historical evidence does not verify a later head.

The prepared whole-PR body keeps What / Why / How to verify / Refs and retains
technical behavior, migrations and material accepted limits. It removes the
publication diary and historical result generations. How to verify contains only
the new head, gate commands/counts and the round-seven final proof. All repository
document URLs use the final-head token, including historical proof links.
Current verification identifies the final source; no older counts are asserted
for the new source. Squash-only merge, stopping writers before merge/main auto-deploy, agreed
approval timing and rebase/gates if main moves are recorded outside verification.

## Checks

- Reviewed all changed prose and compared historical results with existing round-five/six evidence.
- Verified body section order, final-head tokens and existing document targets.
- Portability/credential sweep of changed documentation and the prepared body found no private paths, local service addresses or credential-bearing DSNs.
- `git diff --check` passed.

These documentation corrections change no runtime source or tests. Combined
verification is recorded in the [final proof](113-review-round7-final.md).
