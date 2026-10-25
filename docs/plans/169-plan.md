# Implementation plan: #169 GitLab MR intake

## Scope and status

Implement operator-managed GitLab connections for existing workspaces, durable MR
receipts, provider-specific projection/eligibility, origin-pinned queue and worker
admission, fast analysis and installed discussion publication. The implementation and
focused review increments are complete; final integrated publication/live acceptance
is not established. Final documentation review, one final full suite and a draft PR
remain. This is not an all-AC-complete or ready-to-merge claim.

Current source is integrated on main `a50be1a2b2536c742743d7224aee4b20d422cc81`;
feature anchor HEAD is `8a04e49221368d5bbe3ab0634c518719cb9af51b` plus final working
changes. Preserve upstream async GitHub Redis caching and conditional snapshot/V2/
reflection behavior; live legacy prompt inputs remain compatible until upstream #130.

## Accepted architecture and invariants

- Installation UUID, workspace and canonical HTTPS origin identify authority. Encrypted
  stored credentials use authenticated tenant/origin/key binding; webhook authentication
  uses a digest. There is no process PAT/base-URL fallback or webhook auto-onboarding.
- Supported local CLI creates/binds/inspects/rotates/revokes/activates. Rotation/revocation
  retire original active runs. Lost-key recovery inspect/revoke is database-only.
  Activation requires positive expected generation and rechecks all bindings/readiness;
  it changes only the selected intake flag and has no budget gate.
- API admission is separately default-off and requires no keyring. Replay/review workers
  share complete optional runtime settings, verified TLS and explicit private origins.
- Receipts are committed before acknowledgement and isolated by provider/installation.
  Bounded replay batches share a fair cooldown loop; confirmed publication/outbox and
  mandatory persisted-origin pointer checks precede normal lifecycle handling.
- Every provider request rechecks the original claimed workspace/installation/repository/
  code-change/generation and pinned inputs. Provider/LLM calls stay outside transactions.
  GitLab never creates GitHub check-runs or uses GitHub CI/no-CI sweep/index authority.
- GitLab supports fast only with `wait_for_ci=never`. Policy/conventions use target/start;
  source context uses fork/head; old diff reads merge-base. Complete bounded listings,
  actual sizes, immutable refs/OIDs, policy provenance and cache authority are required.
  Language percentages use recognized-language bytes; unknown/empty bytes do not add a share.
- Publication retains immutable origin/head/payload checks, bounded retries, receipts and
  markers. A stale pointer cannot terminalize a newer publication result.

## Verification and remaining work

Component tests cover these boundaries. Current-main affected verification passed 341
unit/component tests; the final stand attempt passed three groups and failed durable
receipt replay with `CanonicalMergeRequestInvalidResponse`. It did not reach model or
publication traffic. See [169-baseline.md](169-baseline.md) for exact evidence limits.

Final documentation describes installed behavior, preserves upstream policy/Redis/
monitoring/backup sections and reports the stand failure explicitly.
Documentation validation: 44 existing contract/config checks passed (two dependency
warnings); YAML remains descriptions-only and GitLab env defaults remain disabled.
Whole Ruff, format (579 files), mypy (576 files) and diff checks passed.

Next: fresh review of all six docs and three admin files; normal final commit; ONE final
full `uv run pytest` and required Ruff/format/mypy gates; honest draft PR with blockers.
No further automatic fixture-debug cycle is planned after the fourth stand attempt.

## External acceptance gate and exclusions

#171 was merged as 05a96bc4 and is included by the rebase onto main 063496ec.
Placement/suggestions, stale-head checks and finite outcomes need current integration
review. Keep intake and per-connection activation gated. Real MR/run/discussion/redelivery evidence and current-head human review
remain required; do not close #169 or claim live AC2 from this implementation.

This scope does not add GitLab OAuth/browser onboarding or portal permissions, GitLab CI,
deep/Sandbox, LEFT publication, graph indexing, deployment,
merge or production activation. Existing deployment scripts do not provision GitLab secrets.
