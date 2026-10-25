# The report

<!-- SYNC: mirrored in dmc-268-{ui,api}-t6/.agents/skills/issue-graph/references/report.md -->

Read this when you need what a section of the report means, why an issue sits where it does, or
what a node colour says. The report is Markdown in Russian and deterministic for a given GitHub
state: every list inside a section is sorted by `(repo, number)` (the api repo sorts before ui).
The goldens show each case byte for byte, after a SYNC line and a blank line:

| Golden                       | Shows                                                                          |
| ---------------------------- | ------------------------------------------------------------------------------ |
| `tests/golden/milestone.md`  | a full sprint across both repos: a subgraph, In Progress alarm, outside issues |
| `tests/golden/cycle.md`      | a cycle: no layers, no critical path; titles with `#5` and `@octocat`          |
| `tests/golden/no-links.md`   | a milestone found in api only; no links at all                                 |
| `tests/golden/epic.md`       | an epic's sub-issues, a critical path that starts outside the scope            |
| `tests/golden/pagination.md` | milestone issues and relations paged past their first page                     |
| `tests/golden/issues.md`     | an explicit list across repos and an outside owner                             |

## The graph

Nodes are the in-scope issues plus every issue an in-scope one is `blocked by` or `blocking`. The
relations are read one hop out: an outside issue shows only its links to in-scope ones. An edge
`A → B` means A blocks B. Closed issues stay in the graph.

## Title and counts

`### Граф задач: <scope>` — `майлстоун «T»`, `эпик <REF> <title>` or `задачи <REF>, …` (in argument
order). When the milestone exists in only some of the searched repos, a blockquote follows:
`> Майлстоун «Спринт 9» не найден в ui.` (`no-links.md`); in none, the script exits 2 with no
report. Then `Задач: N (открыто O, закрыто C). Связей блокировки: E. Вне выборки: X.` — N counts the
in-scope issues, closed included; E counts every edge in the graph.

## The Mermaid block

`flowchart LR`. A node id is `<alias>_<n>` (`api_139`; an outside owner `octo_tools_5`), its label
`<alias>#35;<n> <title>` with the leading `[tag]` groups dropped, cut to 40 characters with `…` and
escaped as Mermaid entities (`#35;`, `#quot;`, `#lt;`, `#gt;`, `#amp;`). An edge from an open
blocker is solid (`-->`), from a closed one dotted (`-.->`).

**Subgraphs.** An in-scope issue whose parent is also in scope is drawn inside
`subgraph <parent>`: the parent becomes a container (`milestone.md`: `api_10` holds `api#11`,
`api#12` and `ui#3`). A container gets no class. One without links of its own is a grouping, not
work: it is left out of the layers and of «Без связей».

**Node classes**, the first that applies:

| Class      | Node                                                                                      |
| ---------- | ----------------------------------------------------------------------------------------- |
| (none)     | a container                                                                               |
| `alarm`    | in a cyclic component (closed and outside nodes too), or In Progress with an open blocker |
| `done`     | closed                                                                                    |
| `external` | open and outside the scope (dashed border)                                                |
| `review`   | Status `On Review` or `AC Check`                                                          |
| `progress` | Status `In Progress`                                                                      |
| (none)     | any other open in-scope issue (`Todo`, no status): the theme's own colours                |

`progress`, `review`, `alarm` and `external` have light fills and each sets a dark text colour,
`color:#212529`: on GitHub's dark theme the label would otherwise keep the theme's light text and be
nearly invisible. `done` sets its own grey text (`#868e96`); unstyled nodes follow the theme.

## «Циклы»

The strongly connected components of the whole graph, closed and outside issues included; a
self-loop counts. One line per component: the cycle walked from its smallest key, successors in key
order, back to the start. The line can stand for a larger tangle: when the component has members
off that cycle, one nested line under it, `- в том же клубке связей ещё: …`, names them in `Key`
order, each tagged `(вне выборки)`, `(закрыта)` or `(вне выборки, закрыта)`; an in-scope open one
has no tag. A component that is just its cycle gets no nested line. In `cycle.md` that line names
`api#5 (вне выборки)` and `api#6 (вне выборки, закрыта)`: all five are one component, and removing
one link of the printed cycle leaves the rest of the tangle. Any cycle: no layers, no critical path,
no publish (exit 1). Empty: «Нет.».

## «In Progress с открытым блокером»

In-scope issues whose board Status is `In Progress` and that have at least one open blocker, in
scope or not; an outside one is tagged «(вне выборки)»: `- api#15 ждёт api#11, api#90 (вне выборки)`
(`milestone.md`). The Status is read from the board `--project-owner` / `--project` (default
`larchanka-training` / 12) — an issue not on that board is never listed. They are `alarm` on the
picture. Empty: «Нет.».

## «Слои: что можно брать параллельно»

Kahn's algorithm over every open node — in scope and open outside issues alike — minus the
containers without links. Only edges with both ends open count, so a closed blocker holds nothing
back. Layer 1, `— можно брать сейчас`, is the open issues with no open blocker; layer k+1 those whose
open blockers are all in layers 1…k. Outside issues are tagged «(вне выборки)»: `milestone.md` layer
1 holds `api#90 (вне выборки)`, `epic.md` starts with two outside issues. The board Status is not
considered: layer 1 can hold issues already In Progress or On Review (the classes show which). With
a cycle: «Не считаются: в графе цикл.»; with no open issue: «Нет открытых задач.».

## «Критический путь»

The longest chain over the same open graph as the layers, as `A → B → C (задач: 3)`; among chains of
equal length the one whose keys sort first by `(repo, number)`. It can start outside the scope
(`epic.md`: `api#130 → api#144 → api#146 → api#147 (задач: 4)`). A chain shorter than two issues:
«Нет: между открытыми задачами нет связей.» (`no-links.md`); with a cycle: «Не считается: в графе
цикл.».

## «Без связей»

Open in-scope issues, containers aside, with no blocking edge at all, each with its title. Empty:
«Нет.».

## «Вне выборки»

Every node outside the scope, with its title, `(закрыта)` when closed, and `блокирует …` /
`ждёт …` naming its in-scope neighbours — something the scope depends on, or that waits on it, but
that is not in it (`milestone.md`: open `api#90` blocks `api#15`; closed `api#80`). Empty: «Нет.».

## Titles outside the picture

Titles in the sections are raw Markdown with `#<digit>` and `@<login>` broken by a zero-width
entity (`#&#8203;5`, `@&#8203;octocat` in `cycle.md`), so a published comment neither autolinks an
issue nor mentions anyone. Code spans are kept as they are.
