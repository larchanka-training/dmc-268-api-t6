<!-- SYNC: mirrored in dmc-268-{ui,api}-t6/.agents/skills/issue-graph/tests/golden/no-links.md -->

### Граф задач: майлстоун «Спринт 9»

> Майлстоун «Спринт 9» не найден в ui.

Задач: 3 (открыто 3, закрыто 0). Связей блокировки: 0. Вне выборки: 0.

```mermaid
flowchart LR
  api_21["api#35;21 Глоссарий терминов ревью"]
  api_22["api#35;22 Кэш зависимостей в пайплайне"]
  api_23["api#35;23 Пагинация списка прогонов"]
  classDef done fill:#f1f3f5,stroke:#adb5bd,color:#868e96
  classDef progress fill:#e7f5ff,stroke:#1c7ed6,color:#212529
  classDef review fill:#fff9db,stroke:#f08c00,color:#212529
  classDef alarm fill:#fff5f5,stroke:#e03131,stroke-width:3px,color:#212529
  classDef external fill:#ffffff,stroke:#868e96,stroke-dasharray:5 5,color:#212529
  class api_22 progress
  class api_23 review
```

#### Циклы

Нет.

#### In Progress с открытым блокером

Нет.

#### Слои: что можно брать параллельно

1. [api#21](https://github.com/larchanka-training/dmc-268-api-t6/issues/21), [api#22](https://github.com/larchanka-training/dmc-268-api-t6/issues/22), [api#23](https://github.com/larchanka-training/dmc-268-api-t6/issues/23) — можно брать сейчас

#### Критический путь

Нет: между открытыми задачами нет связей.

#### Без связей

- [api#21](https://github.com/larchanka-training/dmc-268-api-t6/issues/21) [docs] Глоссарий терминов ревью
- [api#22](https://github.com/larchanka-training/dmc-268-api-t6/issues/22) [ci] Кэш зависимостей в пайплайне
- [api#23](https://github.com/larchanka-training/dmc-268-api-t6/issues/23) [api] Пагинация списка прогонов

#### Вне выборки

Нет.
