<!-- SYNC: mirrored in dmc-268-{ui,api}-t6/.agents/skills/issue-graph/tests/golden/epic-pages.md -->

### Граф задач: эпик [api#60](https://github.com/larchanka-training/dmc-268-api-t6/issues/60) [notify] Уведомления о завершении прогона

Задач: 5 (открыто 4, закрыто 1). Связей блокировки: 1. Вне выборки: 0.

```mermaid
flowchart LR
  api_61["api#35;61 Модель подписки на уведомления"]
  api_62["api#35;62 Очередь исходящих уведомлений"]
  api_63["api#35;63 Шаблоны писем"]
  api_64["api#35;64 Отправка в Telegram"]
  api_65["api#35;65 Настройки уведомлений в профиле"]
  api_62 --> api_64
  classDef done fill:#f1f3f5,stroke:#adb5bd,color:#868e96
  classDef progress fill:#e7f5ff,stroke:#1c7ed6,color:#212529
  classDef review fill:#fff9db,stroke:#f08c00,color:#212529
  classDef alarm fill:#fff5f5,stroke:#e03131,stroke-width:3px,color:#212529
  classDef external fill:#ffffff,stroke:#868e96,stroke-dasharray:5 5,color:#212529
  class api_61 done
```

#### Циклы

Нет.

#### In Progress с открытым блокером

Нет.

#### Слои: что можно брать параллельно

1. [api#62](https://github.com/larchanka-training/dmc-268-api-t6/issues/62), [api#63](https://github.com/larchanka-training/dmc-268-api-t6/issues/63), [api#65](https://github.com/larchanka-training/dmc-268-api-t6/issues/65) — можно брать сейчас
2. [api#64](https://github.com/larchanka-training/dmc-268-api-t6/issues/64)

#### Критический путь

[api#62](https://github.com/larchanka-training/dmc-268-api-t6/issues/62) → [api#64](https://github.com/larchanka-training/dmc-268-api-t6/issues/64) (задач: 2)

#### Без связей

- [api#63](https://github.com/larchanka-training/dmc-268-api-t6/issues/63) [notify] Шаблоны писем
- [api#65](https://github.com/larchanka-training/dmc-268-api-t6/issues/65) [notify] Настройки уведомлений в профиле

#### Вне выборки

Нет.
