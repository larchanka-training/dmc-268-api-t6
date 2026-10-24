# Architecture

## Handlers and the database

HTTP handlers in `app/api/` only translate requests and responses. They must not
build queries or use an `AsyncSession` themselves: every database read or write
goes through a function in `app/services/`, which owns the session, the
transaction and the tenant filter. Report a handler that queries the database
directly as a correctness problem.

# Security

SQL that includes user input must be parameterized.

# Ignore

The generated OpenAPI client is not a review source.

```review-ignore
clients/generated/**
```
