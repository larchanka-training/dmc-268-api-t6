# Slicing a task

<!-- SYNC: mirrored in dmc-268-{ui,api}-t6/.agents/skills/create-issue/references/slicing.md -->

A request bigger than one pull request is sliced before it is filed. Whoever picks it up —
a teammate, or a weak model in Codex or OpenCode (GLM, Kimi, DeepSeek) — finishes a
PR-sized part with its own criteria far more reliably than an open-ended task; a strong
model loses nothing by it.

## When to slice

Slice when any of these holds:

- the work needs more than one PR;
- it changes code in more than one repository;
- parts can be done independently or in parallel;
- a part goes to another person.

Signs that a request is bigger than it looks: more than one working session, more than
three acceptance criteria, two or more subsystems, an "and" in the title. As a size hint a
part touches 1–5 files; one of 5–8 files is a candidate for slicing further. A task that
fits one PR is not sliced — one part is just the issue (`examples/01-single`).

## A part or a sub-issue

| The part …                                                                         | Becomes                                                                                               |
| ---------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------- |
| is one more PR by the same person, in sequence                                     | **Ч.N** — a `parts` entry of the same issue (the default)                                             |
| is the SYNC copy of a shared file in the other repo                                | the **same Ч.N** — a second PR in the other repo with the same `Refs`: ui first, then the copy in api |
| changes different code in two repos, goes to another assignee, or runs in parallel | a **sub-issue** — a batch item with `parent: "@key"` (`batch.md`)                                     |

The default follows «Приёмка задачи» in `docs/CONTRIBUTING.md` of
`larchanka-training/dmc-268-api-t6`: a task of several parts is accepted part by part, each
part a PR with `Refs`, and the issue is closed once every part is checked. The SYNC rule is
`.agents/README.md` § Sync map: shared files are edited in ui, then copied to api.

A blocker between issues is a `blocked_by` edge (`@key` inside a batch), never prose only.
The body still says why, in words: it cannot name the number of an issue that does not
exist yet.

## Each part

- its own acceptance criteria, rendered with the prefix «Ч.N:»;
- `boundary` — where the PR stops: what is in it and what is left to the next part;
- `done_when` — the stop criterion: a command someone can run and its expected result, on
  one line (`` `uv run pytest tests -k sarif` — зелёный ``);
- tests and docs land in the same part as the code they cover — no trailing "tests" part;
- a vertical slice: after each part `main` works and the part can be shown; a wide refactor
  goes expand → migrate → contract.

Decomposition is not an implementation plan. Parts say WHAT each PR delivers and WHERE it
stops, never HOW: the assignee writes the plan against a live checkout (`templates.md`
§ Kickoff). Resolve open questions before slicing — a part that hinges on an undecided
question is not ready.

## Ready for a weak model

Check every issue and every part before the preview:

- [ ] exact repo-relative paths to read and to change — kickoff `read_first`;
- [ ] the verification commands with their expected result — `done_when` per part,
      kickoff `before_writing_code`;
- [ ] the mines — kickoff `mines`: the trap and how it bites;
- [ ] one PR per part; nothing spans two PRs;
- [ ] every step works in one context: nothing relies on a sub-agent definition in
      `.agents/agents/*.md` (Codex does not load them); where a step needs isolation, use the
      harness mechanism from `.agents/README.md` § Sub-agents across harnesses.

## Examples

The three cases the rule is accepted by, each `input.md` (the request) → `batch.json`,
`preview.md` (what `issue-batch.py --dry-run` prints) and `expected.md` (the shape in plain
words):

- `examples/01-single/` — one PR in ui: not sliced, one issue without parts.
- `examples/02-two-prs/` — two PRs in api by one person, in sequence: one issue, two parts.
- `examples/03-ui-api/` — different code in api and ui, ui for another assignee: a parent
  issue in api and a sub-issue in ui, blocked by it.

A batch written for the same request is checked against the golden by shape —
`issue-batch.py --compare-shape examples/<case>/batch.json <candidate>` (`batch.md`).
