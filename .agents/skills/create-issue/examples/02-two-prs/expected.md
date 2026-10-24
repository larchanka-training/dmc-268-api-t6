<!-- SYNC: mirrored in dmc-268-{ui,api}-t6/.agents/skills/create-issue/examples/02-two-prs/expected.md -->

Два PR в одном репозитории, один исполнитель, по очереди — это части одного issue, не
sub-issues.

- Issues в пакете: 1 — `sarif-export`, Task в `larchanka-training/dmc-268-api-t6`.
- Частей (`parts`): 2, каждая — отдельный PR в этом же issue:
  - Ч.1 — эндпоинт выгрузки SARIF; 2 AC.
  - Ч.2 — отправка SARIF в GitHub code scanning по завершении прогона; 2 AC.
- У каждой части есть граница PR и `done_when` (команда и ожидаемый результат); в
  kickoff есть `read_first` и `mines`.
- Sub-issues и `blocked_by`: нет — порядок частей задаёт их нумерация.
