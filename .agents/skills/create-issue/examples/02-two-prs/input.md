<!-- SYNC: mirrored in dmc-268-{ui,api}-t6/.agents/skills/create-issue/examples/02-two-prs/input.md -->

Хочу выгрузку находок прогона в SARIF, всё в api. Сначала просто эндпоинт, который
отдаёт SARIF по id прогона, — это отдельный PR. Потом вторым PR — чтобы по завершении
прогона SARIF сам уходил в GitHub code scanning репозитория. Делаю сам, по очереди:
второй PR опирается на сериализацию из первого.
