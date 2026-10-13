# Historical unpublished PR #132 round-seven checklist (#113)

The completed local tasks below apply to the unpublished `075004e` plus P3 delta
on main `2251b59`. They do not verify the later #136 integration; see the
[round-eight checklist](113-review-round8-todo.md).

See [plan](113-review-round7-plan.md) and [review](https://github.com/larchanka-training/dmc-268-api-t6/pull/132#pullrequestreview-5503707715).

Starting head: `075004e996db907b19787c91322ff8b62e43825e`; base main: `2251b5936822827fc75b38cfc6b352c6a5e89a6b`.

## GitLab origin — P3-1

- [x] Real PostgreSQL active/unknown origins exercise context, claim guard and publisher.
- [x] Literal states/error codes, project 101 gateway calls and zero GitHub/check-run calls asserted.
- [x] GitLab always-active mutation fails meaningfully; exact production source restored.
- [x] GitLab always-inactive mutation fails meaningfully; exact production source restored.
- [x] Restored focused suite: 40 passed, zero failures/skips; focused Ruff/format/mypy passed and [proof](113-review-round7-a.md) saved.

## Waiting receipt — P3-2

- [x] Waiting #121 receipt → unchanged/rebound installation → retry/redelivery regression cases added.
- [x] Same-GUID and new-GUID receipt semantics asserted.
- [x] Pending/terminal receipt fields, immutable old/new origins and messages asserted.
- [x] No installation 29 authority/provider calls or rebound new-head Run asserted.
- [x] Combined-fence mutation produces two meaningful failures; exact production files restored.
- [x] Restored full focused suite: 42 passed, zero failures/skips; focused checks and [proof](113-review-round7-b.md) complete.

## Documentation — P3-3/P3-4

- [x] Future GitLab writer origin/wording contract added in SYSTEM_DESIGN §8.4.
- [x] Round-five final/docs/plan explicitly historical with actual checkpoint claims.
- [x] Completed round-four integration/trigger/gates items marked with evidence.
- [x] Concise whole-PR body template prepared in the four required sections.
- [x] Verification template contains gate commands, new-count tokens and the round-seven final proof link.
- [x] Every document URL uses the final-head token; historical SHAs remain in proofs.
- [x] Prose/source inspection, portability sweep, section/link checks and diff check passed; [proof](113-review-round7-c.md) saved.

## Final verification and review

- [x] Temporary production mutations restored exactly.
- [x] uv run ruff check .
- [x] uv run ruff format --check .
- [x] uv run mypy .
- [x] Configured PostgreSQL/RabbitMQ uv run pytest -rs: 3196 passed, zero failures/skips.
- [x] Actual full/integration counts and [final proof](113-review-round7-final.md) recorded: 3196 full tests including 549 integration cases.
- [x] Fresh Standards and Spec reviews: no actionable findings on each axis.

## Publication evidence

This checkpoint was not published and has no review-response or required-CI
receipt. Later publication evidence belongs to the exact subsequent published head.

## External merge prerequisites

- [ ] Squash merge only; intermediate failing commits excluded from main history.
- [ ] Current-head approval at agreed time with @axyi; reviewer-owned resolution confirmed.
- [ ] Every old Run writer stopped before merge/main auto-deploy.
- [ ] If main changes before merge, rebase and verify all gates on the new exact head.
