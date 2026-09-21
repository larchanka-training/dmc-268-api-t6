# Lint filter #2 — deterministic post-processing of review findings

This is filter #2. Filter #1 is the `Do not report` section of the `review.system` prompt
(`../prompts/review.system.v1.md`, section 3), which asks the model not to report what a
linter, formatter or type checker enforces. This document specifies what the backend's
`FindingsPostProcessor` does with the model's answer afterwards, so that the product does
not depend on the model obeying filter #1. The filter is pure: same input, same output; no
model call, no network. The backend owns the implementation; this file owns the behaviour.

## Inputs

- `ReviewOutput` as returned by the model and already parsed and schema-checked by the
  backend (`findings[]`, `summary`). A malformed answer never reaches this filter.
  Attribution consistency (a prefix that names no rule, or a `rule_name` the body does not
  quote) is not a schema error: it reaches step 6 and is repaired there.
  `scripts/validate_findings.py` rejects the same case deliberately — it is the strict gate
  for proof-runs and fixtures, not the runtime contract.
- The run's diff: for every changed file, the set of new-version line numbers that lie
  inside a hunk (`added` and `context` lines of the unified diff). Lines the backend added
  around hunks as extra context are not anchors.
- The names of the rules in the run's rule set (`rule_versions.rules[].name`).
- `lint-filter-patterns.json` (this directory): `drop_if_any`, `keep_if_any`,
  `min_confidence`, `max_inline`. Every pattern compiles with Python `re`, uses no
  lookbehind, and is applied with `re.search` and `re.IGNORECASE` to `title + "\n" + body`.
- Optionally a repository setting for the inline cap; `max_inline` is the default and the
  upper bound.

## Outputs

Three buckets that together contain every input finding exactly once:

- `inline` — findings published as inline review comments, at most `max_inline`, ordered.
- `body_only` — findings rendered into the review body instead of inline comments.
- `dropped` — findings not published, each with a `drop_reason`: `lint_pattern`,
  `duplicate`, `unknown_path`.

The filter never edits `title`, `severity`, `category`, `confidence` or `suggestion`, and
edits `body` in one case only: step 6 strips a prefix that names no rule. Otherwise it may
only move a finding between buckets, set `drop_reason`, clear `rule_name` (step 6) and
clear `start_line` (step 4).

## Pipeline

Run the steps in this order; each step sees the findings the previous step left in play. In
play means not dropped: a `body_only` finding stays in play and still passes steps 4 and 6.

1. **Lint drop.** Let `text = title + "\n" + body`. Drop the finding with reason
   `lint_pattern` when any `drop_if_any` pattern matches `text`, unless a `keep_if_any`
   pattern matches `text` or `severity` is `critical` or `high`. A finding with `rule_name`
   set is not exempt: a custom rule that restates a linter check belongs in the linter.
2. **Dedup.** Key = `(path, line, normalized title)`, where the normalized title is
   lower-cased with every run of non-alphanumeric characters replaced by one space and
   trimmed. For each key keep the finding with the highest severity, then the highest
   `confidence`, then the earliest position in the model's answer; drop the others with
   reason `duplicate`.
3. **Confidence threshold.** `confidence < min_confidence` moves the finding to `body_only`.
4. **Hunk validation.** A `path` that is not among the run's changed files drops the finding
   with reason `unknown_path`. A `line` outside every hunk of that file (the extra context
   lines included) moves the finding to `body_only`. A `start_line` that is not inside a
   hunk, or not smaller than `line`, is cleared to `null`; the finding stays where it is. A
   finding in `body_only` is published without its `suggestion`.
5. **Cap.** Sort the findings still `inline` by severity (`critical`, `high`, `medium`, `low`,
   `info`), then by `confidence` descending, then by position in the model's answer. The
   first `N` stay `inline`, where `N` is the smaller of `max_inline` and the repository
   setting; the rest move to `body_only` and keep that order. A finding already in
   `body_only` (steps 3 and 4) is never promoted back into the cap.
6. **Attribution consistency.** `rule_name` stays set only when all three hold: `body`
   starts with `According to custom instructions in '`, the name quoted between the first
   pair of single quotes equals `rule_name`, and `rule_name` is one of the run's rule names.
   Otherwise `rule_name` is cleared to `null`. A `body` that carries the prefix while
   `rule_name` is `null` (from the model, or cleared here) has the prefix stripped — up to
   and including the first `): ` after the quoted name — and moves to `body_only`: the
   author never sees an attribution that names no rule.

## Pseudocode (illustrative)

```text
for f in findings:
    text = f.title + "\n" + f.body
    if matches_any(drop_if_any, text) and not matches_any(keep_if_any, text)
       and f.severity not in {critical, high}:
        drop(f, "lint_pattern")
dedup by (f.path, f.line, normalize(f.title)); keep best(severity, confidence, position)
for f in not_dropped:
    if f.confidence < min_confidence: to_body(f)
    if f.path not in changed_files: drop(f, "unknown_path")
    elif f.line not in hunk_lines[f.path]: to_body(f)
    if f.start_line is not None and (f.start_line not in hunk_lines[f.path]
                                     or f.start_line >= f.line): f.start_line = None
sort inline by (severity_rank, -confidence, position)
inline, overflow = inline[:N], inline[N:]; to_body(each of overflow)
for f in inline + body_only:
    if not (f.body starts with PREFIX and quoted_name(f.body) == f.rule_name
            and f.rule_name in rule_names): f.rule_name = None
    if f.rule_name is None and f.body starts with PREFIX:
        f.body = strip_prefix(f.body); to_body(f)
to_body(f): move f to body_only; f.suggestion is not published
```

## Hand-off to publishing

The backend renders `body_only` findings and the model's `summary` into the single review
body, in the order the cap step produced, and truncates the body to 4000 characters. Inline
findings become one review comment each, anchored at `line` (and `start_line` when set) on
the new version of the file. A finding in `dropped` is stored with its `drop_reason` and
never published.

## Expected behaviour (cases for the backend's unit tests)

- `title: "Unused import of os"`, severity `low` — dropped, `lint_pattern`.
- `title: "Unused variable holds a hardcoded password"`, severity `low` — kept
  (`keep_if_any` matches `password`).
- `title: "Line too long hides an SQL injection"`, severity `critical` — kept (severity).
- Two findings with the same `path`, `line` and titles `"Missing null check"` and
  `"missing null-check"` — one kept, one dropped as `duplicate`.
- `confidence: 0.4` — `body_only`, even when the line is inside a hunk.
- `line` not inside any hunk of its file — `body_only`, `suggestion` not published.
- 14 findings still `inline` after steps 1–4 with `max_inline: 10` — 10 `inline`, 4 `body_only`.
- `rule_name: "Naming Consistency"` with a `body` that does not start with the prefix —
  published with `rule_name: null`.
- `rule_name: null` with a `body` that starts with the prefix — prefix stripped, `body_only`.
