# Publishing

<!-- SYNC: mirrored in dmc-268-{ui,api}-t6/.agents/skills/issue-graph/references/publishing.md -->

Read this when you need how `--publish-to` finds and updates its comment, why a run refused to
publish or left a second comment, or how publishing is exercised offline. Without `--publish-to`
the script makes no REST call at all.

## Order of operations

1. Arguments are parsed; a bad `--publish-to` exits 2 before any request.
2. The report is fetched, built and written to stdout — always, and first.
3. A cycle → stderr `{"error": "not published: the graph has a cycle"}`, exit 1, no REST call.
4. The comment body is built; over 65536 characters (GitHub's comment limit) → exit 2
   `report is N characters, over GitHub's 65536-character comment limit; narrow the scope`, no REST
   call.
5. The target's comments are listed oldest first, page by page, up to the first marked one; the
   search stops after 100 pages (10000 comments) with exit 3.
6. Found → `PATCH repos/<owner>/<name>/issues/comments/<id>`; none →
   `POST repos/<owner>/<name>/issues/<n>/comments`.
7. stderr `{"published": "created" | "updated", "url": "<html_url>"}`, exit 0.

## Target

`--publish-to N` is `api#N` (the sprint issues live in api); `ui#N` or `OWNER/NAME#N` otherwise.
Anything else (`0`, `-3`, `261x`) exits 2 with a message naming both forms. The target is only ever
the one the user named.

## Marker and scope key

The comment's first line is the marker `<!-- issue-graph: <key> -->`, e.g.
`<!-- issue-graph: milestone=Спринт 4 -->`:

| Mode            | Key                                                                    |
| --------------- | ---------------------------------------------------------------------- |
| `--milestone T` | `milestone=T`                                                          |
| `--epic REF`    | `epic=<owner/name>#<n>`                                                |
| `--issues …`    | `issues=<owner/name>#<n>,…` — deduplicated, sorted by `(repo, number)` |

- The repo set is not part of a milestone key: a re-run with another `--repo` set updates the same
  comment (and replaces a two-repo graph with the narrower one).
- The order of `--issues` arguments does not matter; another set of issues is another key, so
  another comment.
- In `epic=` and `issues=` keys `owner/name` is lower-cased (GitHub repo names are
  case-insensitive, so `Owner/Name#5` and `owner/name#5` give one key); the milestone title is kept
  as it is.
- In the key `%` becomes `%25`, then CR `%0D` and LF `%0A`, then `>` `%3E`: the marker stays one
  line (a multi-line title still matches on a re-run) and a title can never close the HTML comment.

## Matching

- A comment matches when the first line of its body (split on `\n`, one trailing `\r` stripped)
  equals the marker exactly. A quoted copy (`> <!-- … -->`) or a marker on a later line does not.
- No author filter: anyone with write access may edit others' comments, and a teammate's re-run must
  update the same comment, not add a second one. GitHub keeps the original author and shows the
  comment as edited.
- With several matches the oldest (first seen) wins and paging stops there; the others are left as
  they are — a person may delete them, the agent never does.
- Paging: `GET repos/<owner>/<name>/issues/<n>/comments?per_page=100&page=<k>` for k = 1, 2, …,
  stopping at the first match or at a page with fewer than 100 items; after page 100 (10000
  comments) without either, exit 3. Not `gh api --paginate`: gh 2.46 has no `--slurp` and
  concatenates the arrays.

## Body

`<marker>\n\n<report>\n<footer>\n`, where `<report>` is the stdout text (it ends with a newline, so
one blank line precedes the footer) and the footer is exactly
`<sub>Сгенерировано скиллом issue-graph (.agents/skills/issue-graph); повторный запуск с тем же охватом обновит этот комментарий.</sub>`.
It goes as the JSON `{"body": …}` on stdin (`gh api -X POST|PATCH <path> --input -`), never as
`-f body=`.

## Failures

| Situation                                                                   | Exit | stderr `error`                                                     |
| --------------------------------------------------------------------------- | ---- | ------------------------------------------------------------------ |
| the graph has a cycle                                                       | 1    | `not published: the graph has a cycle`                             |
| the body is over 65536 characters                                           | 2    | `report is N characters, …`                                        |
| 404 on the listing or the POST                                              | 2    | `issue <ref> not found`                                            |
| 404 on the PATCH                                                            | 2    | `comment <id> not found`                                           |
| the marked comment is one this account cannot edit                          | 3    | `comment <id> carries the marker but this account cannot edit it`  |
| 403 (no write access), 422, timeout after 120 s, non-JSON, a non-array page | 3    | gh's error                                                         |
| the marker search ran past 100 pages (10000 comments)                       | 3    | `issue <ref>: more than 10000 comments, the marker search stopped` |

The report is on stdout in every row. After an exit 3 a retry is safe — the marker turns it into an
update of anything the failed run may have created — but whether to retry is the user's call. A
write that timed out may still have landed: re-running is safe, it updates.

A marked comment the account cannot edit gets no second comment: the script stops with exit 3.
Whoever can edit it (its author or a maintainer) re-runs, or a person removes the comment. The agent
neither retries nor works around it.

## Fixture seam

With `ISSUE_GRAPH_FIXTURE_DIR=<dir>` set, `gh` is never called and every request reads
`<dir>/<name>.json`. Set but empty (`ISSUE_GRAPH_FIXTURE_DIR=`) is not "unset": the script exits 3
and never goes live.

- GraphQL, the full `{"data": …}` response: `milestones-<alias>`, `milestone-<alias>-<m>-p<k>`,
  `epic-<alias>-<n>-p<k>`, `issues-<alias>`, `node-<id>-<field>-p<k>` (the first follow-up page is
  `p2`).
- REST, the response body itself: `comments-<alias>-<n>-p<k>` (a JSON array, or the 404 object
  `{"status": "404"}` → the same exit 2), `comment-created-<alias>-<n>`, `comment-updated-<id>`.
- `<alias>` is `api`, `ui`, or `OWNER/NAME` with every non-alphanumeric as `_`.

A write first appends one JSON line `{"method", "path", "body"}` to the file `ISSUE_GRAPH_WRITE_LOG`
names and then reads its response fixture; without that variable a write exits 3
`fixture mode cannot publish: set ISSUE_GRAPH_WRITE_LOG`. Fixtures never reach GitHub, so fixtures
plus `--publish-to` can never post a fake graph. `ISSUE_GRAPH_WRITE_LOG` exists for the tests; an
agent never sets it. The committed fixture sets in `tests/fixtures/` hold no comment pages — the
publish tests generate theirs in `tmp_path`.
