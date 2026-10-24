<!-- SYNC: mirrored in dmc-268-{ui,api}-t6/.agents/skills/issue-graph/tests/golden/milestone.md -->

### Граф задач: майлстоун «Спринт 5»

Задач: 10 (открыто 9, закрыто 1). Связей блокировки: 9. Вне выборки: 2.

```mermaid
flowchart LR
  subgraph api_10["api#35;10 Ревью по графу символов"]
    api_11["api#35;11 Индекс символов репозитория"]
    api_12["api#35;12 Провайдер контекста для ревью"]
    ui_3["ui#35;3 Тумблер графа в настройках"]
  end
  api_13["api#35;13 Миграция таблицы индекса"]
  api_14["api#35;14 Очистка устаревших индексов"]
  api_15["api#35;15 Очередь пересборки индекса"]
  api_16["api#35;16 Описание формата отчёта"]
  api_17["api#35;17 Кнопка #quot;Повторить#quot; для #35;42 и #lt;Retry#gt; в …"]
  api_80["api#35;80 Схема хранения графа"]
  api_90["api#35;90 Стенд для нагрузочных прогонов"]
  ui_4["ui#35;4 Бейдж контекста в ревью"]
  api_11 --> api_15
  api_12 --> api_17
  api_12 --> ui_4
  api_13 -.-> api_14
  api_13 -.-> api_15
  api_13 -.-> api_80
  api_80 -.-> api_14
  api_90 --> api_15
  ui_3 --> api_12
  classDef done fill:#f1f3f5,stroke:#adb5bd,color:#868e96
  classDef progress fill:#e7f5ff,stroke:#1c7ed6,color:#212529
  classDef review fill:#fff9db,stroke:#f08c00,color:#212529
  classDef alarm fill:#fff5f5,stroke:#e03131,stroke-width:3px,color:#212529
  classDef external fill:#ffffff,stroke:#868e96,stroke-dasharray:5 5,color:#212529
  class api_13,api_80 done
  class api_14,ui_3 progress
  class api_11 review
  class api_15 alarm
  class api_90 external
```

#### Циклы

Нет.

#### In Progress с открытым блокером

- [api#15](https://github.com/larchanka-training/dmc-268-api-t6/issues/15) ждёт [api#11](https://github.com/larchanka-training/dmc-268-api-t6/issues/11), [api#90](https://github.com/larchanka-training/dmc-268-api-t6/issues/90) (вне выборки)

#### Слои: что можно брать параллельно

1. [api#11](https://github.com/larchanka-training/dmc-268-api-t6/issues/11), [api#14](https://github.com/larchanka-training/dmc-268-api-t6/issues/14), [api#16](https://github.com/larchanka-training/dmc-268-api-t6/issues/16), [api#90](https://github.com/larchanka-training/dmc-268-api-t6/issues/90) (вне выборки), [ui#3](https://github.com/larchanka-training/dmc-268-ui-t6/issues/3) — можно брать сейчас
2. [api#12](https://github.com/larchanka-training/dmc-268-api-t6/issues/12), [api#15](https://github.com/larchanka-training/dmc-268-api-t6/issues/15)
3. [api#17](https://github.com/larchanka-training/dmc-268-api-t6/issues/17), [ui#4](https://github.com/larchanka-training/dmc-268-ui-t6/issues/4)

#### Критический путь

[ui#3](https://github.com/larchanka-training/dmc-268-ui-t6/issues/3) → [api#12](https://github.com/larchanka-training/dmc-268-api-t6/issues/12) → [api#17](https://github.com/larchanka-training/dmc-268-api-t6/issues/17) (задач: 3)

#### Без связей

- [api#16](https://github.com/larchanka-training/dmc-268-api-t6/issues/16) [docs] Описание формата отчёта

#### Вне выборки

- [api#80](https://github.com/larchanka-training/dmc-268-api-t6/issues/80) [db] Схема хранения графа (закрыта) — блокирует [api#14](https://github.com/larchanka-training/dmc-268-api-t6/issues/14); ждёт [api#13](https://github.com/larchanka-training/dmc-268-api-t6/issues/13)
- [api#90](https://github.com/larchanka-training/dmc-268-api-t6/issues/90) [infra] Стенд для нагрузочных прогонов — блокирует [api#15](https://github.com/larchanka-training/dmc-268-api-t6/issues/15)
