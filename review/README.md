# review/ — prompts, rules and post-processing of the AI code reviewer

## Purpose

The model-facing part of the product: the prompts the reviewer runs with, the format of a
repository's custom rules with two default rule sets, and the specification of the
deterministic filter applied to the model's answer. Everything here is versioned and
immutable — a file with a version in its name is never edited — and served from the database.

## Files

| Path                                    | What it is                                        |
| --------------------------------------- | ------------------------------------------------- |
| `prompts/review.system.v1.md`           | review prompt: order, filters, attribution, output |
| `prompts/review.conventions.v1.md`      | pre-review prompt: repository patterns, review plan |
| `rules/schema.json`                     | JSON Schema (draft 2020-12) of a custom rule set  |
| `rules/default-frontend.v1.json`        | default rules for a Vite/React/TypeScript repo    |
| `rules/default-backend.v1.json`         | default rules for a FastAPI/SQLAlchemy repo       |
| `postprocess/lint-filter.md`            | filter #2: what the backend drops or demotes      |
| `postprocess/lint-filter-patterns.json` | regex lists, confidence threshold, inline cap     |
| `scripts/validate_findings.py`          | stdlib validator of model outputs (with the tests) |
| `examples/`                             | synthetic diff and sample outputs (proof run)     |

## Seeding contract

Prompts and default rule sets are loaded from this directory at deploy or migration time — an Alembic data
migration (pending api #4) or a `seed` command owned by the backend — never read from disk at runtime: `review/`
is not part of the Docker image, `Dockerfile` copies only `app/`. Prompts are global rows of `prompt_versions`.
A `rule_versions` row belongs to one repository, so a default set becomes a repository's first `rule_versions`
row when that repository is onboarded; where the backend keeps the defaults until then is its choice.

| Column                      | Value                                                     |
| --------------------------- | --------------------------------------------------------- |
| `prompt_versions.key`       | frontmatter `key` (`review.system`, `review.conventions`) |
| `prompt_versions.version`   | frontmatter `version`; equals the `vN` in the file name   |
| `prompt_versions.content`   | the full file bytes, frontmatter included                 |
| `prompt_versions.checksum`  | `sha256(content)`, hex — verifiable against the file      |
| `prompt_versions.is_active` | managed by the backend; one active version per `key`      |
| `rule_versions.rules`       | the `rules` array of a rule set file                      |
| `rule_versions.version`     | the file's `version`                                      |
| `rule_versions.checksum`    | `sha256` of the canonical `rules` JSON, hex (see below)   |

Canonical JSON is `json.dumps(rules, sort_keys=True, separators=(",", ":"))` in UTF-8, so
the checksum is recomputable from the row. A change to a prompt or a rule set is a new file
with the next version number; the old file stays. A run references the `prompt_version_id`
and `rule_version_id` it was made with. `RepoConventionsDraft` is split on save:
`key_patterns` and `recommendations` go to `repo_conventions`, cached per `(repository,
AGENTS.md sha, prompt_version)` and reused by every pull request of the repository until
`AGENTS.md` or the prompt version changes; `files[]` describes one pull request and goes to
the run trace (`run_actions`); `repo_conventions.languages` is computed deterministically by
the backend and is not part of the draft.

## Assembly order

One request is assembled as `system → <custom_instructions> → <agents_md> → <repo_conventions> → <pr_meta> →
<changed_files> → <omitted_files>`: the prompt file is the system message, the tags follow in this order. The
order serves the provider's prompt cache: prompt, rules and conventions form the stable prefix shared by every
run of a repository, the diff is the varying tail (`docs/SYSTEM_DESIGN.md` §10 of the ui repository).
`review.conventions` runs first, with its own tags in the same style.

## Input envelope

- `<custom_instructions>` — the rendered rules of the active rule set; empty when none.
- `<agents_md>` — the reviewed repository's `AGENTS.md` as text; empty when none.
- `<repo_conventions>` — the `key_patterns` and `recommendations` of `review.conventions`.
- `<pr_meta>` — title, description, author, branch, base ref, labels, counts, draft/fork.
- `<changed_files>` — the diff as XML with pre-numbered lines (below). For `review.conventions`
  it lists every changed path, including those the review prompt later omits.
- `<omitted_files>` — changed paths not shown to the model, one per line.
- `<repo_tree>`, `<repo_files>` — conventions prompt only: all paths, and up to 10 files of
  at most 300 lines as `<file path="…">` blocks with pre-numbered lines.

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
`[Showing lines 260-339 of 376 total. Use offset=340 to continue reading.]`. The model has no
tool to continue; the trailer is the contract for a future tool-enabled engine's file reader.

## Rule sets

`rules/schema.json` defines `RuleSet{version, stack, rules[]}` and `Rule{name, include[], exclude[], checks[],
severity_hint?}`; `additionalProperties` is false everywhere, names are unique within a set.
`rule_versions.rules` holds the `rules` array of a repository's active set: a default set first, the
repository's own rules as a new version. A rule's `name` is quoted verbatim in the attribution prefix.

## Post-processing hand-off

The model's JSON is parsed and schema-checked by the backend, then passed through the
filter specified in [`postprocess/lint-filter.md`](postprocess/lint-filter.md) with the
values of `postprocess/lint-filter-patterns.json`: lint-class drop, dedup, confidence
threshold, hunk validation, inline cap, attribution consistency. Its buckets `inline`,
`body_only` and `dropped` feed publication; a dropped finding is never published.

## Evaluation

`uv run python review/scripts/validate_findings.py <json>` checks one model output: exit `0` when valid, `1`
when it violates the contract (violations printed one per line), `2` when the file is not JSON or its kind is
unknown. The kind is detected from the top-level key: `findings` → `ReviewOutput`, `files` →
`RepoConventionsDraft`. The tests run it over `examples/*.sample.json`. Quality targets and the golden dataset
are defined in `docs/TEST_PLAN.md` §3–4 (pending #25, ui repository).

### Proof-run record

Bot: `claude-sonnet-5`, Claude Code subagent, clean context. Date: 2026-09-19. 0/5 custom-rule attributions on
the backend diff vs 4/5 on the frontend: an attribution-metric signal (`docs/TEST_PLAN.md` §3, pending #25).

| Diff                                       | Files | Findings | Of which custom-rule | Validator |
| ------------------------------------------- | ----- | -------- | --------------------- | --------- |
| api PR #4 code diff                        | 41    | 5        | 0                     | exit 0    |
| ui PR #31 `src/{entities,widgets,shared}`  | 46    | 5        | 4                     | exit 0    |
| `examples/sample.diff`                     | 3     | 5        | 3                     | exit 0    |

## Open questions

- **Model choice** (`docs/SYSTEM_DESIGN.md` OQ-2): the prompts are model-agnostic; model
  and token budget are a separate task. The proof-run record names the model used.
- **Output language**: English by default (stated in both prompts). Proposed override: a
  line `Review language: xx` in the reviewed repository's `AGENTS.md`, honoured by a later
  prompt version. Decision pending with the team.
- **Default rule set**: how the backend picks a repository's first set (the `stack` of
  `rules/default-*.v1.json`) is open; proposal: by dominant language. Decision pending with role 6.

## Ownership

Role 7 (DevTools) authors the prompts, the rule format, the default rule sets and the filter
specification. Role 6 (backend) loads them into `prompt_versions` and `rule_versions`,
assembles the envelope, calls the model and implements `FindingsPostProcessor` to this spec.
