# Field templates

<!-- SYNC: mirrored in dmc-268-{ui,api}-t6/.agents/skills/create-issue/references/templates.md -->

What to put in each spec field. The emitter fixes the layout; these are the
_contents_, and they are what makes an issue useful to whoever picks it up.

Spec keys at the top level: `repo`, `type`, `lang`, `summary`, `as_role`, `i_want`,
`so_that`, `background`, `scope`, `acceptance_criteria`, `dependencies`, `dod`,
`references`, `environment`, `steps`, `expected`, `actual`, `severity`, `labels`,
`assignees`, `milestone`, `kickoff`, `parts`, `parent`, `blocked_by`, `status`. Any other
key is a hard error, as is a value of the wrong type (every list field is a list of strings) and an
unclosed code fence in a prose field. `milestone` must be open in the target repo and
`status` must be an option of the board's Status field (default `Todo`); both are checked
before the create.

## Type and label

The type is not a field on GitHub: it picks the layout and one repo label. The repo has
no `type:` labels and none should be created.

| `type`  | Layout | Label           |
| ------- | ------ | --------------- |
| `Story` | Story  | `enhancement`   |
| `Task`  | Task   | `enhancement`   |
| `Bug`   | Bug    | `bug`           |
| `Docs`  | Task   | `documentation` |

`Story` is the default. Extra `labels` in the spec are appended after the type label,
without duplicates; each must already exist in the repo.

## Story

```
summary              [component] Short imperative description
as_role              who benefits — a role, not a person
i_want               the capability, stated as behaviour
so_that              the value; if this is hard to write, the issue may not be worth filing
background           why this exists now: the incident, the conversation, the constraint
scope                what will be done — and, as its own bullet, what will not
acceptance_criteria  observable statements. "Endpoint returns per-node rows",
                     not "endpoint works"
dependencies         "None" is a real answer. More than one kind of dependency →
                     an object of labelled lists, blocking group first
references           links to designs, related issues, the discussion
kickoff              the briefing — see § Kickoff below
parts                more than one PR — one entry per PR, see § Parts below
```

## Task and Docs

```
summary              [component] Short imperative description
background           why this technical work is needed
scope                exactly what to do — and what is out of scope
acceptance_criteria  how anyone can confirm it is finished
dependencies, references, kickoff, parts   as for a Story
```

## Bug

```
summary              [component] Short description of the defect
environment          where it happens: prod/staging/dev, browser, version
steps                numbered, minimal, reproducible by someone else
expected             what should happen
actual               what does happen
severity             Critical / High / Medium / Low
acceptance_criteria  at minimum: no longer reproducible + a regression test
kickoff              the briefing — see § Kickoff below
```

The bug layout replaces the user-story triplet, background and scope. If you find
yourself wanting Background on a bug, the issue is probably a Story about fixing a
class of problem, not a Bug about one defect.

## Dependencies

Three shapes, and the emitter renders whichever is passed: a **string** (one
paragraph), a **list** (bullets), or an **object** `{"<label>": [items]}` (a labelled
list per group, in the order written). Use the object as soon as there is more than one
kind of dependency — a reader who has to work out from prose which of five numbers
actually blocks them will get it wrong:

```json
"dependencies": {
  "Blocked by": ["acme/sandbox#11 — the decision the work cannot start without, and why"],
  "Related, not blocking": ["acme/sandbox#12 — shares the tooling, gates nothing here"]
}
```

Labels are free text: say what the group means to the reader.

Native links come from two spec keys, not from the body: `parent` (one ref — the issue
becomes its sub-issue; same owner only) and `blocked_by` (a list of refs, read as "this
issue is blocked by X"; the parent may be one of them). A ref is `#N` (the spec's repo) or
`OWNER/REPO#N`, so a blocker may live in the other team repo. The script sets them after
the create and reads each back (`hosts.md`). An issue that _this_ one blocks has no key:
after the create, set that edge from the other side with
`issue-link.sh OWNER/REPO#<other> --blocked-by <this>`, `<this>` as its full `OWNER/REPO#N`
(a short `#N` resolves against `<other>`'s repo). The body still says _why_ each one
matters, under a label naming the same direction as the edge ("Blocked by" / «Блокирующие»
for `blocked_by`, "Blocks" / «Блокирует» for the reverse) — the link is what makes it
visible on the other issue. A blocked-by group ("Blocked by", «Блокирующие», «Блокируется»,
"Blockiert durch") with no `blocked_by` gets a warning: that link would be missing. A group
that says only «нет» / "none" (also `keine`, `n/a`, a lone `—`) names no blocker and does not
count. "Related, not blocking" has no native link on GitHub and stays text only:

```json
"parent": "#11",
"blocked_by": ["acme/shared#4"],
"dependencies": {
  "Blocked by": ["acme/shared#4 — the client this work calls, not released yet"],
  "Related, not blocking": ["acme/sandbox#12 — shares the tooling, gates nothing here"]
}
```

## Parts

A task bigger than one PR is sliced (`slicing.md`). Another PR by the same person, in
sequence, is a part of this issue — «Ч.N», a separate PR with `Refs`; different code in two
repos, another assignee or parallel work is a sub-issue instead (`batch.md`). Each entry of
`parts` has four required fields, one line each:

```json
"parts": [
  {
    "scope": "Endpoint `GET /api/runs/{id}/findings.sarif` returns SARIF 2.1.0",
    "boundary": "read only: serializer, endpoint, tests, OpenAPI; nothing is sent to GitHub",
    "done_when": "`uv run pytest tests -k sarif` — green",
    "acceptance_criteria": ["One `result` per finding (file, line, severity, rule)"]
  },
  {
    "scope": "Upload the SARIF to GitHub code scanning when a run finishes",
    "boundary": "the upload after the commit and its error handling; the endpoint is unchanged",
    "done_when": "`uv run pytest tests -k sarif_upload` — green, one POST `code-scanning/sarifs`",
    "acceptance_criteria": ["A failed upload leaves the run status unchanged"]
  }
]
```

The emitter numbers them from 1 and renders each after the plain `scope` bullets —
`Part 1 (separate PR): <scope>` with `PR boundary` and `Done when` nested under it — and
its criteria after the plain ones as `Part 1: <criterion>`; the labels follow `lang`
(`Ч.1 (отдельный PR)`, `Граница PR`, `Готово, когда`, `Ч.1:`). `done_when` is the stop
criterion: a check someone can run, with its expected result. With `parts` the plain
`acceptance_criteria` may be left out — every part's criteria count. One part is refused (it
is just the issue), as is a part with a missing, blank or multi-line field, an empty or
blank criterion, and `parts` on a Bug. The full example: `examples/02-two-prs/`.

## Definition of Done

Emitted on every issue, whether or not the spec supplies it. The default is the team's
merge gate (`.agents/rules/git-workflow.md`: one approving review, all review threads
resolved) plus the repo gates and the board:

```
Gates green (AGENTS.md → Commands)
Docs updated
PR approved, all review threads resolved
Issue status updated on board 12
```

Override with `dod` in the spec when an issue has gates of its own — a workflow run, a
coverage floor, a staging sign-off. The override replaces the whole list.

## Titles

`[component] short description`. The component prefix is what makes a list of thirty
issues scannable. The title is written in the body's language (`lang`).

For a Story, Task or Docs issue the description is **imperative**, because the title is
a request rather than a report: "Add per-node metrics endpoint", not "Per-node metrics
are missing". A Bug inverts it: the title states **what is wrong** — "Diff viewer drops
the last hunk", not "Fix the diff viewer". The fix is the issue's job; the title's job is
identification.

## Language

Title and prose in Russian (`"lang": "ru"`), even if the request came in English. Section
headings stay English (`Background`, `Scope`, `Acceptance Criteria`, `Dependencies`,
`DoD Checklist`, `References`) as in `larchanka-training/dmc-268-api-t6#33` and `…#46`;
Kickoff is rendered in Russian.

## Reference hygiene

Everything in the body must resolve for the reader. A path that only resolves on your
machine reads as authoritative and quietly wastes the reader's time — and on a public
repository it also publishes your directory layout and username.

| Instead of                       | Write                                                                            |
| -------------------------------- | -------------------------------------------------------------------------------- |
| `~/work/acme/frontend`           | `acme/frontend` (the repository)                                                 |
| `/home/<user>/…`, `$TMPDIR/…`    | a path relative to the repo root — or drop it                                    |
| `~/notes/<a private journal>.md` | the issue or PR holding the same facts — or inline the two sentences that matter |
| `../shared-libs/tools/lint.py`   | `tools/lint.py` in `acme/shared-libs` — **needs a checkout alongside this repo** |

No caveat is needed for what every contributor has by definition (`git`, the repo's own
`scripts/`, the documented toolchain). A sibling checkout, an internal-only host or credentials
are allowed with the precondition stated in the same line. Home paths, session temp
directories and private journals never. The test for a line: would it still be true and
followable on someone else's machine, and in CI?

## Ticking boxes later

When work lands, flip the boxes that are genuinely done — find each by its visible text,
change `- [ ]` to `- [x]`, and write the whole body back with `gh api -X PATCH
repos/OWNER/REPO/issues/N -F body=@file` (gh 2.46 caveats: `hosts.md`). Anything waiting
on a review, a merge or someone else stays unticked; a checklist ticked ahead of reality
is worse than none.

## Kickoff

The last section of the body, and the one that decides whether a stranger can start.
Whoever picks the issue up was not in the conversation and may not know the repository:
the body is the whole briefing.

```
repo                 org/repo — name any second repository and say what lives there
branch               the naming convention, and whether the default branch is protected
problem_shape        what is actually broken or missing, in mechanism terms
read_first           a repo-relative path per entry — and why THAT one first
before_writing_code  a command per entry — and what running it PROVES
mines                a trap per entry — and how it BITES, not merely that it exists
```

Every key is optional and the whole block is optional; absent renders nothing. Unknown
keys are a hard error — a misspelt `landmines` would otherwise lose the one hazard you
bothered to record.

The distinction people get wrong is `background` versus `problem_shape`. `background` is
why the issue exists — the reader is deciding whether to care. `problem_shape` is what
they will be looking at in the code — the reader has already decided and is opening the
file:

> **background** — "Coverage has been below the gate since the auth work landed."
>
> **problem_shape** — "All 76 files in the `security` module report 0% while the module
> has 40 test classes. It is a reporting gap, not missing tests: the sharded CI fan-out
> lists services only, so the module's tests never run in the lane that produces the
> report."

Do not write the implementation plan here. Whoever takes the issue will have current
`main` in front of them; the plan will be stale and they will follow it anyway. Record
instead what they cannot cheaply rediscover — which file mattered, and which command lied
to you. Every line obeys § Reference hygiene. Decomposition is allowed and is not a plan:
`parts` say what each PR delivers and where it stops, never how (`slicing.md`).

The headings follow `lang`: a Russian repository gets `Kickoff — с чего начать` and
Russian field names. The check before you move on: _could a competent contributor with
repository access and no memory of this conversation start from this issue alone?_
