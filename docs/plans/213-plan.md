# #213 — Proven empty additions and honest coverage

## Scope and architecture

Issue [#213](https://github.com/larchanka-training/dmc-268-api-t6/issues/213)
allows an independently proven ordinary empty addition beside fully reviewed
code without preventing complete coverage and `clean` when no supported risk above
`info` remains. Complete inventory containing only proven exclusions is `skipped`
with null verdict. Missing or truncated input remains partial; no supported finding above
`info` means a partial review has no verdict, while supported critical/high and
medium/low findings retain `blocking` and `attention` respectively.

An empty addition requires every candidate condition and independent pinned-head
proof of its exact path, empty Git blob, ordinary-file mode and zero size. The
optional GitHub tree request is bounded and runs only for candidates. GitLab has
no equivalent independent ordinary-file proof in this flow: an empty addition
there remains `missing_patch` and partial. SHA or size alone cannot grant proof.

Coverage is application-owned, reflects actual accepted model input and survives
reflection, packing, storage and reload. Required internal coverage, truthful
legacy fallback, complete inventory, ignored-path privacy and SUCCEEDED-only
public results remain intact. Checked-path membership does not establish full
coverage. Public DTOs and the model schema do not change. Network calls stay
outside database transactions; repositories flush and use cases commit.

Preserve main's provider authority, policy/context pins, accepted manifests,
verification security, publication budget, attempt metadata and SSE behavior.
Actual sent-patch loss and `unknown_path` affect #213 coverage; the broader
reflection/verifier technical-drop lifecycle and module split remain separate
#107/#130 work.

## Round 5 requirements

The [round-5 review](https://github.com/larchanka-training/dmc-268-api-t6/pull/283#pullrequestreview-5659553559)
examines `9de9f17d`. Its new P2 requires active UI documentation tracking because
UI PR #189 merged without the `clean` correction. P3 remarks require two exact
text regressions, complete canonical literals, the GitLab limitation in the
specification, stable pipeline wording, reproducible verification commands and
removal of environment traces and obsolete execution claims from these plans.
The additional snapshot-test comment requires a docstring that remains true
after F2 activation.

The user waived another plan confirmation. The existing Ready API PR #283 remains
the publication target; do not merge, close #213, add a Development-panel link or
resolve reviewer threads as author. [UI documentation PR #198](https://github.com/larchanka-training/dmc-268-ui-t6/pull/198)
supplies the active tracking link and page correction. Its core-page approval remains an explicit human
review requirement under CONTRIBUTING §8.

## Ordered implementation slices

| Slice | Acceptance criteria | Dependencies | Expected files / size |
| --- | --- | --- | --- |
| R5-1 | Coordinate overlapping open PRs, rebase on actual main, retain merged policy/F2/monitoring behavior and one Alembic head without editing merged revisions | None | Rebase conflicts and own migration/schema assertions only if required / M per conflict |
| R5-2 | Tests assert the complete `Ещё причин: {n}` line with correct count and the exact none-coverage summary; label and off-by-one mutants fail | R5-1 | `tests/test_clean_without_text_reasons.py` / S |
| R5-3a | Add missing render literals to the canonical label table without changing existing reason mappings or runtime wording | R5-2 | CVS, PIPELINE_SPEC, RULES_FORMAT_SPEC / M |
| R5-3b | Mirror the canonical block exactly and state the GitLab empty-file limitation in CVS §2.2 and its mirrors/TEST_PLAN; replace “На ветке #213” with “После #213” | R5-3a | SYSTEM_DESIGN, TEST_PLAN and relevant spec paragraphs / M |
| R5-4 | Update the live-pin test docstring for pre-F2 or pre-0039 runs bound to v3 or lower; preserve assertions and current snapshot behavior | R5-1 | `tests/test_snapshot_path_live_pin.py` / S |
| R5-5 | Read the whole user guide page, correct its `clean` row and explanation, publish a docs-only UI PR and use its active link in the API PR body | R5-1 | UI `how-it-works.mdx` and PR metadata / S |
| R5-6 | Complete fresh gates and independent Standards/Spec reviews on the final candidate; report factual results and limitations | R5-2–R5-5 | Verification and these two plans / M |
| R5-7 | Prepare the publisher handoff for one feature commit, the whole-feature PR body, exact-head CI, open P2/P3 replies and renewed review | R5-6 | Publication preparation |

Canonical labels must include lowercase `проверка неполная`, the exact `done_well`
template, `Проверено файлов с текстовыми изменениями`, `Подтверждено замечаний`
and `**Coverage:**`, taken from runtime and then treated as specification literals
for tests. Preserve all existing general and specialized reason rows and labels.
Do not change correct production behavior merely to add tests or documentation.

The UI wording covers complete text review, proven empty-only/binary-only changes,
partial input without a supported risk, partial input with a supported risk, and
legacy/unavailable coverage. It distinguishes checked text from complete inventory
and states the GitLab limitation. The UI PR requests the explicit full-page review
phrase; an automated review does not claim that approval.

Checkpoint after R5-1: verify the migration graph, inspect semantic rebase changes
and run affected settings, pins, provider and coverage regressions. Checkpoint
after R5-2–R5-4: focused tests, mutation evidence and five-spec block parity must
pass before the full suite. UI work may proceed separately after ownership checks;
Git integration and final candidate edits remain serialized.

## Verification and release protocol

Use `uv` and the repository's Python version. Run all four required gates:

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy .
uv run pytest
```

The full run must execute integration tests with PostgreSQL and RabbitMQ available;
missing prerequisites and skipped tests are not passing integration proof. Also
verify the own migration's downgrade/re-upgrade, current full migration chain,
required line/branch coverage, exact literal/count mutants, canonical block parity
and current test references. Preserve assertions, deadlines and coverage thresholds.
Run relevant corpus/rules/replay checks and describe recorded replay limitations.
Freeze source inputs for verification and both independent review axes; any later
change needs explicit scope accounting and appropriate renewed checks.

The API PR's `How to verify` contains reproducible migration, focused and
integration commands with their prerequisites, followed by actual results. Its
What / Why / How to verify / Refs describes the whole final feature, current main
integration, provider limits and active UI documentation PR. Do not use an obsolete
UI PR as the tracking destination or present historical gates as current proof.

Recheck actual main before publication. Stage only intended paths and publish one
Conventional Commit referencing #213 with normal hooks and an exact lease for any
history rewrite. Confirm CI success on that exact head before replying to the new
P2 and posting the P3 dispositions and renewed review request. Closed threads stay
closed; reviewer approval and thread resolution remain reviewer actions. Execution
receipts belong in PR evidence after the actions succeed, not as future claims in
this source document.

## Risks and mitigations

| Risk | Mitigation |
| --- | --- |
| Main advances or another PR owns the same files | Recheck main and open-PR inventory; coordinate before edits and preserve both semantic changes |
| F2 activation makes legacy prose or test descriptions false | Inspect actual main's selector, prompt pins and attempt flow; describe legacy runs without changing their assertions |
| A provider migration collides with the feature revision | Inspect only merged main's graph, renumber only the unmerged feature revision if necessary and update its expectations |
| Render labels or counts regress silently | Literal tests plus independent label and off-by-one mutations |
| Five specifications drift | Compare canonical blocks exactly and check GitLab statements alongside runtime regressions |
| User documentation tracking closes without the correction | Publish the concrete page change in its own UI PR and link that active PR |
| Old evidence is mistaken for proof of the new candidate | Separate historical context from current checked tasks and report only executed results |

## Completed round-5 verification and publisher preparation

The final candidate integrates main `430d6604` and preserves its merged F2,
policy, monitoring and provider behavior. Only the feature migration adds revision
`20261026_0043` after `20261026_0042`; merged revisions remain unchanged. Five
canonical blocks now match exactly, with 13 general reason rows, nine specialized
rows and eight render labels. The GitLab limitation and stable pipeline wording
are synchronized, and the legacy-path docstring retains every test assertion.

| Check | Actual result and scope |
| --- | --- |
| Required static gates | Ruff check passed; format check: 742 files; mypy: 732 sources clean |
| Current full `uv run pytest` | 9,402 passed; zero failures/errors/skips/duplicate nodes; two existing dependency warnings; 2,911.71 pytest seconds / 2,919.201 command seconds |
| Retained unit/coverage evidence | 7,686 passed; unchanged production, unit, configuration and dependency inputs; no fresh dedicated unit command after the integration-fixture correction |
| Required core coverage | `review_output`: 392 statements / 132 branches; `llm.answers`: 183 / 28; both 100% lines and branches on unchanged inputs |
| Targeted scope | 501 unique nodes: 277 integration and 224 non-integration; 496 retained passing nodes plus five fresh affected PostgreSQL passes; no fresh standalone 501-case command claimed |
| Full node accounting | All 7,686 unit nodes and 501 targets passed exactly once in the current full run; 1,716 additional cases versus the unit set |
| Independent review | Whole-feature Standards and Spec reviews plus the final two-path delta each have zero findings |
| Exact-label regressions | Both label mutations and the remaining-count off-by-one mutation killed; restored focused tests passed |
| PostgreSQL chain | 0043 → 0042 → 0043 → 0039 → 0043 passed with catalog checks; the probe schema was removed |
| Corpus / rules | 24/24 plus distribution checks passed / 8/8 passed |

The first candidate's targeted run had two failures caused by an F2 test fixture
with insufficient fixed-prompt room; a full run on that candidate was never
started. The correction changes only the integration fixture's input profile,
still forces real truncation and preserves all 37 assertions. Five affected
PostgreSQL cases and the entire current full suite pass. Production guards,
prompts, seed, helpers and coverage thresholds remain unchanged; the original
failure diagnostics are retained rather than relabelled as passes.

Recorded replay exited successfully with 10/24 valid responses and the two
existing static path/digest warnings. Its report is byte-identical to pristine
main `430d6604`; no complete quality baseline refresh or live/paid capture is
claimed. The controlled gateway test made two actual HTTP POSTs, both 200, each
sending 71/400 patch lines and 8,907 bytes. Tail lines stayed absent; trusted loss,
raw accepted output, usage/call parity and review/attempt-2 metadata survived.
Five imported module hashes match the candidate. This proves controlled legacy
transport scenarios, not live provider availability, answer quality or final
publication; snapshot-v4 behavior is covered by the full suite and affected
PostgreSQL cases.

The tested manifest contains 1,364 regular files and two symlinks, SHA-256
`22f9f1ccce7ce86722e9202d963165176648e5c77fd76ff59a61b4b9d4f0bd03`.
Postproofs found unchanged source inputs. This factual update changes only these
two plan documents; the other 1,362 regular files, file modes, file set and symlink
targets remain identical to the tested candidate.

[UI PR #198](https://github.com/larchanka-training/dmc-268-ui-t6/pull/198)
contains the single-page correction and passed its required gates, docs build,
2,326 tests and CI on head `88362e0a`. Full-page peer review found no issue; the
explicit human tech-lead page approval remains required and is not claimed here.
The API publisher handoff is prepared with that active tracking link, four
reproducible reviewer commands, the whole-feature body, actual evidence and the
P2/P3/docstring dispositions. Execute the release protocol above; successful
commit, push, exact-head CI, replies and renewed review receipts are recorded in
the PR after execution, without another source amendment solely for those events.

## Historical context

Earlier rounds implemented strict empty-file proof, durable loss/coverage handling,
public read gates, supported-risk verdicts and provider integration. Published
`9de9f17d` had a 9,229-test full pass and green CI; the round-5 reviewer found no
new code defect and killed all 24 mandatory mutants. Those are historical results
for that head, not validation of the rebased round-5 candidate. Detailed diagnostics
and execution receipts remain in the PR discussion rather than this design plan.
