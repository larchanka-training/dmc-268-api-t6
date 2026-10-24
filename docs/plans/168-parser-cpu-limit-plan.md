# Implementation plan: OS CPU limit for the parser worker (#168, part 6)

Issue https://github.com/larchanka-training/dmc-268-api-t6/issues/168, part 6. Baseline `origin/main`
`1ae74b1a`, branch `fix/168-parser-cpu-limit`, test DB and vhost `imp168c`, task list
[168-parser-cpu-limit-todo.md](168-parser-cpu-limit-todo.md). The tech lead decided on 2026-10-24 (#168
comment 6387947446): add `RLIMIT_CPU` next to `RLIMIT_AS`; the network caveat is accepted in writing; the SD
and CVS wording is fixed later in docs PR G, so this PR changes no docs outside `docs/plans/`.

## Goal

The OS caps the CPU time of each request in the parser worker (`ast_worker.py`): a backstop for a worker whose
parent failed to stop it (parent hung or gone while a parse spins). It never changes a parent outcome of today.
`main()` first sets `RLIMIT_CORE` to (0, 0) (coordinator, 2026-10-24): SIGXCPU dumps core, a core holds sources.

## The design constraint

`RLIMIT_CPU` counts the CPU time of the whole process. One worker serves many requests, and the code_index
worker passes `attempt_cpu_seconds=None` (`code_index_worker.py:446`), so a fixed cap would kill a healthy
long-lived worker. The limit therefore follows each request: `main()` arms it after reading a request and before
`_handle`. Soft limit = `ceil(CPU used so far) + ceil(MAX_PARSE_CPU_SECONDS_PER_FILE) + slack`. The hard limit is
passed through unchanged (an unprivileged process can never raise a lowered hard limit); a finite inherited hard
limit (`ulimit -t`) clamps the soft one. Over the soft limit the kernel sends SIGXCPU, whose default action
terminates the worker; the parent already treats a dead worker as a failed parse and restarts it
(`subprocess_source_parser.py:96-99`). No-op where `resource` has no `RLIMIT_CPU`, like `_limit_memory`.

## The slack: 20 whole seconds

- The parent's longest wait for one request is 18 s of wall time from the write: `MAX_PARSE_CPU_SECONDS_PER_FILE
  + _WALL_SLACK_SECONDS + _STARTUP_SECONDS` (`subprocess_source_parser.py:83-88`). The first request after
  every start or restart gets the startup allowance, and restarts follow every kill.
- Once armed, a request may spend at least `1 + slack` CPU seconds (rounding up only adds time). The worker is
  single-threaded (CPU time never exceeds wall time) and arms the limit after the parent's timer started, so with
  slack 20 the OS fires no earlier than 21 s after it, 3 s after the parent's latest kill: every parent outcome
  stays the same, and the 3 s cover a late timer of a busy parent loop. A request spends under 22 CPU seconds.
- Slack 2 would tie the 3 s wait of a normal request; slack 3 would cut short the first request, so a runaway
  parse would report `AST_RESOURCE_LIMIT` instead of `AST_TIMEOUT`. A test pins `1 + slack > 18` against the
  parent's constants, so an edit to either side fails loudly.

## Tests (`tests/test_ast_worker.py`, new)

- The limit computation: literal soft limits for used CPU 0.0, 0.2, 7.0 and 7.01; the slack inequality.
- `resource` patched: `_limit_cpu` keeps an infinite hard limit, keeps a finite one and clamps the soft one to
  it; `_disable_core_dumps` sets (0, 0); neither calls `setrlimit` where its limit is missing.
- Fake worker `tests/ast_workers/cpu_bound.py`: arms the real helper with the slack lowered to 0 (each request
  may spend 1 to 2 CPU seconds) and sets `RLIMIT_CORE` to 0 (no core dump). Run directly, not through the
  parent, whose wall-clock kill would fire first. A request that burns 30 s dies with `-SIGXCPU`, without an
  answer, within a few CPU seconds. Six requests of 0.5 s each survive, and their total CPU exceeds the soft
  limit armed for the first one: the limit follows each request.
- Wiring (Linux only), after a parse through `SubprocessSourceParser`, in `/proc/<worker pid>/limits`: the soft
  "Max cpu time" is 21 plus 1 to the worker's CPU seconds rounded up; "Max core file size" is 0 and 0.
