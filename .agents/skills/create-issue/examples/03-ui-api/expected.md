<!-- SYNC: mirrored in dmc-268-{ui,api}-t6/.agents/skills/create-issue/examples/03-ui-api/expected.md -->

Разный код в двух репозиториях и разные исполнители — это родительский issue и
sub-issue, а не части одного issue. Порядок задаёт связь «blocked by».

- Issues в пакете: 2, создаются в таком порядке:
  1. `api-pr-filter` — Task в `larchanka-training/dmc-268-api-t6`, родитель; частей нет.
  2. `ui-pr-filter` — Story в `larchanka-training/dmc-268-ui-t6`, sub-issue
     (`parent: "@api-pr-filter"`) и `blocked_by: ["@api-pr-filter"]`; частей нет.
- В обоих kickoff есть `read_first` и `mines`.
- Тело не называет номер другого issue (его ещё нет): зависимость описана словами,
  номер показывает нативная связь.
