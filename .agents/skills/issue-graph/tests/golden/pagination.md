<!-- SYNC: mirrored in dmc-268-{ui,api}-t6/.agents/skills/issue-graph/tests/golden/pagination.md -->

### Граф задач: майлстоун «Спринт 7»

Задач: 3 (открыто 3, закрыто 0). Связей блокировки: 4. Вне выборки: 3.

```mermaid
flowchart LR
  api_40["api#35;40 Фильтр прогонов по статусу"]
  api_41["api#35;41 Экспорт прогонов в CSV"]
  api_42["api#35;42 Сводка прогонов на дашборде"]
  api_43["api#35;43 Реплика БД для отчётов"]
  api_44["api#35;44 Квота на хранилище отчётов"]
  api_45["api#35;45 Архив старых прогонов"]
  api_40 --> api_42
  api_40 --> api_44
  api_40 --> api_45
  api_43 --> api_42
  classDef done fill:#f1f3f5,stroke:#adb5bd,color:#868e96
  classDef progress fill:#e7f5ff,stroke:#1c7ed6
  classDef review fill:#fff9db,stroke:#f08c00
  classDef alarm fill:#fff5f5,stroke:#e03131,stroke-width:3px
  classDef external fill:#ffffff,stroke:#868e96,stroke-dasharray:5 5
  class api_45 done
  class api_42 alarm
  class api_43,api_44 external
```

#### Циклы

Нет.

#### In Progress с открытым блокером

- [api#42](https://github.com/larchanka-training/dmc-268-api-t6/issues/42) ждёт [api#40](https://github.com/larchanka-training/dmc-268-api-t6/issues/40), [api#43](https://github.com/larchanka-training/dmc-268-api-t6/issues/43) (вне выборки)

#### Слои: что можно брать параллельно

1. [api#40](https://github.com/larchanka-training/dmc-268-api-t6/issues/40), [api#41](https://github.com/larchanka-training/dmc-268-api-t6/issues/41), [api#43](https://github.com/larchanka-training/dmc-268-api-t6/issues/43) (вне выборки) — можно брать сейчас
2. [api#42](https://github.com/larchanka-training/dmc-268-api-t6/issues/42), [api#44](https://github.com/larchanka-training/dmc-268-api-t6/issues/44) (вне выборки)

#### Критический путь

[api#40](https://github.com/larchanka-training/dmc-268-api-t6/issues/40) → [api#42](https://github.com/larchanka-training/dmc-268-api-t6/issues/42) (задач: 2)

#### Без связей

- [api#41](https://github.com/larchanka-training/dmc-268-api-t6/issues/41) [api] Экспорт прогонов в CSV

#### Вне выборки

- [api#43](https://github.com/larchanka-training/dmc-268-api-t6/issues/43) [infra] Реплика БД для отчётов — блокирует [api#42](https://github.com/larchanka-training/dmc-268-api-t6/issues/42)
- [api#44](https://github.com/larchanka-training/dmc-268-api-t6/issues/44) [infra] Квота на хранилище отчётов — ждёт [api#40](https://github.com/larchanka-training/dmc-268-api-t6/issues/40)
- [api#45](https://github.com/larchanka-training/dmc-268-api-t6/issues/45) [infra] Архив старых прогонов (закрыта) — ждёт [api#40](https://github.com/larchanka-training/dmc-268-api-t6/issues/40)
