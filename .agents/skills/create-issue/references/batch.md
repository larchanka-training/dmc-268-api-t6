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
key, an unreadable state file. Then
`--dry-run` runs `issue-create.sh <item> --dry-run` for each item with the local refs left
out, so a milestone, Status or external link target that is wrong fails the preview, not
the run. It prints one preview — a table in creation order (order, key, repo, type, title,
parent, blocked by, milestone, status; local refs as `@key`), then every body under a
heading with its key — and creates nothing, writes no state. Items an earlier run already
created are named below the table.

## 3. One confirmation for the batch

Every item clears the Definition of Ready (SKILL.md § 5) and the batch the gate before
creating (SKILL.md § Gate before creating), with the local-path sweep run over the batch
file: `python3 -m json.tool "$BATCH" | grep -nE '~/|/home/|/Users/|/tmp/|\$TMPDIR|\.\./'`.
Then print the whole preview as ordinary text, say how many issues it creates and ask once:

- **Claude Code** — `AskUserQuestion`: "Create these N issues?", options **Create** /
  **Edit** / **Cancel**;
- **OpenCode** — its `question` tool, the same question and options;
- **any harness without a structured-question tool** — "Reply `Create` to post these N
  issues, `Edit` to change something, or `Cancel`." Only the word `Create` posts.

The preview alone is never approval. The batch confirmation replaces the per-issue one of
SKILL.md § 7: one for the set. On **Edit** change the file, rerun `--dry-run`, show the full
preview, ask again; on **Cancel** or no answer create nothing.

## 4. Create

```bash
.agents/skills/create-issue/scripts/issue-batch.py "$BATCH"             # the flags of the preview
```

Parents go before their children and blockers before what they block; among the items free
to go, file order. Each item runs `issue-create.sh <spec>` as its own process, with every
`@key` replaced by the created `OWNER/REPO#N`, so the attribution guard, validation,
duplicate guard, read-back, board and links hold per item. Each result is written to the
state file `${XDG_CACHE_HOME:-$HOME/.cache}/create-issue/batches/<id>.json`
(`{key: {"url", "number", "repo"}}`, atomically) before the next item starts.

stdout: `{"id", "items": [{"key", "url", "number", "repo", "created", "adopted",
"verified", "board", "links"}], "state_file"}` — printed also when an item stops the batch,
listing what was done before it. Report each item: created, adopted or already there; its
board and links; any `verified: false`.

## 5. A rerun resumes

Run the same command again — that is the normal way to resume. The state file is the first
lookup:

- an item recorded there is not created again; its links are ensured with `issue-link.sh`,
  which leaves what is in place alone and never replaces a parent (`created: false`);
- an item not recorded whose create hits the exact-title duplicate guard (issue-create.sh
  exit 4, the duplicate's `url` / `number` on stdout) is **adopted**: recorded, used for the
  links that point at it, its own links ensured (`created: false, adopted: true`). Board and
  Status are not touched for it. The guard searches open and closed issues of every author,
  so look at each adopted issue in the report.

The state file is kept outside /tmp so that a reboot cannot erase the record of what was
created. Never delete or edit it to start over — that is how a batch gets created twice.

## 6. Failures

| Item result                                             | Batch                                                                                 |
| ------------------------------------------------------- | ------------------------------------------------------------------------------------- |
| 0                                                       | next item                                                                             |
| 6 (board) or 7 (links)                                  | the issue is created and recorded, the batch goes on; run the repair the stderr names |
| 4 naming the duplicate                                  | adopted (§ 5)                                                                         |
| 2, 3, 4 or 5                                            | the batch stops: the state keeps what was created; fix the cause and rerun            |
| `issue-link.sh` 2, 3 or 5 on a recorded or adopted item | the batch stops the same way; its 7 is a failed link and does not stop it             |

The driver exits 7 if any item had a failed link, else 6 if any had a failed board step,
else 0; a stop exits with the item's code. Never roll back: a created issue is real work —
report it with its URL.

## 7. Verify the batch

After the run compare what exists with the plan: the number of items, each url, `board`,
every link `ok`, `verified` true. A sub-issue whose parent is on board 12 lands on the board
through its auto-add even with `--no-board` (`hosts.md`).

## Comparing with a golden

`issue-batch.py --compare-shape GOLDEN CANDIDATE` compares two batch files by shape, not by
wording: the item count, then per item in creation order — repo, type, parent and
blocked_by (local refs as positions, so the keys may differ), the AC count of each part,
`done_when` of each part, kickoff `read_first` and `mines`. It prints
`{"same_shape", "differences"}` and exits 0 when equal, 1 when not, 2 when a file is not a
batch. Items are matched by creation order (ties by file order), so two unrelated items in
another order count as a difference. The goldens are `examples/<case>/batch.json`
(`slicing.md` § Examples).
