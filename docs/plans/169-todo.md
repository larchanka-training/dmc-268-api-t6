# Task state: #169 GitLab MR intake

Checked items describe implemented/reviewed source boundaries, not full issue acceptance.

## Implemented

- [x] Provider/installation/repository identity and ownership constraints.
- [x] Encrypted per-connection credential/keyring storage and digest-only webhook secrets.
- [x] Operator create/bind/inspect/rotate/revoke/recovery and generation-checked activation.
- [x] Default-off strict webhook transport, durable receipts and provider-isolated replay.
- [x] Detached/locked authority, canonical MR projection and GitLab-specific eligibility.
- [x] Origin-pinned enqueue/outbox, lifecycle/rerun/cancellation and persisted queue guards.
- [x] GitHub-only check-runs; GitLab fast-only worker with mandatory policy/context sources.
- [x] Immutable fork/diff/tree/blob reads, target/start conventions and completeness gates.
- [x] Installed publication resources and original-origin/head/payload retirement fencing.
- [x] Optional shared worker settings, API-only admission flag and resource lifetime controls.
- [x] Integrate current main async GitHub cache, verifier contracts and existing behavior.
- [x] Prepare final documentation and compact current plan/evidence state.

## Verification and delivery gates

- [x] Focused component review increments and current-main affected checks completed.
- [x] Final planned stand attempt completed; its independent closure audit passed and
      all source bytes were unchanged. This is an observed failed stand, not acceptance.
- [ ] Final docs: fresh Standards and Spec review of the entire six-plus-three increment.
- [ ] Normal final commit and ONE full current-source `uv run pytest`, Ruff/format/mypy gates.
- [ ] Draft PR with exact known blockers, validation and current-head human review.
- [ ] Resolve the observed durable replay `CanonicalMergeRequestInvalidResponse` blocker
      through an explicitly authorized follow-up; no automatic fixture-debug loop.
- [ ] Establish nonempty integrated inline/summary and confirmed retry/redelivery,
      retirement/sibling and generation-two publication evidence.
- [ ] Actual merged/rebased #171 dependency evidence, then explicit selective live setup
      and real MR/run/discussion/redelivery acceptance. Intake stays default-off.

## Current evidence

Current-main affected selection: 341 passed, nine integration cases deselected. Final
stand: 27 passed and one failed assertion; trust, tree/policy revisions and incomplete
source groups passed. GitLab-only worker group failed at prepared durable receipt replay;
configured-GitHub/retry/redelivery, retirement/sibling and generation-two groups were
unreached. No model/publication traffic or HTTP-ingress/live acceptance was established.
Documentation validation: 44 existing contract/config checks passed (two dependency
warnings); YAML remains descriptions-only and GitLab env defaults remain disabled.
Whole Ruff, format (579 files), mypy (576 files) and diff checks passed.

No dependency, schema, production rollout, merge or issue closure is authorized here.
See [169-plan.md](169-plan.md) and [169-baseline.md](169-baseline.md).
