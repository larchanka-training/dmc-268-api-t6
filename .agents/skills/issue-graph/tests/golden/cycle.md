<!-- SYNC: mirrored in dmc-268-{ui,api}-t6/.agents/skills/issue-graph/tests/golden/cycle.md -->

### Граф задач: задачи [api#1](https://github.com/larchanka-training/dmc-268-api-t6/issues/1), [api#2](https://github.com/larchanka-training/dmc-268-api-t6/issues/2), [api#3](https://github.com/larchanka-training/dmc-268-api-t6/issues/3), [api#4](https://github.com/larchanka-training/dmc-268-api-t6/issues/4)

Задач: 4 (открыто 4, закрыто 0). Связей блокировки: 11. Вне выборки: 2.

```mermaid
flowchart LR
  api_1["api#35;1 Первый шаг цикла"]
  api_2["api#35;2 Второй шаг цикла"]
  api_3["api#35;3 Третий шаг цикла"]
  api_4["api#35;4 Свободная задача для @octocat по #35;5"]
  api_5["api#35;5 Общий раннер для шагов цикла"]
  api_6["api#35;6 Старый раннер"]
  api_1 --> api_2
  api_1 --> api_5
  api_2 --> api_3
  api_2 --> api_5
  api_2 --> api_6
  api_3 --> api_1
  api_3 --> api_5
  api_5 --> api_1
  api_5 --> api_2
  api_5 --> api_3
  api_6 -.-> api_3
  classDef done fill:#f1f3f5,stroke:#adb5bd,color:#868e96
  classDef progress fill:#e7f5ff,stroke:#1c7ed6,color:#212529
  classDef review fill:#fff9db,stroke:#f08c00,color:#212529
  classDef alarm fill:#fff5f5,stroke:#e03131,stroke-width:3px,color:#212529
  classDef external fill:#ffffff,stroke:#868e96,stroke-dasharray:5 5,color:#212529
  class api_1,api_2,api_3,api_5,api_6 alarm
```

#### Циклы

- [api#1](https://github.com/larchanka-training/dmc-268-api-t6/issues/1) → [api#2](https://github.com/larchanka-training/dmc-268-api-t6/issues/2) → [api#3](https://github.com/larchanka-training/dmc-268-api-t6/issues/3) → [api#1](https://github.com/larchanka-training/dmc-268-api-t6/issues/1)
  - в том же клубке связей ещё: [api#5](https://github.com/larchanka-training/dmc-268-api-t6/issues/5) (вне выборки), [api#6](https://github.com/larchanka-training/dmc-268-api-t6/issues/6) (вне выборки, закрыта)

#### In Progress с открытым блокером

- [api#1](https://github.com/larchanka-training/dmc-268-api-t6/issues/1) ждёт [api#3](https://github.com/larchanka-training/dmc-268-api-t6/issues/3), [api#5](https://github.com/larchanka-training/dmc-268-api-t6/issues/5) (вне выборки)

#### Слои: что можно брать параллельно

Не считаются: в графе цикл.

#### Критический путь

Не считается: в графе цикл.

#### Без связей

- [api#4](https://github.com/larchanka-training/dmc-268-api-t6/issues/4) [docs] Свободная задача для @&#8203;octocat по #&#8203;5

#### Вне выборки

- [api#5](https://github.com/larchanka-training/dmc-268-api-t6/issues/5) [infra] Общий раннер для шагов цикла — блокирует [api#1](https://github.com/larchanka-training/dmc-268-api-t6/issues/1), [api#2](https://github.com/larchanka-training/dmc-268-api-t6/issues/2), [api#3](https://github.com/larchanka-training/dmc-268-api-t6/issues/3); ждёт [api#1](https://github.com/larchanka-training/dmc-268-api-t6/issues/1), [api#2](https://github.com/larchanka-training/dmc-268-api-t6/issues/2), [api#3](https://github.com/larchanka-training/dmc-268-api-t6/issues/3)
- [api#6](https://github.com/larchanka-training/dmc-268-api-t6/issues/6) [infra] Старый раннер (закрыта) — блокирует [api#3](https://github.com/larchanka-training/dmc-268-api-t6/issues/3); ждёт [api#2](https://github.com/larchanka-training/dmc-268-api-t6/issues/2)
