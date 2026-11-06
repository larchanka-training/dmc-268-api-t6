Template for Python/FastAPI services; adapt to your project: the paths `app/api/` and `app/services/`, the `alembic/` folder, the masks at the end.

# Architecture

## Handlers and the database

HTTP handlers in `app/api/` only translate requests and responses. They must not
build queries or use an `AsyncSession` themselves: every database read or write
goes through a function in `app/services/`, which owns the session and the
transaction. Report a handler that queries the database directly as a correctness
problem.

## Errors across layers

Only `app/api/` knows HTTP. Code in `app/services/` raises its own exceptions, and
`app/api/` maps them to status codes. Report `HTTPException` raised in
`app/services/` as a readability problem: the service then cannot run without HTTP.

# Correctness

## Blocking calls in async code

In `async def` code, a blocking call stops every other request of the worker. Report
`requests`, `time.sleep`, a synchronous SQLAlchemy `Session` or `subprocess.run`
called directly in `async def` as a performance problem.

## Work after the response

A `BackgroundTasks` task or an `asyncio.create_task` must not use the `AsyncSession`
of the request, which is closed once the response is sent: the task opens its own
session. Report such use as a correctness problem. Also report an
`asyncio.create_task` whose result nobody keeps, since the task can be garbage
collected before it finishes.

## Timestamps

Stored and compared timestamps are timezone-aware UTC. Report `datetime.utcnow()` and
`datetime.now()` without a timezone as a correctness problem: comparing them with an
aware value raises `TypeError`.

## Migrations

A migration in `alembic/` must work while the previous release is still running.
Report a new non-null column without a server default, and a dropped or renamed column
that the previous release still reads, as a correctness problem.

# Security

## Response models

A route declares a Pydantic response model. Report a route that returns an ORM object
or a plain dict of its columns as a security problem: a column added later reaches
the client without anyone deciding so.

# Complexity

Report a function that this pull request adds or grows to more than 50 lines or more
than 3 levels of nesting as a readability problem, and name the piece to extract.

# Ignore

Generated clients and protobuf modules are not a review source.

```review-ignore
clients/generated/**
**/*_pb2.py
**/*_pb2_grpc.py
```
