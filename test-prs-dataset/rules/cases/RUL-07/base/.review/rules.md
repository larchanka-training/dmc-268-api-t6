# Reliability

## Telemetry never fails a request

Telemetry is best effort. In `app/telemetry/`, a broad `except Exception` that logs
at debug level and returns is the intended pattern: telemetry must never turn a
successful request into an error. Do not report it as swallowing errors.

## Resources

Files, sockets and HTTP clients are closed deterministically with `with` or
`async with`, also in best-effort code.
