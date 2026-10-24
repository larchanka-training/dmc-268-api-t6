<!-- SYNC: mirrored in dmc-268-{ui,api}-t6/.agents/skills/issue-graph/tests/golden/issues.md -->

### Граф задач: задачи [ui#9](https://github.com/larchanka-training/dmc-268-ui-t6/issues/9), [api#31](https://github.com/larchanka-training/dmc-268-api-t6/issues/31), [api#32](https://github.com/larchanka-training/dmc-268-api-t6/issues/32), [api#30](https://github.com/larchanka-training/dmc-268-api-t6/issues/30)

Задач: 4 (открыто 4, закрыто 0). Связей блокировки: 2. Вне выборки: 1.

```mermaid
flowchart LR
  subgraph api_30["api#35;30 Импорт задач из трекера"]
    subgraph api_31["api#35;31 Парсер ссылок на задачи"]
      api_32["api#35;32 Тесты парсера ссылок"]
    end
  end
  ui_9["ui#35;9 Экран импорта задач"]
  octo_tools_5["octo/tools#35;5 Экспорт задач в JSON"]
  api_31 --> ui_9
  octo_tools_5 --> ui_9
  classDef done fill:#f1f3f5,stroke:#adb5bd,color:#868e96
  classDef progress fill:#e7f5ff,stroke:#1c7ed6
  classDef review fill:#fff9db,stroke:#f08c00
  classDef alarm fill:#fff5f5,stroke:#e03131,stroke-width:3px
  classDef external fill:#ffffff,stroke:#868e96,stroke-dasharray:5 5
  class ui_9 review
  class octo_tools_5 external
```

#### Циклы

Нет.

#### In Progress с открытым блокером

Нет.

#### Слои: что можно брать параллельно

1. [api#31](https://github.com/larchanka-training/dmc-268-api-t6/issues/31), [api#32](https://github.com/larchanka-training/dmc-268-api-t6/issues/32), [octo/tools#5](https://github.com/octo/tools/issues/5) (вне выборки) — можно брать сейчас
2. [ui#9](https://github.com/larchanka-training/dmc-268-ui-t6/issues/9)

#### Критический путь

[api#31](https://github.com/larchanka-training/dmc-268-api-t6/issues/31) → [ui#9](https://github.com/larchanka-training/dmc-268-ui-t6/issues/9) (задач: 2)

#### Без связей

- [api#32](https://github.com/larchanka-training/dmc-268-api-t6/issues/32) [import] Тесты парсера ссылок

#### Вне выборки

- [octo/tools#5](https://github.com/octo/tools/issues/5) [cli] Экспорт задач в JSON — блокирует [ui#9](https://github.com/larchanka-training/dmc-268-ui-t6/issues/9)
