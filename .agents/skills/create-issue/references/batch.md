# Batch mode

<!-- SYNC: mirrored in dmc-268-{ui,api}-t6/.agents/skills/create-issue/references/batch.md -->

For several related issues filed together: a task sliced into sub-issues (`slicing.md`), an
epic and its children, a set of follow-ups. A batch can stop halfway; everything below makes
that resumable instead of duplicating. The mechanism is `scripts/issue-batch.py`.

## 1. Capture the batch as data

Write the whole batch into one file before anything is created — never create as you go
while the list is still being discussed. Under `$(mktemp -d)`, like a spec:

```json
{
  "id": "runs-pr-filter",
  "items": [
    { "key": "api-pr-filter", "repo": "larchanka-training/dmc-268-api-t6", "type": "Task" },
    {
      "key": "ui-pr-filter",
      "repo": "larchanka-training/dmc-268-ui-t6",
      "type": "Story",
      "parent": "@api-pr-filter",
      "blocked_by": ["@api-pr-filter"]
    }
  ]
}
```

(Shortened: each item is a full spec — `examples/03-ui-api/batch.json`.)

- `id` and every `key` match `[a-z0-9][a-z0-9-]*`; keys are unique. The `id` names the state
  file: a rerun of the same batch keeps its `id`, a new batch gets a new one.
- An item is a normal spec (`templates.md`) plus `key`. `parent` / `blocked_by` may name
  another item as `@key`; it becomes that item's `OWNER/REPO#N` once it exists. Refs to
  existing issues (`#N`, `OWNER/REPO#N`) work as in a single spec.
- A body cannot carry `@key`: there is no number yet, and on GitHub it renders as an
  @mention. Say in words what the dependency is; the native link carries the number. The
  driver refuses a body that mentions a key.

## 2. Validate and preview

```bash
.agents/skills/create-issue/scripts/issue-batch.py "$BATCH" --dry-run   # --no-board: skip the board
```

Before anything runs the driver refuses (exit 2): a malformed file, a duplicate key, an
unknown `@key` or one naming its own item, a dependency cycle over parent and blocked_by
edges, a local parent of another owner, `status` with `--no-board`, an item
`build_issue_body.py` rejects, two items with one title in one repo, a body mentioning a
key, an unreadable state file, a state entry recorded in another repo or with another
title than its item (§ 5). Then `--dry-run` runs `issue-create.sh <item> --dry-run` for
each item not yet created — the set the run validates (§ 4) — with the local refs left out,
so a milestone, Status or external link target that is wrong fails the preview, not the run.
It prints one preview — a table in creation order (order, key, repo, type, title, parent,
blocked by, milestone, status; local refs as `@key`), then every body still to create under
a heading with its key — and creates nothing, writes no state. Below the table it names the
items an earlier run already created (key, title, url; no dry-run, no body) and, for each
item whose exact title already exists in its repo, what the run will do: `would adopt #N`
(§ 5) or `would stop: duplicate #N (state, author)` — the run then stops before its first
create; rename the item or settle that issue first.

## 3. One confirmation for the batch

Every item clears the Definition of Ready (SKILL.md § 5) and the batch the gate before
creating (SKILL.md § Gate before creating), with the local-path sweep run over the batch
file: `python3 -m json.tool "$BATCH" | grep -nE '~/|/home/|/Users/|/tmp/|\$TMPDIR|\.\./'`.
Then print the whole preview as ordinary text, say how many issues it creates and ask once:

- **Claude Code** — `AskUserQuestion`: "Create these N issues?", options **Create** /
  **Edit** / **Cancel**;
- **OpenCode** — its `question` tool, the same question and options;
- **any harness without a structured-question tool** — "Reply `Create` / «Создать» to
  post these N issues, `Edit` / «Изменить» to change something, or `Cancel` / «Отмена»."
  Only `Create` or «Создать» posts; "ok", "да" or silence do not.

The preview alone is never approval. The batch confirmation replaces the per-issue one of
SKILL.md § 7: one for the set. On **Edit** change the file, rerun `--dry-run`, show the full
preview, ask again; on **Cancel** or no answer create nothing.

## 4. Create

```bash
.agents/skills/create-issue/scripts/issue-batch.py "$BATCH"             # the flags of the preview
```

One run of a batch at a time — nothing locks a batch against a second run started beside it.

The run first validates every item not yet created as the preview does
(`issue-create.sh --dry-run`, local refs left out) and creates nothing if one fails or has a
duplicate it may not adopt (§ 5). Then it writes `started_at` (UTC) to the state file
`${XDG_CACHE_HOME:-$HOME/.cache}/create-issue/batches/<id>.json` and creates in order:
parents before their children and blockers before what they block; among the items free to
go, file order. Each item runs `issue-create.sh <spec>` as its own process, with every
`@key` replaced by the created `OWNER/REPO#N`, so the attribution guard, validation,
duplicate guard, read-back, board and links hold per item. Each result is written to the
state file, atomically, before the next item starts:
`{"started_at", "items": {key: {"url", "number", "repo", "summary", "board", "links"}}}`.

stdout: `{"id", "items": [{"key", "url", "number", "repo", "created", "adopted",
"verified", "board", "links"}], "state_file"}` — printed also when an item stops the batch,
listing what was done before it. Report each item: created, adopted or already there; its
board and links; any `verified: false`.

## 5. A rerun resumes

Run the same command again — that is the normal way to resume. The state file is the first
lookup:

- an item recorded there is not created again; its links are ensured with `issue-link.sh`,
  which leaves what is in place alone and never replaces a parent (`created: false`), and
  the entry gets the new links result. A recorded `board: failed` is reported again and the
  run exits 6 with `gh project item-add 12 --owner larchanka-training --url URL` on stderr
  (then set the Status on the board by hand). The state cannot see that repair: once the
  issue is on the board, set that entry's `board` to `added` by hand.
- an item not recorded may still exist: an earlier run created it and stopped — or crashed —
  before recording it. A rerun passes `--adopt-since <started_at − 120 s>` to
  issue-create.sh, which looks for it in the newest issues of the repo (a plain listing: the
  search index lags) and then runs the exact-title duplicate guard. Either way the issue
  found (exit 4, its `url`, `number`, `state`, `author`, `createdAt` on stdout) is
  **adopted** only if all hold: this is a rerun (the state file already had `started_at`),
  the issue is open, you opened it, and it was created at or after `started_at` less 120 s
  (`started_at` is this machine's clock, `createdAt` GitHub's; the margin covers this clock
  running ahead). Adopted, it is recorded, used for the links that point at it and its own
  links are ensured (`created: false, adopted: true`); on stderr the adoption is one
  `{"note": "adopted <url> — …"}` line, not issue-create.sh's exit-4 duplicate error. Its
  `board` is `unchecked` — its board step may never have run: look at it, and add it with
  the command above if it is missing. Any other duplicate — on a first run, closed, a
  teammate's or older — stops the batch with exit 4 naming it; one you opened, on a rerun,
  may still be this batch's own (closed since, or a clock further ahead), so the message
  first says how to record it.

The state file is kept outside /tmp so that a reboot cannot erase the record of what was
created. Never delete it, and never start a new `id` for a batch that created anything: a new
`id` has no `started_at`, so it adopts nothing and stops at the first issue that exists. Edit
it by hand only for a repaired board (above), for an issue of this batch a rerun refused —
add `items.<key>` with its `url`, `number`, `repo` and `summary`, and rerun — and for a
recorded issue that is gone: one that was **deleted** — remove its entry from `items`, and
the rerun files it again; one that was **transferred** to another repo — remove its entry,
drop the item from the batch file and replace its `@key` in the other items by the issue's
new `OWNER/REPO#N`.

## 6. Failures

| Item result                                             | Batch                                                                                                                                              |
| ------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------- |
| 0                                                       | next item                                                                                                                                          |
| 6 (board) or 7 (links)                                  | the issue is created and recorded, the batch goes on; run the repair the stderr names                                                              |
| 4 naming a duplicate                                    | adopted, or the batch stops (§ 5)                                                                                                                  |
| 2, 3 or 4                                               | the batch stops: the state keeps what was created; fix the cause and rerun                                                                         |
| 5, or a result that cannot be read or recorded          | the batch stops the same way, but the issue may exist: check the newest issues of the repo first — a rerun adopts it (§ 5); likewise after a crash |
| `issue-link.sh` 2, 3 or 5 on a recorded or adopted item | the batch stops the same way; its 7 is a failed link and does not stop it                                                                          |

The driver exits 7 if any item had a failed link, else 6 if any had a failed board step —
also one recorded by an earlier run — else 0; a stop exits with the item's code. Never roll
back: a created issue is real work — report it with its URL.

## 7. Verify the batch

After the run compare what exists with the plan: the number of items, each url, `board`
(an `unchecked` one by looking at the board), every link `ok`, `verified` true. A sub-issue
whose parent is on board 12 lands on the board through its auto-add even with `--no-board`
(`hosts.md`).

## Comparing with a golden

`issue-batch.py --compare-shape GOLDEN CANDIDATE` compares two batch files by shape, not by
wording: the item count, then per item in creation order — repo, parent and blocked_by
(local refs as positions, so the keys may differ), the number of parts and whether each part
has acceptance criteria and a `done_when`, kickoff `read_first` and `mines`. The type, the
titles, the wording and the number of criteria per part are not compared: the expected
result is the number of parts, PR vs sub-issue and the repo. It prints
`{"same_shape", "differences"}` and exits 0 when equal, 1 when not, 2 when a file is not a
batch. Items are matched by creation order (ties by file order), so two unrelated items in
another order count as a difference. The goldens are `examples/<case>/batch.json`
(`slicing.md` § Examples).
