<!-- SYNC: mirrored in dmc-268-{ui,api}-t6/.agents/skills/issue-graph/tests/golden/epic.md -->

### Граф задач: эпик [api#139](https://github.com/larchanka-training/dmc-268-api-t6/issues/139) [context][rag] Graph RAG: граф символов репозитория как контекст ревью

Задач: 9 (открыто 3, закрыто 6). Связей блокировки: 4. Вне выборки: 2.

```mermaid
flowchart LR
  api_130["api#35;130 Инженер 3 - [reviews] Добавьте чтение V…"]
  api_140["api#35;140 ADR: поправка Р-13 и SD/CVS под граф си…"]
  api_141["api#35;141 Модель данных индекса графа и изоляция …"]
  api_142["api#35;142 Индексатор: очередь index.repo и извлеч…"]
  api_143["api#35;143 Жизненный цикл индекса: триггеры обновл…"]
  api_144["api#35;144 RagContextProvider: контекст графа на р…"]
  api_145["api#35;145 Тумблер RAG на репозитории, статус инде…"]
  api_146["api#35;146 Замер вклада графа в точность ревью и г…"]
  api_147["api#35;147 v2: векторный поиск (pgvector + эмбедди…"]
  api_219["api#35;219 Остаток #35;120: guard, бюджет, summary и …"]
  ui_102["ui#35;102 Тумблер RAG и статус индекса в настройк…"]
  api_130 --> api_144
  api_144 --> api_146
  api_146 --> api_147
  api_219 --> api_144
  classDef done fill:#f1f3f5,stroke:#adb5bd,color:#868e96
  classDef progress fill:#e7f5ff,stroke:#1c7ed6,color:#212529
  classDef review fill:#fff9db,stroke:#f08c00,color:#212529
  classDef alarm fill:#fff5f5,stroke:#e03131,stroke-width:3px,color:#212529
  classDef external fill:#ffffff,stroke:#868e96,stroke-dasharray:5 5,color:#212529
  class api_140,api_141,api_142,api_143,api_145,ui_102 done
  class api_144 review
  class api_130,api_219 external
```

#### Циклы

Нет.

#### In Progress с открытым блокером

Нет.

#### Слои: что можно брать параллельно

1. [api#130](https://github.com/larchanka-training/dmc-268-api-t6/issues/130) (вне выборки), [api#219](https://github.com/larchanka-training/dmc-268-api-t6/issues/219) (вне выборки) — можно брать сейчас
2. [api#144](https://github.com/larchanka-training/dmc-268-api-t6/issues/144)
3. [api#146](https://github.com/larchanka-training/dmc-268-api-t6/issues/146)
4. [api#147](https://github.com/larchanka-training/dmc-268-api-t6/issues/147)

#### Критический путь

[api#130](https://github.com/larchanka-training/dmc-268-api-t6/issues/130) → [api#144](https://github.com/larchanka-training/dmc-268-api-t6/issues/144) → [api#146](https://github.com/larchanka-training/dmc-268-api-t6/issues/146) → [api#147](https://github.com/larchanka-training/dmc-268-api-t6/issues/147) (задач: 4)

#### Без связей

Нет.

#### Вне выборки

- [api#130](https://github.com/larchanka-training/dmc-268-api-t6/issues/130) Инженер 3 - [reviews] Добавьте чтение VCS, DiffMap, загрузчик политики и AST-контекст — блокирует [api#144](https://github.com/larchanka-training/dmc-268-api-t6/issues/144)
- [api#219](https://github.com/larchanka-training/dmc-268-api-t6/issues/219) [reviews][120] Остаток #&#8203;120: guard, бюджет, summary и отбрасывание кандидатов по CVS/RFS — блокирует [api#144](https://github.com/larchanka-training/dmc-268-api-t6/issues/144)
