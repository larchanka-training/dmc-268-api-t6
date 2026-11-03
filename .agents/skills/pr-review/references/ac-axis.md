# The AC axis

<!-- SYNC: mirrored in dmc-268-{ui,api}-t6/.agents/skills/pr-review/references/ac-axis.md -->

The third axis of `pr-review`. It answers one question per acceptance criterion of the brief: does
the pull request's head meet it, and where is the proof? It does not look for other defects —
Standards and Spec do that.

## Statuses

| Status        | When                                                                                                                                                                             |
| ------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `covered`     | The criterion holds at the head, and every claim in it has a link: the code or document that does it, plus the test that checks it when the claim is about behaviour.            |
| `not covered` | Nothing at the head does it; or the behaviour is there but no test checks it; or the only test would pass without the change (it asserts what the code computes, not the spec).  |
| `can't tell`  | The proof lives outside the repo at the head — a CI run, the board, an issue comment, a deploy, a manual run — or the criterion is too vague to check. Say what would settle it. |

- A link is `path:line` of the file at the head (`git show <head_sha>:<path>`), or a test as
  `path:line` plus its name. Mark each `(diff)` when the pull request changes that line, `(base)`
  when it was there already: a criterion an earlier pull request met is still `covered`.
- Read the test before the code: a test that names the criterion but asserts nothing it states does
  not count.
- Unsure is never `covered`. Between `covered` and anything else, take the other status and give the
  reason; a skipped check is not a pass.
- The pull request body counts as proof only for a criterion about the pull request itself (an
  attached run, a linked graph): link the pull request.
- A criterion about documentation needs the document, not a test.
- A criterion whose work lives in the other repo is `can't tell`, not `not covered`: this clone
  cannot see it. Say which repo holds its proof.

## Answer

The first line as every axis: `Reviewed: <n> files, <m> commits, head <sha>`. Then one row per
criterion of the brief, in its order, with its id — none added, merged or dropped — and a totals
line. No other text.

```markdown
| AC  | Status      | Proof                                                                         |
| --- | ----------- | ----------------------------------------------------------------------------- |
| AC1 | covered     | `src/a.ts:12` (diff); test `src/a.test.ts:40` «rejects an empty name» (diff)  |
| AC2 | not covered | `src/b.ts:30` (diff) sets the limit, but no test checks it                    |
| AC3 | can't tell  | «all gates green» is a CI run, not in the repo: see the pull request's checks |

covered 1 · not covered 1 · can't tell 1
```
