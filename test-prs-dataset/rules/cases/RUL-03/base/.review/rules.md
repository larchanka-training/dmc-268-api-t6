# Time

## Timezone-aware timestamps

All timestamps are timezone-aware UTC. Get the current time from
`app.common.clock.utcnow()`; never call `datetime.now()` or `datetime.utcnow()` in
`app/`. Stored values are ISO 8601 strings with an offset, so a naive value breaks
the string comparisons in `app/auth/`.

# Security

SQL that includes user input must be parameterized.
