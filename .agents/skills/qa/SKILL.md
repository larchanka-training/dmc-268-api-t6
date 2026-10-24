---
name: qa
description: This skill should be used when running an interactive QA session where the user reports bugs or issues conversationally and the agent files GitHub issues, exploring the codebase in the background for context and domain language.
metadata:
  version: 1.0.0
  source: instructor-pack
  adapted-for: any
---

# QA Session

<!-- SYNC: mirrored in dmc-268-{ui,api}-t6/.agents/skills/qa/SKILL.md -->

Run an interactive QA session. The user describes problems they're encountering. You clarify, explore the codebase for context, and file GitHub issues that are durable, user-focused, and use the project's domain language.

Issues are filed only through create-issue (`.agents/skills/create-issue/`) in its batch mode, never by calling `gh` yourself. This skill decides what to file; create-issue owns the body, its language, the Definition of Ready, the preview and the create.

## For each issue the user raises

### 1. Listen and lightly clarify

Let the user describe the problem in their own words. Ask **at most 2-3 short clarifying questions** focused on:

- What they expected vs what actually happened
- Steps to reproduce (if not obvious)
- Whether it's consistent or intermittent

Do NOT over-interview. If the description is clear enough to file, move on.

### 2. Explore the codebase in the background

While talking to the user, kick off an Agent (subagent_type=Explore) in the background to understand the relevant area (other harnesses: see `.agents/README.md` § Sub-agents across harnesses). A harness without a sub-agent tool explores inline, in this same context: exploring only gathers context for the issue, so the fresh-session rule there for independent checks does not apply. The goal is NOT to find a fix — it's to:

- Learn the domain language used in that area (check UBIQUITOUS_LANGUAGE.md if it exists)
- Understand what the feature is supposed to do
- Identify the user-facing behavior boundary

This context helps you write a better issue. The user-facing parts (title, steps, expected, actual) stay in behavior and domain terms; what the exploration found goes into the `kickoff` block — `repo`, repo-relative `read_first` paths, `problem_shape` — which create-issue requires for a technical issue such as a Bug (create-issue `SKILL.md` § 5, `references/templates.md` § Kickoff).

### 3. Assess scope: single issue or breakdown?

Before filing, decide whether this is a **single issue** or needs to be **broken down** into multiple issues.

Break down when:

- The fix spans multiple independent areas (e.g. "the form validation is wrong AND the success message is missing AND the redirect is broken")
- There are clearly separable concerns that different people could work on in parallel
- The user describes something that has multiple distinct failure modes or symptoms

Keep as a single issue when:

- It's one behavior that's wrong in one place
- The symptoms are all caused by the same root behavior

Whether a slice is a separate issue or a part (Ч.N) of one issue: create-issue `references/slicing.md` § A part or a sub-issue. A Bug cannot carry `parts`, so a Bug breakdown is always separate Bug items.

### 4. File the issue(s) through create-issue

Issues must be **durable** — they should still make sense after major refactors. Write from the user's perspective.

Capture the report as one batch file (create-issue `references/batch.md` § 1), defined before anything uses it:

```bash
BATCH="$(mktemp -d)/batch.json"   # every step below reads "$BATCH"
```

Its `id` is `qa-YYYYMMDD-HHMM` (UTC: `date -u +qa-%Y%m%d-%H%M`, taken once when the file is written; a second report in the same minute adds `-2`) — a fresh id per report, matching `[a-z0-9][a-z0-9-]*`. Never reuse an id for another report: it inherits the old state file and its `started_at`. A rerun that resumes this batch keeps its file and `id` (create-issue `references/batch.md` § 5). One item per issue, each a full create-issue spec plus a `key`. Fill each item by create-issue `SKILL.md` § Workflow steps 1–6:

- `repo` from `issue-preflight.sh` (§ 1);
- `type: "Bug"` with `environment` / `steps` / `expected` / `actual` for something broken, otherwise the fitting type (§ 2, `references/templates.md`);
- `lang`: the language create-issue § 4 Language picks (in this team: `ru`), set explicitly, and the body shape from create-issue (`references/templates.md`), never a hand-written body;
- the Definition of Ready (§ 5): a `[component]` title, at least one acceptance criterion, the kickoff for a technical issue;
- `blocked_by`: `@key` of another item when it genuinely blocks, or the ref of an existing issue — with the matching "Blocked by" group in `dependencies` (§ A breakdown below);
- `parent`: only when a tracking issue exists (its ref) or is an item of the same batch (`@key`), otherwise left out.

#### A breakdown

- **Prefer many thin issues over few thick ones** — each item independently fixable and verifiable
- **Mark blocking relationships honestly** — if issue B genuinely can't be tested until issue A is fixed, B carries `"blocked_by": ["@a"]` and a `dependencies` object whose "Blocked by" group («Блокирующие» in a Russian body) names A in words: what it is and why B waits for it (create-issue `references/templates.md` § Dependencies). A body cannot carry an `@key` (the driver refuses it); the native link carries the number. Independent items set neither and leave Dependencies to the default ("None")
- **Maximize parallelism** — the goal is that multiple people (or agents) can grab different issues simultaneously

The driver creates parents before children and blockers before what they block, so the file order does not have to follow dependencies.

#### Rules for all issue bodies

- **Use the project's domain language** (check UBIQUITOUS_LANGUAGE.md if it exists)
- **Describe behaviors, not code** — "the sync service fails to apply the patch" not "applyPatch() throws on line 42"; paths belong in the kickoff and follow create-issue reference hygiene (`references/templates.md` § Reference hygiene)
- **Reproduction steps are mandatory** — if you can't determine them, ask the user
- **Keep it concise** — the title and the acceptance criteria read in 30 seconds; the kickoff, DoD and Dependencies that create-issue requires stay, short

#### Preview, one confirmation, create

1. Run the gate before creating (create-issue `SKILL.md` § Gate before creating), with the local-path sweep over the batch file (`references/batch.md` § 3).
2. Preview: `.agents/skills/create-issue/scripts/issue-batch.py "$BATCH" --dry-run`. Print the whole preview as ordinary text and say how many issues it creates.
3. Ask ONCE for the whole batch (`references/batch.md` § 3): the structured question tool where the harness has one (Claude Code `AskUserQuestion`, OpenCode `question`; options Create / Edit / Cancel), otherwise the explicit reply `Create` / «Создать». The preview alone is never approval. On Edit change the file, preview again, ask again; on Cancel or no answer create nothing.
4. Create: `.agents/skills/create-issue/scripts/issue-batch.py "$BATCH"` with the flags of the preview.

Exit codes, a batch that stopped halfway and the rerun that resumes it: create-issue `references/batch.md` § 4–6. Never file the missing issues by hand.

After the run, share each issue's URL from the driver's output — created, adopted or already there, its links, any `verified: false` — summarize the blocking relationships and ask: "Next issue, or are we done?"

### 5. Continue the session

Keep going until the user says they're done. Each user report is filed as its own batch — one item or a breakdown — with one confirmation.
