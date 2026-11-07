# Профиль дашборда (#456): остальные планы

Дополнение к разделу «Планы» отчёта [db-profile-456.md](db-profile-456.md): тот же прогон 3, `EXPLAIN (ANALYZE, BUFFERS)` каждого запроса с его собственными параметрами, как их пишет `--explain-out`. Время запроса на клиенте и его место среди запросов сценария — в разделе «Куда уходит время» отчёта. Строки длиннее 500 символов обрезаны с указанием числа символов.

## `summary 30d`: `_days`, категории находок по дням

```text
HashAggregate  (cost=19886.05..19997.13 rows=5554 width=48) (actual time=68.315..68.435 rows=124 loops=1)
  Group Key: (timezone('UTC'::text, runs.created_at))::date, findings.category
  Batches: 1  Memory Usage: 217kB
  Buffers: shared hit=4475 read=10200
  ->  Hash Join  (cost=6849.46..19844.40 rows=5554 width=8) (actual time=60.288..66.982 rows=8253 loops=1)
        Hash Cond: (findings.run_id = runs.id)
        Buffers: shared hit=4467 read=10200
        ->  Seq Scan on findings  (cost=0.00..12574.99 rows=89769 width=20) (actual time=0.019..40.218 rows=127375 loops=1)
              Filter: ((published IS TRUE) AND (drop_reason IS NULL))
              Rows Removed by Filter: 54924
              Buffers: shared hit=552 read=10200
        ->  Hash  (cost=6807.12..6807.12 rows=3387 width=24) (actual time=13.871..13.881 rows=3614 loops=1)
              Buckets: 4096  Batches: 1  Memory Usage: 230kB
              Buffers: shared hit=3915
              ->  Hash Join  (cost=2873.37..6807.12 rows=3387 width=24) (actual time=11.253..13.143 rows=3614 loops=1)
                    Hash Cond: (code_changes.repository_id = repositories.id)
                    Buffers: shared hit=3915
                    ->  Hash Join  (cost=2732.14..6652.21 rows=4457 width=40) (actual time=11.109..12.376 rows=4517 loops=1)
                          Hash Cond: (code_changes.id = runs.code_change_id)
                          Buffers: shared hit=3844
                          ->  Seq Scan on code_changes  (cost=0.00..3693.00 rows=36500 width=32) (actual time=0.002..4.958 rows=36500 loops=1)
                                Buffers: shared hit=3328
                          ->  Hash  (cost=2676.42..2676.42 rows=4457 width=40) (actual time=3.377..3.378 rows=4517 loops=1)
                                Buckets: 8192  Batches: 1  Memory Usage: 382kB
                                Buffers: shared hit=516
                                ->  Index Only Scan using ix_runs_code_change_created_id on runs  (cost=0.41..2676.42 rows=4457 width=40) (actual time=0.024..2.402 rows=4517 loops=1)
                                      Index Cond: ((created_at >= '2026-10-08 01:45:09.161137+00'::timestamp with time zone) AND (created_at <= '2026-11-07 01:45:09.161137+00'::timestamp with time zone))
                                      Heap Fetches: 0
                                      Buffers: shared hit=516
                    ->  Hash  (cost=141.00..141.00 rows=19 width=16) (actual time=0.138..0.144 rows=20 loops=1)
                          Buckets: 1024  Batches: 1  Memory Usage: 9kB
                          Buffers: shared hit=71
                          ->  Seq Scan on repositories  (cost=0.00..141.00 rows=19 width=16) (actual time=0.017..0.126 rows=20 loops=1)
                                Filter: (EXISTS(SubPlan 1) OR EXISTS(SubPlan 2))
                                Rows Removed by Filter: 5
                                Buffers: shared hit=71
                                SubPlan 1
                                  ->  Nested Loop  (cost=0.00..3.46 rows=1 width=0) (actual time=0.004..0.004 rows=1 loops=25)
                                        Buffers: shared hit=65
                                        ->  Nested Loop  (cost=0.00..2.10 rows=1 width=16) (actual time=0.002..0.002 rows=1 loops=25)
                                              Join Filter: (provider_installations.workspace_id = github_user_workspace_access.workspace_id)
                                              Rows Removed by Join Filter: 0
                                              Buffers: shared hit=45
                                              ->  Seq Scan on provider_installations  (cost=0.00..1.05 rows=1 width=32) (actual time=0.001..0.001 rows=1 loops=25)
                                                    Filter: ((workspace_id = ANY ('{858fd0e5-ca21-40d3-a635-8257d2805b71,c1c34d37-8d54-41c5-8cc4-7f110348200e}'::uuid[])) AND (id = repositories.provider_installation_id) AND ((provider)::text = 'github'::text))
                                                    Rows Removed by Filter: 1
                                                    Buffers: shared hit=25
                                              ->  Seq Scan on github_user_workspace_access  (cost=0.00..1.02 rows=2 width=16) (actual time=0.000..0.001 rows=2 loops=20)
                                                    Filter: (github_user_id = '42'::bigint)
                                                    Buffers: shared hit=20
                                        ->  Seq Scan on github_user_repository_access  (cost=0.00..1.35 rows=1 width=16) (actual time=0.001..0.001 rows=1 loops=20)
                                              Filter: ((github_user_id = '42'::bigint) AND (provider_installation_id = repositories.provider_installation_id) AND (repository_external_id = repositories.external_id))
                                              Rows Removed by Filter: 10
                                              Buffers: shared hit=20
                                SubPlan 2
                                  ->  Result  (cost=0.00..2.13 rows=1 width=0) (actual time=0.003..0.003 rows=0 loops=5)
                                        One-Time Filter: (repositories.installation_available IS TRUE)
                                        Buffers: shared hit=5
                                        ->  Nested Loop  (cost=0.00..2.13 rows=1 width=0) (actual time=0.002..0.003 rows=0 loops=5)
                                              Join Filter: (github_user_workspace_access_1.workspace_id = bitbucket_repository_identities.workspace_id)
                                              Buffers: shared hit=5
                                              ->  Nested Loop  (cost=0.00..2.11 rows=1 width=64) (actual time=0.002..0.002 rows=0 loops=5)
                                                    Join Filter: (github_user_workspace_access_1.workspace_id = bitbucket_connections.workspace_id)
                                                    Buffers: shared hit=5
                                                    ->  Nested Loop  (cost=0.00..2.10 rows=1 width=48) (actual time=0.002..0.002 rows=0 loops=5)
                                                          Join Filter: (provider_installations_1.workspace_id = github_user_workspace_access_1.workspace_id)
                                                          Buffers: shared hit=5
                                                          ->  Seq Scan on provider_installations provider_installations_1  (cost=0.00..1.05 rows=1 width=32) (actual time=0.001..0.001 rows=0 loops=5)
                                                                Filter: ((deleted_at IS NULL) AND (workspace_id = ANY ('{858fd0e5-ca21-40d3-a635-8257d2805b71,c1c34d37-8d54-41c5-8cc4-7f110348200e}'::uuid[])) AND (id = repositories.provider_installation_id) AND ((provider)::text = 'bitbucket'::text))
                                                                Rows Removed by Filter: 3
                                                                Buffers: shared hit=5
                                                          ->  Seq Scan on github_user_workspace_access github_user_workspace_access_1  (cost=0.00..1.02 rows=2 width=16) (never executed)
                                                                Filter: (github_user_id = '42'::bigint)
                                                    ->  Seq Scan on bitbucket_connections  (cost=0.00..0.00 rows=1 width=32) (never executed)
                                                          Filter: ((revoked_at IS NULL) AND (provider_installation_id = repositories.provider_installation_id))
                                              ->  Seq Scan on bitbucket_repository_identities  (cost=0.00..0.00 rows=1 width=32) (never executed)
                                                    Filter: ((provider_installation_id = repositories.provider_installation_id) AND (repository_id = repositories.id))
Planning:
  Buffers: shared hit=82
Planning Time: 1.599 ms
Execution Time: 69.038 ms
```

## `summary 30d`: `_finding_counts`

```text
HashAggregate  (cost=19858.28..19858.68 rows=20 width=80) (actual time=68.373..68.390 rows=20 loops=1)
  Group Key: findings.severity, findings.category
  Batches: 1  Memory Usage: 24kB
  Buffers: shared hit=4499 read=10168
  ->  Hash Join  (cost=6849.46..19816.63 rows=5554 width=8) (actual time=61.173..67.068 rows=8253 loops=1)
        Hash Cond: (findings.run_id = runs.id)
        Buffers: shared hit=4499 read=10168
        ->  Seq Scan on findings  (cost=0.00..12574.99 rows=89769 width=24) (actual time=0.097..41.227 rows=127375 loops=1)
              Filter: ((published IS TRUE) AND (drop_reason IS NULL))
              Rows Removed by Filter: 54924
              Buffers: shared hit=584 read=10168
        ->  Hash  (cost=6807.12..6807.12 rows=3387 width=16) (actual time=13.679..13.687 rows=3614 loops=1)
              Buckets: 4096  Batches: 1  Memory Usage: 202kB
              Buffers: shared hit=3915
              ->  Hash Join  (cost=2873.37..6807.12 rows=3387 width=16) (actual time=11.078..13.007 rows=3614 loops=1)
                    Hash Cond: (code_changes.repository_id = repositories.id)
                    Buffers: shared hit=3915
                    ->  Hash Join  (cost=2732.14..6652.21 rows=4457 width=32) (actual time=10.929..12.213 rows=4517 loops=1)
                          Hash Cond: (code_changes.id = runs.code_change_id)
                          Buffers: shared hit=3844
                          ->  Seq Scan on code_changes  (cost=0.00..3693.00 rows=36500 width=32) (actual time=0.002..4.913 rows=36500 loops=1)
                                Buffers: shared hit=3328
                          ->  Hash  (cost=2676.42..2676.42 rows=4457 width=32) (actual time=3.286..3.287 rows=4517 loops=1)
                                Buckets: 8192  Batches: 1  Memory Usage: 347kB
                                Buffers: shared hit=516
                                ->  Index Only Scan using ix_runs_code_change_created_id on runs  (cost=0.41..2676.42 rows=4457 width=32) (actual time=0.029..2.374 rows=4517 loops=1)
                                      Index Cond: ((created_at >= '2026-10-08 01:45:09.161137+00'::timestamp with time zone) AND (created_at <= '2026-11-07 01:45:09.161137+00'::timestamp with time zone))
                                      Heap Fetches: 0
                                      Buffers: shared hit=516
                    ->  Hash  (cost=141.00..141.00 rows=19 width=16) (actual time=0.136..0.141 rows=20 loops=1)
                          Buckets: 1024  Batches: 1  Memory Usage: 9kB
                          Buffers: shared hit=71
                          ->  Seq Scan on repositories  (cost=0.00..141.00 rows=19 width=16) (actual time=0.020..0.128 rows=20 loops=1)
                                Filter: (EXISTS(SubPlan 1) OR EXISTS(SubPlan 2))
                                Rows Removed by Filter: 5
                                Buffers: shared hit=71
                                SubPlan 1
                                  ->  Nested Loop  (cost=0.00..3.46 rows=1 width=0) (actual time=0.004..0.004 rows=1 loops=25)
                                        Buffers: shared hit=65
                                        ->  Nested Loop  (cost=0.00..2.10 rows=1 width=16) (actual time=0.002..0.002 rows=1 loops=25)
                                              Join Filter: (provider_installations.workspace_id = github_user_workspace_access.workspace_id)
                                              Rows Removed by Join Filter: 0
                                              Buffers: shared hit=45
                                              ->  Seq Scan on provider_installations  (cost=0.00..1.05 rows=1 width=32) (actual time=0.001..0.001 rows=1 loops=25)
                                                    Filter: ((workspace_id = ANY ('{858fd0e5-ca21-40d3-a635-8257d2805b71,c1c34d37-8d54-41c5-8cc4-7f110348200e}'::uuid[])) AND (id = repositories.provider_installation_id) AND ((provider)::text = 'github'::text))
                                                    Rows Removed by Filter: 1
                                                    Buffers: shared hit=25
                                              ->  Seq Scan on github_user_workspace_access  (cost=0.00..1.02 rows=2 width=16) (actual time=0.000..0.000 rows=2 loops=20)
                                                    Filter: (github_user_id = '42'::bigint)
                                                    Buffers: shared hit=20
                                        ->  Seq Scan on github_user_repository_access  (cost=0.00..1.35 rows=1 width=16) (actual time=0.001..0.001 rows=1 loops=20)
                                              Filter: ((github_user_id = '42'::bigint) AND (provider_installation_id = repositories.provider_installation_id) AND (repository_external_id = repositories.external_id))
                                              Rows Removed by Filter: 10
                                              Buffers: shared hit=20
                                SubPlan 2
                                  ->  Result  (cost=0.00..2.13 rows=1 width=0) (actual time=0.002..0.002 rows=0 loops=5)
                                        One-Time Filter: (repositories.installation_available IS TRUE)
                                        Buffers: shared hit=5
                                        ->  Nested Loop  (cost=0.00..2.13 rows=1 width=0) (actual time=0.002..0.002 rows=0 loops=5)
                                              Join Filter: (github_user_workspace_access_1.workspace_id = bitbucket_repository_identities.workspace_id)
                                              Buffers: shared hit=5
                                              ->  Nested Loop  (cost=0.00..2.11 rows=1 width=64) (actual time=0.002..0.002 rows=0 loops=5)
                                                    Join Filter: (github_user_workspace_access_1.workspace_id = bitbucket_connections.workspace_id)
                                                    Buffers: shared hit=5
                                                    ->  Nested Loop  (cost=0.00..2.10 rows=1 width=48) (actual time=0.001..0.002 rows=0 loops=5)
                                                          Join Filter: (provider_installations_1.workspace_id = github_user_workspace_access_1.workspace_id)
                                                          Buffers: shared hit=5
                                                          ->  Seq Scan on provider_installations provider_installations_1  (cost=0.00..1.05 rows=1 width=32) (actual time=0.001..0.001 rows=0 loops=5)
                                                                Filter: ((deleted_at IS NULL) AND (workspace_id = ANY ('{858fd0e5-ca21-40d3-a635-8257d2805b71,c1c34d37-8d54-41c5-8cc4-7f110348200e}'::uuid[])) AND (id = repositories.provider_installation_id) AND ((provider)::text = 'bitbucket'::text))
                                                                Rows Removed by Filter: 3
                                                                Buffers: shared hit=5
                                                          ->  Seq Scan on github_user_workspace_access github_user_workspace_access_1  (cost=0.00..1.02 rows=2 width=16) (never executed)
                                                                Filter: (github_user_id = '42'::bigint)
                                                    ->  Seq Scan on bitbucket_connections  (cost=0.00..0.00 rows=1 width=32) (never executed)
                                                          Filter: ((revoked_at IS NULL) AND (provider_installation_id = repositories.provider_installation_id))
                                              ->  Seq Scan on bitbucket_repository_identities  (cost=0.00..0.00 rows=1 width=32) (never executed)
                                                    Filter: ((provider_installation_id = repositories.provider_installation_id) AND (repository_id = repositories.id))
Planning:
  Buffers: shared hit=68
Planning Time: 1.542 ms
Execution Time: 68.821 ms
```

## `summary 30d repo`: `_days`, категории находок по дням

```text
GroupAggregate  (cost=5502.07..5510.83 rows=292 width=48) (actual time=6.571..6.678 rows=113 loops=1)
  Group Key: ((timezone('UTC'::text, runs.created_at))::date), findings.category
  Buffers: shared hit=6585
  ->  Sort  (cost=5502.07..5502.80 rows=292 width=8) (actual time=6.559..6.585 rows=493 loops=1)
        Sort Key: ((timezone('UTC'::text, runs.created_at))::date), findings.category
        Sort Method: quicksort  Memory: 36kB
        Buffers: shared hit=6585
        ->  Nested Loop  (cost=64.57..5490.11 rows=292 width=8) (actual time=4.969..6.366 rows=493 loops=1)
              Buffers: shared hit=6577
              ->  Nested Loop  (cost=64.15..3610.82 rows=178 width=24) (actual time=4.920..5.385 rows=204 loops=1)
                    Buffers: shared hit=5779
                    ->  Nested Loop  (cost=63.73..2873.31 rows=1460 width=16) (actual time=0.498..1.982 rows=1514 loops=1)
                          Buffers: shared hit=1236
                          ->  Seq Scan on repositories  (cost=0.00..141.06 rows=1 width=16) (actual time=0.027..0.039 rows=1 loops=1)
                                Filter: (((full_name)::text = 'profile/repo-01'::text) AND (EXISTS(SubPlan 1) OR EXISTS(SubPlan 2)))
                                Rows Removed by Filter: 24
                                Buffers: shared hit=4
                                SubPlan 1
                                  ->  Nested Loop  (cost=0.00..3.46 rows=1 width=0) (actual time=0.015..0.017 rows=1 loops=1)
                                        Buffers: shared hit=3
                                        ->  Nested Loop  (cost=0.00..2.10 rows=1 width=16) (actual time=0.011..0.013 rows=1 loops=1)
                                              Join Filter: (provider_installations.workspace_id = github_user_workspace_access.workspace_id)
                                              Buffers: shared hit=2
                                              ->  Seq Scan on provider_installations  (cost=0.00..1.05 rows=1 width=32) (actual time=0.007..0.007 rows=1 loops=1)
                                                    Filter: ((workspace_id = ANY ('{858fd0e5-ca21-40d3-a635-8257d2805b71,c1c34d37-8d54-41c5-8cc4-7f110348200e}'::uuid[])) AND (id = repositories.provider_installation_id) AND ((provider)::text = 'github'::text))
                                                    Buffers: shared hit=1
                                              ->  Seq Scan on github_user_workspace_access  (cost=0.00..1.02 rows=2 width=16) (actual time=0.003..0.003 rows=1 loops=1)
                                                    Filter: (github_user_id = '42'::bigint)
                                                    Buffers: shared hit=1
                                        ->  Seq Scan on github_user_repository_access  (cost=0.00..1.35 rows=1 width=16) (actual time=0.003..0.003 rows=1 loops=1)
                                              Filter: ((github_user_id = '42'::bigint) AND (provider_installation_id = repositories.provider_installation_id) AND (repository_external_id = repositories.external_id))
                                              Rows Removed by Filter: 1
                                              Buffers: shared hit=1
                                SubPlan 2
                                  ->  Result  (cost=0.00..2.13 rows=1 width=0) (never executed)
                                        One-Time Filter: (repositories.installation_available IS TRUE)
                                        ->  Nested Loop  (cost=0.00..2.13 rows=1 width=0) (never executed)
                                              Join Filter: (github_user_workspace_access_1.workspace_id = bitbucket_repository_identities.workspace_id)
                                              ->  Nested Loop  (cost=0.00..2.11 rows=1 width=64) (never executed)
                                                    Join Filter: (github_user_workspace_access_1.workspace_id = bitbucket_connections.workspace_id)
                                                    ->  Nested Loop  (cost=0.00..2.10 rows=1 width=48) (never executed)
                                                          Join Filter: (provider_installations_1.workspace_id = github_user_workspace_access_1.workspace_id)
                                                          ->  Seq Scan on provider_installations provider_installations_1  (cost=0.00..1.05 rows=1 width=32) (never executed)
                                                                Filter: ((deleted_at IS NULL) AND (workspace_id = ANY ('{858fd0e5-ca21-40d3-a635-8257d2805b71,c1c34d37-8d54-41c5-8cc4-7f110348200e}'::uuid[])) AND (id = repositories.provider_installation_id) AND ((provider)::text = 'bitbucket'::text))
                                                          ->  Seq Scan on github_user_workspace_access github_user_workspace_access_1  (cost=0.00..1.02 rows=2 width=16) (never executed)
                                                                Filter: (github_user_id = '42'::bigint)
                                                    ->  Seq Scan on bitbucket_connections  (cost=0.00..0.00 rows=1 width=32) (never executed)
                                                          Filter: ((revoked_at IS NULL) AND (provider_installation_id = repositories.provider_installation_id))
                                              ->  Seq Scan on bitbucket_repository_identities  (cost=0.00..0.00 rows=1 width=32) (never executed)
                                                    Filter: ((provider_installation_id = repositories.provider_installation_id) AND (repository_id = repositories.id))
                          ->  Bitmap Heap Scan on code_changes  (cost=63.73..2717.65 rows=1460 width=32) (actual time=0.470..1.785 rows=1514 loops=1)
                                Recheck Cond: (repository_id = repositories.id)
                                Heap Blocks: exact=1216
                                Buffers: shared hit=1232
                                ->  Bitmap Index Scan on code_changes_repository_id_external_number_key  (cost=0.00..63.37 rows=1460 width=0) (actual time=0.313..0.313 rows=1514 loops=1)
                                      Index Cond: (repository_id = repositories.id)
                                      Buffers: shared hit=16
                    ->  Index Only Scan using ix_runs_code_change_created_id on runs  (cost=0.41..0.50 rows=1 width=40) (actual time=0.002..0.002 rows=0 loops=1514)
                          Index Cond: ((code_change_id = code_changes.id) AND (created_at >= '2026-10-08 01:45:10.00639+00'::timestamp with time zone) AND (created_at <= '2026-11-07 01:45:10.00639+00'::timestamp with time zone))
                          Heap Fetches: 0
                          Buffers: shared hit=4543
              ->  Index Scan using ix_findings_run_id on findings  (cost=0.42..10.52 rows=3 width=20) (actual time=0.003..0.004 rows=2 loops=204)
                    Index Cond: (run_id = runs.id)
                    Filter: ((published IS TRUE) AND (drop_reason IS NULL))
                    Rows Removed by Filter: 1
                    Buffers: shared hit=798
Planning:
  Buffers: shared hit=67 read=1
Planning Time: 1.922 ms
Execution Time: 6.930 ms
```

## `summary 30d repo`: `_status_counts`

```text
GroupAggregate  (cost=5162.44..5163.82 rows=5 width=12) (actual time=4.582..4.590 rows=4 loops=1)
  Group Key: runs.state
  Buffers: shared hit=5922
  ->  Sort  (cost=5162.44..5162.88 rows=178 width=4) (actual time=4.558..4.570 rows=204 loops=1)
        Sort Key: runs.state
        Sort Method: quicksort  Memory: 25kB
        Buffers: shared hit=5922
        ->  Nested Loop  (cost=64.15..5155.78 rows=178 width=4) (actual time=4.031..4.519 rows=204 loops=1)
              Buffers: shared hit=5922
              ->  Nested Loop  (cost=63.73..2873.31 rows=1460 width=16) (actual time=0.402..1.670 rows=1514 loops=1)
                    Buffers: shared hit=1236
                    ->  Seq Scan on repositories  (cost=0.00..141.06 rows=1 width=16) (actual time=0.019..0.030 rows=1 loops=1)
                          Filter: (((full_name)::text = 'profile/repo-01'::text) AND (EXISTS(SubPlan 1) OR EXISTS(SubPlan 2)))
                          Rows Removed by Filter: 24
                          Buffers: shared hit=4
                          SubPlan 1
                            ->  Nested Loop  (cost=0.00..3.46 rows=1 width=0) (actual time=0.011..0.012 rows=1 loops=1)
                                  Buffers: shared hit=3
                                  ->  Nested Loop  (cost=0.00..2.10 rows=1 width=16) (actual time=0.008..0.009 rows=1 loops=1)
                                        Join Filter: (provider_installations.workspace_id = github_user_workspace_access.workspace_id)
                                        Buffers: shared hit=2
                                        ->  Seq Scan on provider_installations  (cost=0.00..1.05 rows=1 width=32) (actual time=0.003..0.003 rows=1 loops=1)
                                              Filter: ((workspace_id = ANY ('{858fd0e5-ca21-40d3-a635-8257d2805b71,c1c34d37-8d54-41c5-8cc4-7f110348200e}'::uuid[])) AND (id = repositories.provider_installation_id) AND ((provider)::text = 'github'::text))
                                              Buffers: shared hit=1
                                        ->  Seq Scan on github_user_workspace_access  (cost=0.00..1.02 rows=2 width=16) (actual time=0.004..0.004 rows=1 loops=1)
                                              Filter: (github_user_id = '42'::bigint)
                                              Buffers: shared hit=1
                                  ->  Seq Scan on github_user_repository_access  (cost=0.00..1.35 rows=1 width=16) (actual time=0.002..0.002 rows=1 loops=1)
                                        Filter: ((github_user_id = '42'::bigint) AND (provider_installation_id = repositories.provider_installation_id) AND (repository_external_id = repositories.external_id))
                                        Rows Removed by Filter: 1
                                        Buffers: shared hit=1
                          SubPlan 2
                            ->  Result  (cost=0.00..2.13 rows=1 width=0) (never executed)
                                  One-Time Filter: (repositories.installation_available IS TRUE)
                                  ->  Nested Loop  (cost=0.00..2.13 rows=1 width=0) (never executed)
                                        Join Filter: (github_user_workspace_access_1.workspace_id = bitbucket_repository_identities.workspace_id)
                                        ->  Nested Loop  (cost=0.00..2.11 rows=1 width=64) (never executed)
                                              Join Filter: (github_user_workspace_access_1.workspace_id = bitbucket_connections.workspace_id)
                                              ->  Nested Loop  (cost=0.00..2.10 rows=1 width=48) (never executed)
                                                    Join Filter: (provider_installations_1.workspace_id = github_user_workspace_access_1.workspace_id)
                                                    ->  Seq Scan on provider_installations provider_installations_1  (cost=0.00..1.05 rows=1 width=32) (never executed)
                                                          Filter: ((deleted_at IS NULL) AND (workspace_id = ANY ('{858fd0e5-ca21-40d3-a635-8257d2805b71,c1c34d37-8d54-41c5-8cc4-7f110348200e}'::uuid[])) AND (id = repositories.provider_installation_id) AND ((provider)::text = 'bitbucket'::text))
                                                    ->  Seq Scan on github_user_workspace_access github_user_workspace_access_1  (cost=0.00..1.02 rows=2 width=16) (never executed)
                                                          Filter: (github_user_id = '42'::bigint)
                                              ->  Seq Scan on bitbucket_connections  (cost=0.00..0.00 rows=1 width=32) (never executed)
                                                    Filter: ((revoked_at IS NULL) AND (provider_installation_id = repositories.provider_installation_id))
                                        ->  Seq Scan on bitbucket_repository_identities  (cost=0.00..0.00 rows=1 width=32) (never executed)
                                              Filter: ((provider_installation_id = repositories.provider_installation_id) AND (repository_id = repositories.id))
                    ->  Bitmap Heap Scan on code_changes  (cost=63.73..2717.65 rows=1460 width=32) (actual time=0.382..1.529 rows=1514 loops=1)
                          Recheck Cond: (repository_id = repositories.id)
                          Heap Blocks: exact=1216
                          Buffers: shared hit=1232
                          ->  Bitmap Index Scan on code_changes_repository_id_external_number_key  (cost=0.00..63.37 rows=1460 width=0) (actual time=0.243..0.243 rows=1514 loops=1)
                                Index Cond: (repository_id = repositories.id)
                                Buffers: shared hit=16
              ->  Index Scan using ix_runs_code_change_created_id on runs  (cost=0.41..1.55 rows=1 width=20) (actual time=0.002..0.002 rows=0 loops=1514)
                    Index Cond: ((code_change_id = code_changes.id) AND (created_at >= '2026-10-08 01:45:10.00639+00'::timestamp with time zone) AND (created_at <= '2026-11-07 01:45:10.00639+00'::timestamp with time zone))
                    Buffers: shared hit=4686
Planning:
  Buffers: shared hit=51
Planning Time: 1.280 ms
Execution Time: 4.846 ms
```

## `summary 30d repo`: `_terminal_heads`

```text
Nested Loop Left Join  (cost=11016.95..15121.20 rows=467 width=94) (actual time=6.742..7.315 rows=355 loops=1)
  Buffers: shared hit=7744 read=103
  ->  Unique  (cost=11016.53..11017.42 rows=178 width=86) (actual time=6.710..6.753 rows=132 loops=1)
        Buffers: shared hit=7232 read=103
        ->  Sort  (cost=11016.53..11016.97 rows=178 width=86) (actual time=6.709..6.722 rows=204 loops=1)
              Sort Key: runs.code_change_id, runs.finished_at DESC NULLS LAST, runs.id DESC
              Sort Method: quicksort  Memory: 45kB
              Buffers: shared hit=7232 read=103
              ->  Nested Loop  (cost=64.15..11009.88 rows=178 width=86) (actual time=4.089..6.596 rows=204 loops=1)
                    Buffers: shared hit=7232 read=103
                    ->  Nested Loop  (cost=63.73..2873.31 rows=1460 width=16) (actual time=0.408..1.641 rows=1514 loops=1)
                          Buffers: shared hit=1236
                          ->  Seq Scan on repositories  (cost=0.00..141.06 rows=1 width=16) (actual time=0.030..0.039 rows=1 loops=1)
                                Filter: (((full_name)::text = 'profile/repo-01'::text) AND (EXISTS(SubPlan 4) OR EXISTS(SubPlan 5)))
                                Rows Removed by Filter: 24
                                Buffers: shared hit=4
                                SubPlan 4
                                  ->  Nested Loop  (cost=0.00..3.46 rows=1 width=0) (actual time=0.019..0.020 rows=1 loops=1)
                                        Buffers: shared hit=3
                                        ->  Nested Loop  (cost=0.00..2.10 rows=1 width=16) (actual time=0.015..0.016 rows=1 loops=1)
                                              Join Filter: (provider_installations.workspace_id = github_user_workspace_access.workspace_id)
                                              Buffers: shared hit=2
                                              ->  Seq Scan on provider_installations  (cost=0.00..1.05 rows=1 width=32) (actual time=0.003..0.003 rows=1 loops=1)
                                                    Filter: ((workspace_id = ANY ('{858fd0e5-ca21-40d3-a635-8257d2805b71,c1c34d37-8d54-41c5-8cc4-7f110348200e}'::uuid[])) AND (id = repositories.provider_installation_id) AND ((provider)::text = 'github'::text))
                                                    Buffers: shared hit=1
                                              ->  Seq Scan on github_user_workspace_access  (cost=0.00..1.02 rows=2 width=16) (actual time=0.011..0.012 rows=1 loops=1)
                                                    Filter: (github_user_id = '42'::bigint)
                                                    Buffers: shared hit=1
                                        ->  Seq Scan on github_user_repository_access  (cost=0.00..1.35 rows=1 width=16) (actual time=0.003..0.003 rows=1 loops=1)
                                              Filter: ((github_user_id = '42'::bigint) AND (provider_installation_id = repositories.provider_installation_id) AND (repository_external_id = repositories.external_id))
                                              Rows Removed by Filter: 1
                                              Buffers: shared hit=1
                                SubPlan 5
                                  ->  Result  (cost=0.00..2.13 rows=1 width=0) (never executed)
                                        One-Time Filter: (repositories.installation_available IS TRUE)
                                        ->  Nested Loop  (cost=0.00..2.13 rows=1 width=0) (never executed)
                                              Join Filter: (github_user_workspace_access_1.workspace_id = bitbucket_repository_identities.workspace_id)
                                              ->  Nested Loop  (cost=0.00..2.11 rows=1 width=64) (never executed)
                                                    Join Filter: (github_user_workspace_access_1.workspace_id = bitbucket_connections.workspace_id)
                                                    ->  Nested Loop  (cost=0.00..2.10 rows=1 width=48) (never executed)
                                                          Join Filter: (provider_installations_1.workspace_id = github_user_workspace_access_1.workspace_id)
                                                          ->  Seq Scan on provider_installations provider_installations_1  (cost=0.00..1.05 rows=1 width=32) (never executed)
                                                                Filter: ((deleted_at IS NULL) AND (workspace_id = ANY ('{858fd0e5-ca21-40d3-a635-8257d2805b71,c1c34d37-8d54-41c5-8cc4-7f110348200e}'::uuid[])) AND (id = repositories.provider_installation_id) AND ((provider)::text = 'bitbucket'::text))
                                                          ->  Seq Scan on github_user_workspace_access github_user_workspace_access_1  (cost=0.00..1.02 rows=2 width=16) (never executed)
                                                                Filter: (github_user_id = '42'::bigint)
                                                    ->  Seq Scan on bitbucket_connections  (cost=0.00..0.00 rows=1 width=32) (never executed)
                                                          Filter: ((revoked_at IS NULL) AND (provider_installation_id = repositories.provider_installation_id))
                                              ->  Seq Scan on bitbucket_repository_identities  (cost=0.00..0.00 rows=1 width=32) (never executed)
                                                    Filter: ((provider_installation_id = repositories.provider_installation_id) AND (repository_id = repositories.id))
                          ->  Bitmap Heap Scan on code_changes  (cost=63.73..2717.65 rows=1460 width=32) (actual time=0.377..1.497 rows=1514 loops=1)
                                Recheck Cond: (repository_id = repositories.id)
                                Heap Blocks: exact=1216
                                Buffers: shared hit=1232
                                ->  Bitmap Index Scan on code_changes_repository_id_external_number_key  (cost=0.00..63.37 rows=1460 width=0) (actual time=0.256..0.256 rows=1514 loops=1)
                                      Index Cond: (repository_id = repositories.id)
                                      Buffers: shared hit=16
                    ->  Index Scan using ix_runs_code_change_created_id on runs  (cost=0.41..1.56 rows=1 width=85) (actual time=0.002..0.002 rows=0 loops=1514)
                          Index Cond: ((code_change_id = code_changes.id) AND (created_at >= '2026-10-08 01:45:10.00639+00'::timestamp with time zone) AND (created_at <= '2026-11-07 01:45:10.00639+00'::timestamp with time zone))
                          Filter: (state = ANY ('{succeeded,failed,cancelled,skipped}'::run_state[]))
                          Buffers: shared hit=4686
                    SubPlan 1
                      ->  Aggregate  (cost=24.52..24.53 rows=1 width=1) (actual time=0.005..0.005 rows=1 loops=204)
                            Buffers: shared hit=813
                            ->  Index Scan using uq_code_change_diffs_run_filename on code_change_diffs  (cost=0.42..24.51 rows=5 width=1) (actual time=0.003..0.004 rows=5 loops=204)
                                  Index Cond: (run_id = runs.id)
                                  Buffers: shared hit=813
                    SubPlan 2
                      ->  Index Scan using context_payloads_run_id_key on context_payloads  (cost=0.29..8.31 rows=1 width=0) (actual time=0.004..0.004 rows=0 loops=201)
                            Index Cond: (run_id = runs.id)
                            Filter: ((NOT (summary ? 'reviewable_lines'::text)) AND ((summary ->> 'status'::text) = 'summary_only'::text))
                            Rows Removed by Filter: 1
                            Buffers: shared hit=497 read=103
  ->  Index Scan using ix_findings_run_id on findings  (cost=0.42..23.00 rows=3 width=20) (actual time=0.003..0.003 rows=2 loops=132)
        Index Cond: (run_id = runs.id)
        Filter: ((published IS TRUE) AND (drop_reason IS NULL))
        Rows Removed by Filter: 1
        Buffers: shared hit=512
Planning:
  Buffers: shared hit=51
Planning Time: 1.426 ms
Execution Time: 7.579 ms
```
