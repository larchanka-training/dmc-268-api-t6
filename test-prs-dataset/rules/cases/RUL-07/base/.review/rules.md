# Reliability

## Telemetry never fails a request

Telemetry is best effort. In `app/telemetry/`, a broad `except Exception:` that
silently passes around a spool or upload call is the intended pattern: telemetry
must never turn a successful request into an error, and the uploader counts its
own failures. Do not report it as swallowing errors.

## Resources

Files, sockets and HTTP clients are closed deterministically with `with` or
`async with`, also in best-effort code.
