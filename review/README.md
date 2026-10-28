# review/ — prompts, rules and post-processing of the AI code reviewer

## Purpose

The model-facing part of the product: the prompts the reviewer runs with, the format of a
repository's custom rules with two default rule sets, and the specification of the
deterministic filter applied to the model's answer. Everything here is versioned and
immutable — a file with a version in its name is never edited. Live reviewer prompts are served
from the database; the separate snapshot verifier still loads a pinned package resource (below).

## Files

| Path                                    | What it is                                          |
| --------------------------------------- | --------------------------------------------------- |
| `prompts/review.system.v1.md`           | review prompt: order, filters, attribution, output  |
| `prompts/review.system.v2.md`           | v1 + input metadata, few-shot examples              |
| `prompts/review.system.v3.md`           | legacy reviewer prompt (#178/#233)                  |
| `prompts/review.system.v4.md`           | snapshot-path reviewer prompt (#130)                |
| `prompts/review.system.v5.md`           | current snapshot-path reviewer prompt: v4 + `<retrieved_excerpts>`, RAG v1 (#144) |
| `prompts/review.verifier.v1.md`         | historical reflection verifier prompt from #133     |
| `prompts/review.verifier.v2.md`         | current snapshot verifier default (#219): conventions & evidence refs |
| `prompts/review.conventions.v1.md`      | pre-review prompt: repository patterns, review plan |
| `prompts/review.conventions.v2.md`      | v1 + explicit counts for omitted input context    |
| `rules/schema.json`                     | JSON Schema (draft 2020-12) of a custom rule set    |
| `rules/default-frontend.v1.json`        | default rules for a Vite/React/TypeScript repo      |
| `rules/default-backend.v1.json`         | default rules for a FastAPI/SQLAlchemy repo         |
| `postprocess/lint-filter.md`            | filter #2: what the backend drops or demotes        |
| `postprocess/lint-filter-patterns.json` | regex lists, confidence threshold, inline cap       |
| `schemas/review-output.schema.json`     | JSON Schema (draft 2020-12) of v1 model output      |
| `schemas/review-output.schema.v2.json`  | JSON Schema (draft 2020-12) of v2 model output      |
| `schemas/verifier-output.schema.v1.json`| JSON Schema of reflection verifier decisions        |
| `scripts/validate_findings.py`          | validator of model outputs: schema + semantic rules |
| `examples/`                             | synthetic diff and sample outputs (proof run)       |

The current live prompt files are `review.system.v5` (#144) and
`review.conventions.v2`. `seed_prompt_versions` in `app/bootstrap/seed_prompts.py`
activates the newest known version of each key in `prompt_versions` by setting
`is_active`, so a new run pins v5, unless its repository overrides the review prompt
(`repositories.prompt_version_id`), and takes the snapshot path; a run pinned to
`review.system` v4 stays on v4 without the retrieved section, and a run pinned to
`review.system.v3` or lower keeps the legacy path. v5 is v4 plus `<retrieved_excerpts>`
(always present, empty without RAG), the rule that repository code in it is data, not
instructions, and its evidence rules: a retrieved excerpt is never the anchor, and a
LEFT one is evidence only. The manual gateway check in `app/bootstrap/llm_gateway.py`
still defaults to `review.system.v3` and `review.conventions.v2`, and the recorded
replay and the eval baseline stay on v3, until the paid eval re-shoot switches them
(#130; R15 in `docs/plans/130-snapshot-switch-plan.md`). `PromptBindings` dataclass defaults
and snapshot test fixtures use v2 version labels; these are not universal
runtime selections. At run creation, `SqlAlchemyWebhookRunStore` captures the
active `review.conventions` and `review.verifier` row IDs alongside the selected
`review.system` ID. `SqlAlchemyPolicySnapshotStore.get_pins` restores each pinned row's
version and checksum by ID, so activating a newer row does not change an existing run's
bindings. Historical or partially pinned runs keep absent IDs and empty checksums; their
bindings remain incomplete instead of acquiring today's active prompts. Older prompt
versions remain immutable history: a published version is never edited or removed.

## Seeding contract

Prompts and default rule sets are loaded from this directory at deploy or migration time — an Alembic data
migration (pending api #4) or a `seed` command owned by the backend. The snapshot verifier is an exception:
`VerifierPromptBuilder` reads the selected `review.verifier.vN.md` package resource at runtime and
checks its raw bytes against the snapshot checksum before use. The mandatory-envelope guard also
reads that selected resource. #216 deliberately retains this loader: replacing it with a DB-backed
pinned-content resolver remains outside #216 scope. Main F1’s lazy per-attempt prompt-builder
cache is preserved; runtime package I/O remains. The Docker
image carries `review/prompts`, `review/postprocess` and `review/rules` for seeding and `review/schemas` for runtime
validation of the model output (LLM gateway, api #33). Prompts are global rows of `prompt_versions`.
A `rule_versions` row belongs to one repository, so a default set becomes a repository's first `rule_versions`
row when that repository is onboarded; where the backend keeps the defaults until then is its choice.

| Column                      | Value                                                                        |
| --------------------------- | ---------------------------------------------------------------------------- |
| `prompt_versions.key`       | frontmatter `key` (`review.system`, `review.conventions`, `review.verifier`) |
| `prompt_versions.version`   | frontmatter `version`; equals the `vN` in the file name                      |
| `prompt_versions.content`   | the full file bytes, frontmatter included                                    |
| `prompt_versions.checksum`  | `sha256(content)`, hex — verifiable against the file                         |
| `prompt_versions.is_active` | managed by the backend; one active version per `key`                         |
| `rule_versions.rules`       | the `rules` array of a rule set file                                         |
| `rule_versions.version`     | the file's `version`                                                         |
| `rule_versions.checksum`    | `sha256` of the canonical `rules` JSON, hex (see below)                      |


Canonical JSON is `json.dumps(rules, sort_keys=True, separators=(",", ":"))` in UTF-8, so
the checksum is recomputable from the row. A change to a prompt or a rule set is a new file
with the next version number; the old file stays. A run references the `prompt_version_id`
and `rule_version_id` it was made with. `RepoConventionsDraft` is split on save:
`key_patterns` and `recommendations` go to `repo_conventions`, cached per
`(repository, AGENTS.md sha, prompt_version, policy_key)` and reused by every
pull request of the repository until one of them changes. `policy_key` is
the attempt's ignore key on the legacy path (NULL for a run without a policy
snapshot), so a changed ignore policy misses; on the snapshot path it is the
`repo-conventions-v2` digest (`docs/CONTEXT_AND_VERIFICATION_SPEC.md` §4.2), which
also changes with rules.md, AGENTS.md, the service defaults, the pinned conventions
prompt or the token counter. `files[]` describes one pull request and goes to
the run trace (`run_actions`); `repo_conventions.languages` is computed deterministically by
the backend and is not part of the draft.

## Assembly order

This order and the review tags of "Input envelope" below are those of the legacy path (a run pinned to
`review.system.v3` or lower); a v4 or v5 run's review request uses the envelope in "Policy snapshot v2 envelope and
reflection pass" below, and the `review.conventions` tags are the same on both paths.

One request is assembled as `system → <custom_instructions> → <agents_md> → <repo_conventions> → <pr_meta> →
<changed_files> → <omitted_files>`: the prompt file is the system message, the tags follow in this order. The
order serves the provider's prompt cache: prompt, rules and conventions form the stable prefix shared by every
run of a repository, the diff is the varying tail (`docs/SYSTEM_DESIGN.md` §10).
`review.conventions` runs first, with its own tags in the same style.

## Input envelope

- `<custom_instructions>` — the rendered rules of the active rule set; empty when none.
- `<agents_md>` — the reviewed repository's `AGENTS.md` as text; empty when none.
- `<repo_conventions>` — the `key_patterns` and `recommendations` of `review.conventions`.
- `<pr_meta>` — title, description, author, branch, base ref, labels, counts, draft/fork; from
  `review.system.v2`, also `<head_sha>` and `<commit_messages>` (one `<commit>` each) when the
  VCS client passes them.
- `<changed_files>` — the diff as XML with pre-numbered lines (below); each `<file>` carries
  `language` (by extension, absent when unknown). For `review.conventions` it lists the changed
  paths, including those the review prompt later omits — at most 100, then a line
  `[N more changed paths omitted]`.
- `<omitted_files>` — changed paths not shown to the model, one per line.
- `<repo_tree>`, `<repo_files>` — conventions prompt only: all paths, and up to 10 files of
  at most 300 lines as `<file path="…">` blocks with pre-numbered lines. A tree over the
  token budget is cut from its end and closed with `[N more paths omitted]`. In v2,
  `<repo_files>` also ends with `[N more repository file contexts omitted]` for tree files
  with no shown content, including unselected or unavailable files. A truncated `<file>`
  ends with `[N more lines omitted]`. Counts describe source input; markers are never paths.


Production commit context comes from `GitHubRunSource.get_pull_request_meta`, which
delegates retrieval to `HttpGitHubVcsProvider` through the commit-metadata port.
The provider reads at most two numeric PR-commit pages of 10 entries, retaining
first 20 messages in API order. Shared application shortening retains at most
500 Unicode characters per message, including the ellipsis. The renderer
XML-escapes text and retains its defensive 20-by-500 limits. `Link` URLs are never
followed. A next relation on the final page or a full capped second page sets
an explicit incomplete-tail flag and renders `[Additional commits may be omitted]`.
An earlier next relation is cleared by a short complete second page. A complete
empty list omits the block; an uncertain empty list renders the marker alone,
and an empty message remains an empty `<commit>` element. Larger in-memory tuples
retain the existing known-count `[N more commits omitted]` marker.

Commit pages and blobs share a capped reader. Each commit page has a 1 MiB budget
before strict nested Pydantic validation; identity encoding is requested, and
other encodings are rejected before reading or decoding to prevent decompression
allocation above that cap. Invalid pages, HTTP 5xx, 401/422, all 429 (including
without headers), other HTTP errors except 404/nonlimited 403, timeouts and other
transport/connection failures use retryable `commit_messages_fetch_failed`.
A 403 is rate-limited when `Retry-After` contains only ASCII decimal digits (seconds) or
`X-RateLimit-Remaining: 0`; 404/nonlimited 403 remain `github_forbidden`.
Diagnostics omit response text and credentials, Retry-After
is preserved, and author-facing check-runs name commit messages. Installation
checkpoints precede token acquisition and every page request; GitHub calls occur
outside database transactions.

Evaluation reads the verified frozen API-order PR prefixes from
[`eval/pr-commit-messages.json`](eval/pr-commit-messages.json) offline. All five PR
heads match their cases' pinned revisions. CLEAN-03, LOG-03 and SEC-04 each retain
one message; RES-03 and SYN-04 retain the first 20 plus the same uncertainty marker
as production. All nineteen synthetic cases retain empty history. Sidecar schema
validation is separate from canonical cohort and selected-case binding, and all
preflight checks finish before any model call or staging. Custom synthetic
subsets remain supported; new real cases require curated canonical identity and
prefix evidence. Live evaluation and replay never query GitHub.

The sidecar, the shared commit-prefix module, Link shaping, recorder/schema, existing inputs,
and every canonical `case.json` identity consulted by binding are conservatively
fingerprinted. [Run 38701845865, attempt 1](https://github.com/larchanka-training/dmc-268-api-t6/actions/runs/38701845865)
captured all 24 first raw answers on 2026-10-15 at frozen source `a1cc6229c6a6fdba65ecb640c60ca823e262a6aa`
with `mistral-small-4` and raw export. All 25 baseline files match the raw artifact
byte-for-byte and safe hashes/sizes; two offline reports are identical with no
drift and match the redacted live report. Current measured results are in the
[corpus README](../test-prs-dataset/README.md#recorded-mistral-baseline--2026-10-15)
and [test plan](../docs/TEST_PLAN.md#323-зафиксированный-post-rebase-raw-baseline-82-2026-10-15).
This complete post-rebase replacement includes the merged #149 parser/schema changes;
its static digest is `sha256-v1:77d46f9c731dc26aded94c7ec60cb860314fe7835e6041a318d63b8ddc690449`.
The [2026-10-14 PR-prefix capture](../test-prs-dataset/README.md#recorded-mistral-baseline--2026-10-14)
remains historical with original bytes/digests at revision bf09117.
The [2026-10-13 snapshot capture](https://github.com/larchanka-training/dmc-268-api-t6/actions/runs/38468081001)
remains historical evidence with its original bytes/digests at revision 76df6d4.

Rendered custom rule — one block per rule, globs space-separated, checks numbered from 1;
this example is the second rule of `rules/default-backend.v1.json`, checks shortened:

```xml
<rule name="Error Handling Standards" include="app/**/*.py" exclude="tests/** alembic/**">
1. Every external call and I/O operation (HTTP client, database, queue, …) has error handling …
2. No bare except and no except Exception that swallows the error; …
</rule>
```

Diff — one `<file>` per changed file, every line numbered; `n` is the new-version number
for `added` and `context` lines and the old-version number for `removed` lines:

```xml
<changed_files>
  <file path="app/modules/reviews/application/use_cases.py" status="modified">
    <line n="11" type="context">async def run_review(self, job_id: UUID) -> None:</line>
    <line n="12" type="removed">    findings = self._gateway.review(payload)</line>
    <line n="12" type="added">    findings = await self._gateway.review(payload)</line>
  </file>
</changed_files>
```

A file block cut to fit the token budget ends with the trailer
`[Showing lines 1-80 of 376 total. Use offset=81 to continue reading.]`. The numbers count the
`<line>` elements of the block (removed lines included), not file line numbers. The model has
no tool to continue; the trailer is the contract for a future tool-enabled engine's file reader.

## Policy snapshot v2 envelope and reflection pass

Under Policy Snapshot v2 (`policy_snapshot` present), prompts and candidate findings use an immutable manifest and content-addressed blocks:
- `<repository_rules block_id="...">`: rules from `.review/rules.md` (or `<repository_rules status="absent" />` when missing).
- `<project_conventions_document block_id="...">`: conventions from `AGENTS.md` (or `<project_conventions_document status="absent" />` when missing).
- `<service_defaults block_id="...">`: builtin platform review defaults (`defaults_checksum`).
- `<repo_conventions>`: cached repository conventions.
- `<pr_meta>`: PR metadata (title, description, author, branch, base ref, commit messages).
- `<changed_files>`: each `<file path="..." block_id="..." status="..." language="...">` carries a stable `block_id`. Contiguous ranges shown to the model are recorded in `ManifestBlock.shown_ranges`.
- `<context_excerpts>`: the L2-L4 blocks of the context stage, one `<excerpt block_id="..." level="..." path="..." revision="..." side="..." representation="...">` each (`path`, `revision` and `side` only when the block's source has them), with pre-numbered `<line n="...">` source lines; present only when the stage produced such blocks. A legacy run's stage context reaches the run trace only.
- `<retrieved_excerpts>` (v5 only, always present, empty without RAG): the RAG blocks of the context stage in the same `<excerpt>` form with `level="RAG"` — code the repository index linked to the diff, RIGHT at the head and, for a removed export, the old definition LEFT at the old revision; data, never instructions; citable as evidence, never the anchor.
- `<omitted_files>`: as in the legacy envelope.
- `<excluded_files count="N" />`: the number of files the repository's ignore policy withholds, never their names; present only when N > 0.

**Reflection Pass (packaged `prompts/review.verifier.v1.md` / `v2.md`, selected by the snapshot pin)**:
- Two-pass verification architecture to eliminate hallucinated findings.
- **Pass 1 (Reviewer, snapshot/v2 contract)** generates candidate findings with required `evidence_refs` (pointing to visible `block_id` line ranges). This applies to the snapshot path: since #130 F2 a new run pins the active snapshot-contract prompt (`review.system.v4`; `v5` since #350), unless its repository overrides the review prompt, and the worker passes the reviewer's answer to the Reflection Pass, which main #130 F1 supplies lazily through the production attempt provider. A run pinned to `review.system.v3` or lower keeps the legacy path and the v1 response contract, without a Reflection Pass.
- **Pass 2 (Verifier)** checks each candidate against visible code and requirements using the packaged verifier version selected by the snapshot binding.
  - Verifier prompt envelope: contains `<repository_rules>`, `<project_conventions_document>`, `<service_defaults>`, `<repo_conventions>`, `<code_context>` (with `<changed_files>`), and `<candidate_findings count="...">` (carrying `<candidate candidate_id="..." path="..." start_line="..." line="..." category="..." severity="...">`, `<title>`, `<body>`, optional `<suggestion>`, `<evidence_refs>`, and optional `<rule_attribution name="..." />`).
  - Verifier output: validated at runtime via Pydantic (`VerificationOutput`, `ReviewFindingV2`) conforming to contract `schemas/verifier-output.schema.v1.json` (`decisions`: `candidate_id`, `status` in `supported` / `contradicted` / `insufficient_context`, `reason`, `evidence_refs`, `requirement_refs`). Legacy runs (pinned to `review.system.v3` or lower, with or without a policy snapshot) continue to process v1 output.
  - Strict filtering: only candidates with `status="supported"` survive; unverified candidates are dropped, and the review summary is cleansed of hallucinated defects.

The legacy `parse_review_output` entry point silently removes each finding's `evidence_refs`,
even when the input looks like v2, then validates the remaining v1 object. It does not validate
evidence against a manifest or preserve it on parsed findings. `parse_review_output_v2` is the
separate evidence-aware contract. Snapshot presence alone does not enable v2 attribution in
`SqlAlchemyReviewOutputRepository.get_post_processing_input`: this legacy adapter passes
`allowed_v2_rules=None` inline, because it has no verified manifest.
The explicit snapshot pipeline carries its allowed attribution names in `VerifiedReviewOutput`.

Verifier diagnostics also depend on the composition. #133 logged candidate IDs/statuses rather
than durable structured explanations. The current explicit pipeline carries drop codes in
`VerifiedReviewOutput`; publication can store them as `Finding.drop_reason`. After #290,
`StoreReviewOutput` can persist a separate `llm.verify` run action with candidate IDs, accepted
call ID and decisions/evidence/requirements. That trace excludes the verifier's free-text `reason`
and carries the stored manifest of the accepted verifier call as `manifest_id` (null only
when the composition has no manifest store). The storage paths alone do not select the verifier: main F1 wires its lazy pass, and the pinned review version selects the snapshot path.

## Rule sets

`rules/schema.json` defines `RuleSet{version, stack, rules[]}` and `Rule{name, include[], exclude[], checks[],
severity_hint?}`; `additionalProperties` is false everywhere, names are unique within a set.
`rule_versions.rules` holds the `rules` array of a repository's active set: a default set first, the
repository's own rules as a new version. A rule's `name` is quoted verbatim in the attribution prefix.

## Post-processing hand-off

The model's JSON is parsed and schema-checked by the backend (JSON shape, enums, lengths —
not the attribution binding, which is repaired by the filter, step 7), then passed through
the filter specified in [`postprocess/lint-filter.md`](postprocess/lint-filter.md) with the
values of `postprocess/lint-filter-patterns.json`: lint-class drop, dedup, severity
threshold, confidence threshold, hunk validation, inline cap, attribution consistency. Its
buckets `inline`, `body_only` and `dropped` feed publication; a dropped finding is never
published.

## Evaluation

The shape of a review output (`ReviewOutput`: `findings` ≤ 10 and `summary`) is defined by
[`schemas/review-output.schema.json`](schemas/review-output.schema.json) (JSON Schema draft 2020-12), the single
source of truth for the v1 shape: every key required, nullable keys present as `null`, no other keys. The runtime
Pydantic model (`app/modules/reviews/application/review_output.py`) mirrors it one to one, checked by
`tests/test_review_output_schema.py`. Rules the schema cannot express (finding order, `start_line < line`, a
one-line title without a trailing period, the `rule_name` ↔ attribution-prefix binding) are listed in
`docs/PIPELINE_SPEC.md` §9.

For Policy Snapshot v2, [`schemas/review-output.schema.v2.json`](schemas/review-output.schema.v2.json) defines
`ReviewOutputV2` with required `evidence_refs` (array of `EvidenceRef{block_id, start_line, end_line}`)
and closed `rule_name` attribution (`.review/rules.md`, `AGENTS.md`, `service-defaults:<ID>`, or null; the lexical domain is enforced by the schema pattern and runtime Pydantic models).
[`schemas/verifier-output.schema.v1.json`](schemas/verifier-output.schema.v1.json) defines the reflection pass contract
(`decisions[]` with `candidate_id`, `status`, `reason`, `evidence_refs`, and `requirement_refs`).


`uv run python review/scripts/validate_findings.py <json>` checks one model output: the shape against the
schema, then the semantic rules on top. Exit `0` when valid, `1` when it violates the contract (violations
printed one per line), `2` when the file is not JSON or its kind is unknown. One command checks the sample
(prints `OK ReviewOutput 5 items`):
`uv run python review/scripts/validate_findings.py review/examples/findings.sample.json`. The kind is
detected from the top-level key: `findings` → `ReviewOutput`, `files` →
`RepoConventionsDraft`. A file carrying both keys is rejected with exit `2`. `--schema v2` checks the
answer of a snapshot-path prompt (v4/v5) as `ReviewOutputV2`, the v2 schema and then the runtime model
(prints `OK ReviewOutputV2 <n> items`). The 120-word `body` limit of
`review.system.v2.md` §7 is a prompt-level brevity target, not part of the backend schema: the validator
enforces only the 1200-character ceiling. The tests run it over `examples/*.sample.json`. Quality targets and
the evaluation corpus (`test-prs-dataset/`) are defined in [`docs/TEST_PLAN.md`](../docs/TEST_PLAN.md) §3–5.

### Proof-run record

Bot: `claude-sonnet-5`, Claude Code subagent, clean context. Date: 2026-09-19. 0/5 custom-rule attributions on
the backend diff vs 4/5 on the frontend: an attribution-metric signal (not yet defined in [`docs/TEST_PLAN.md`](../docs/TEST_PLAN.md)).

| Diff                                      | Files | Findings | Of which custom-rule | Validator |
| ----------------------------------------- | ----- | -------- | -------------------- | --------- |
| api PR #4 code diff                       | 41    | 5        | 0                    | exit 0    |
| ui PR #31 `src/{entities,widgets,shared}` | 46    | 5        | 4                    | exit 0    |
| `examples/sample.diff`                    | 3     | 5        | 3                    | exit 0    |

The sample outputs were amended by hand after the sample diff was corrected (`except:` →
`except Exception:`; 2026-09-21: async tests rewritten to `asyncio.run`). The fixtures in
`examples/` must stay in sync with `sample.diff`: after any edit to the diff, revise
`conventions.sample.json` `key_patterns` and `findings.sample.json` line anchors, then re-run
the validator and `uv run pytest tests/test_review_artifacts.py`.

## Open questions

- **Model choice** (`docs/SYSTEM_DESIGN.md` OQ-2): closed by #46 — primary `mistral-small-4`,
  fallback `mistral-small-3.2-24b`; strict `json_schema` on the serving EUrouter routes is
  confirmed by run 37304578603 of the `LLM live run` workflow (SD §15). The prompts stay
  model-agnostic.
- **Output language**: English by default (stated in both prompts). Proposed override: a
  line `Review language: xx` in the reviewed repository's `AGENTS.md`, honoured by a later
  prompt version. Decision pending with the team.
- **Default rule set**: how the backend picks a repository's first set (the `stack` of
  `rules/default-*.v1.json`) is open; proposal: by dominant language. Decision pending with role 6.

## Few-shot examples

`review.system.v2.md` §11 holds three input → answer pairs (one with no findings). They sit in
the system message — the stable prefix of the provider's prompt cache — and are versioned with
the prompt (Р-6): a change is a new prompt file. They are synthetic: never taken from
`examples/` (proof-run fixtures) nor from the eval dataset of #30.
`tests/test_review_prompt_examples.py` runs every answer through the gateway's parse path.

## Ownership

Role 7 (DevTools) authors the prompts, the rule format, the default rule sets and the filter
specification. Role 6 (backend) loads them into `prompt_versions` and `rule_versions`,
assembles the envelope and implements `FindingsPostProcessor` to this spec. The backend
calls the model only through the ports `ReviewModel` and `ConventionsModel`; their
implementation — the LLM gateway (`app/modules/reviews/infrastructure/llm/`: providers, key
rotation, fallback model, structured output, token budget, usage and `llm.call` trace) — is
owned by Engineer 4 (api #33).
